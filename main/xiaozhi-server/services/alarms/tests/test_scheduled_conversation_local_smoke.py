from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from plugins_func.register import Action
from services.alarms import firestore_client, reminder_push_job


class _WriteClient:
    def __init__(self):
        self.path = []
        self.written = None
        self.merge = None
        self.updated = None

    def collection(self, name):
        self.path.append(("collection", name))
        return self

    def document(self, name):
        self.path.append(("document", name))
        return self

    def set(self, doc, **kwargs):
        self.written = doc
        self.merge = kwargs.get("merge")

    def update(self, doc):
        self.updated = doc


class _Query:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return self._docs


class _Reference:
    def __init__(self, path, user_id="user-1"):
        self.path = path
        self.parent = SimpleNamespace(parent=SimpleNamespace(id=user_id))


class _Doc:
    def __init__(self, path, data, *, doc_id=None, user_id="user-1"):
        self._data = data
        self.id = doc_id or path.split("/")[-1]
        self.reference = _Reference(path, user_id=user_id)

    def to_dict(self):
        return dict(self._data)


class _AlarmReadClient:
    def __init__(self, alarm_docs, device_docs):
        self._alarm_docs = alarm_docs
        self._device_docs = device_docs

    def collection_group(self, name):
        assert name == "alarms"
        return _Query(self._alarm_docs)

    def collection(self, name):
        assert name == "devices"
        return _Query(self._device_docs)


def test_daily_scheduled_conversation_hydrates_plushie_session(monkeypatch):
    now = datetime(2026, 3, 24, 16, 0, tzinfo=timezone.utc)
    fake_client = _WriteClient()

    reminder_id = firestore_client.create_scheduled_conversation(
        uid="user-1",
        device_id="90:e5:b1:a8:e4:38",
        resolved_dt=now,
        label="Take vitamins",
        context="take vitamins",
        tz_str="UTC",
        recurrence="daily",
        content="take vitamins",
        type_hint="habit",
        priority="high",
        conversation_outline="Open gently, ask if done.",
        character_reminder="Stay warm.",
        emotional_context="User is building a habit.",
        completion_signal="Done means they took vitamins.",
        delivery_preference="gentle",
        client=fake_client,
    )

    doc = fake_client.written
    assert reminder_id
    assert fake_client.path[:4] == [
        ("collection", "users"),
        ("document", "user-1"),
        ("collection", "reminders"),
        ("document", reminder_id),
    ]
    assert doc["targets"] == [
        {"deviceId": "90:e5:b1:a8:e4:38", "mode": "scheduled_conversation"}
    ]
    assert doc["deliveryChannel"] == ["app", "plushie"]
    assert doc["schedule"]["days"] == list(firestore_client.models.DAY_NAMES)
    assert "dateLocal" not in doc["schedule"]
    assert doc["content"] == "take vitamins"
    assert doc["priority"] == "high"
    assert doc["conversationOutline"] == "Open gently, ask if done."
    assert doc["characterReminder"] == "Stay warm."
    assert doc["completionSignal"] == "Done means they took vitamins."

    monkeypatch.setenv("ALARM_WS_URL", "ws://alarm.test")
    monkeypatch.setenv("MQTT_URL", "mqtt://broker.test")
    monkeypatch.setattr(reminder_push_job.session_context_store, "get_session", lambda *a, **k: None)
    monkeypatch.setattr(reminder_push_job, "publish_ws_start", lambda broker, device, ws: True)

    created = {}

    def fake_create_session(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(device_id=kwargs["device_id"])

    deleted = []
    monkeypatch.setattr(reminder_push_job.session_context_store, "create_session", fake_create_session)
    monkeypatch.setattr(reminder_push_job.session_context_store, "delete_session", deleted.append)

    sent = reminder_push_job._send_plushie_notification(
        reminder_id=reminder_id,
        reminder_data=doc,
        uid="user-1",
        label=doc["label"],
        first_message="Friendly reminder text.",
        now=now,
    )

    assert sent is True
    assert deleted == []
    assert created["session_type"] == "alarm"
    assert created["session_config"]["mode"] == "scheduled_conversation"
    assert created["session_config"]["alarmId"] == reminder_id
    assert created["session_config"]["reminderId"] == reminder_id
    assert created["session_config"]["userId"] == "user-1"
    assert created["session_config"]["content"] == "take vitamins"
    assert created["session_config"]["priority"] == "high"
    assert created["session_config"]["firstMessage"] == "Friendly reminder text."


def test_legacy_alarm_without_targets_uses_device_fallback(monkeypatch):
    now = datetime.now(timezone.utc)
    alarm_doc = _Doc(
        "users/user-1/alarms/alarm-1",
        {
            "status": "on",
            "nextOccurrenceUTC": now.isoformat(),
            "schedule": {"repeat": "once", "timeLocal": "07:00", "days": []},
            "label": "Wake up",
        },
        user_id="user-1",
    )
    device_doc = _Doc(
        "devices/90e5b1a8e438",
        {"deviceId": "90:e5:b1:a8:e4:38"},
        doc_id="90e5b1a8e438",
    )
    client = _AlarmReadClient([alarm_doc], [device_doc])

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *args, **kwargs: (args, kwargs))
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    due = firestore_client.fetch_due_alarms(now, timedelta(minutes=1), client=client)

    assert len(due) == 1
    assert due[0].alarm_id == "alarm-1"
    assert due[0].targets[0].device_id == "90:e5:b1:a8:e4:38"
    assert due[0].targets[0].mode == "morning_alarm"


def test_config_exposes_new_reminder_tools_and_preserves_magic_camera():
    config_path = Path(__file__).resolve().parents[3] / "config.yaml"
    text = config_path.read_text(encoding="utf-8")

    assert "      - schedule_conversation\n" in text
    assert "      - list_reminders\n" in text
    assert "      - cancel_reminder\n" in text
    assert "      - modify_reminder\n" in text
    assert "      - complete_reminder\n" in text
    assert "      - inspect_recent_magic_camera_photo\n" in text
    assert "      - set_reminder\n" not in text


def test_schedule_conversation_smoke_uses_portable_result_format(monkeypatch):
    from plugins_func.functions import schedule_conversation as sc_module

    resolved = datetime(2026, 3, 24, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    conn = SimpleNamespace(device_id="90:e5:b1:a8:e4:38", mode_session=None)

    monkeypatch.setattr(sc_module, "get_timezone_for_device", lambda device_id: "America/Los_Angeles")
    monkeypatch.setattr(sc_module, "get_owner_phone_for_device", lambda device_id: "user-1")
    monkeypatch.setattr(sc_module.dateparser, "parse", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(sc_module, "create_scheduled_conversation", lambda **kwargs: "reminder-123")

    result = sc_module.schedule_conversation(
        conn,
        time_expression="tomorrow at 9am",
        label="Take vitamins",
        content="take vitamins",
        type_hint="habit",
        priority="medium",
        conversation_outline="Open gently.",
        character_reminder="Stay warm.",
        emotional_context="User is building a habit.",
        completion_signal="Done means they took vitamins.",
    )

    assert result.action == Action.REQLLM
    assert "Tuesday, March 24 at 9:00 AM" in result.result
    assert "reminder-123" in result.result
