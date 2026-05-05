"""Tests for unified reminder orchestration behavior."""
from __future__ import annotations

from datetime import datetime, timezone

from services.alarms import reminder_push_job


class _FakeQuery:
    def __init__(self, docs):
        self.docs = docs

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return self.docs


class _FakeDocRef:
    def __init__(self, path: str):
        self.path = path


class _FakeReminderDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data
        self.reference = _FakeDocRef(f"reminders/{doc_id}")

    def to_dict(self):
        return dict(self._data)


class _FakeSnapshot:
    def __init__(self, payload: dict | None):
        self._payload = payload or {}
        self.exists = payload is not None

    def to_dict(self):
        return dict(self._payload)


class _FakeCollection:
    def __init__(self, store: dict):
        self.store = store
        self._doc_id = None

    def document(self, doc_id: str):
        self._doc_id = doc_id
        return self

    def get(self):
        return _FakeSnapshot(self.store.get(self._doc_id))


class _FakeClient:
    def __init__(self, reminder_docs, users, characters):
        self.reminder_docs = reminder_docs
        self.users = users
        self.characters = characters

    def collection_group(self, name: str):
        assert name == "reminders"
        return _FakeQuery(self.reminder_docs)

    def collection(self, name: str):
        if name == "users":
            return _FakeCollection(self.users)
        if name == "characters":
            return _FakeCollection(self.characters)
        raise AssertionError(f"unexpected collection {name}")


def _make_client(reminder_data: dict, user_payload: dict | None = None) -> _FakeClient:
    doc = _FakeReminderDoc("rem-1", reminder_data)
    return _FakeClient(
        [doc],
        {
            "+1": user_payload or {
                "name": "Yan",
                "timezone": "UTC",
                "fcm": "ExponentPushToken[test]",
                "characterIds": ["char-1"],
            }
        },
        {
            "char-1": {
                "profile": {"name": "Milu", "personality": "warm", "characterToUser": "friend"},
                "emotionUrls": {"normal": {"thumbnail": "https://example.com/a.png"}},
            }
        },
    )


def test_delivery_channel_defaults_to_app_when_missing(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "take vitamins",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
        }
    )

    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    calls = []
    monkeypatch.setattr(
        reminder_push_job,
        "_send_app_notification",
        lambda **kwargs: calls.append("app") or True,
    )
    monkeypatch.setattr(
        reminder_push_job,
        "_send_plushie_notification",
        lambda **kwargs: calls.append("plushie") or True,
    )
    monkeypatch.setattr(
        reminder_push_job,
        "_finalize_if_occurrence_matches",
        lambda **kwargs: True,
    )

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 1
    assert calls == ["app"]
    assert result["results"][0]["deliveryChannel"] == ["app"]


def test_both_channel_reminder_sends_app_then_plushie_then_finalizes(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "drink water",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
            "deliveryChannel": ["app", "plushie"],
            "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "reminder"}],
            "context": "drink water",
        }
    )

    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    order: list[str] = []
    monkeypatch.setattr(
        reminder_push_job,
        "_send_app_notification",
        lambda **kwargs: order.append("app") or True,
    )
    plushie_calls = []
    monkeypatch.setattr(
        reminder_push_job,
        "_send_plushie_notification",
        lambda **kwargs: plushie_calls.append(kwargs) or order.append("plushie") or True,
    )
    captured = {}

    def fake_finalize(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(reminder_push_job, "_finalize_if_occurrence_matches", fake_finalize)

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert order == ["app", "plushie"]
    assert result["triggered"] == 1
    assert result["results"][0]["appSent"] is True
    assert result["results"][0]["plushieSent"] is True
    assert plushie_calls[0]["first_message"] == "msg"
    assert captured["expected_occurrence_iso"] == "2026-04-25T08:00:00Z"
    assert captured["updates"]["lastDelivered.app.at"] == now.isoformat()
    assert captured["updates"]["lastDelivered.plushie.at"] == now.isoformat()
    assert captured["updates"]["lastDelivered.occurrenceUTC"] == "2026-04-25T08:00:00Z"


def test_plushie_success_can_finalize_dual_channel_when_app_fails(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "stretch",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
            "deliveryChannel": ["app", "plushie"],
            "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "reminder"}],
        }
    )

    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: False)
    monkeypatch.setattr(reminder_push_job, "_send_plushie_notification", lambda **kwargs: True)
    captured = {}
    monkeypatch.setattr(
        reminder_push_job,
        "_finalize_if_occurrence_matches",
        lambda **kwargs: captured.update(kwargs) or True,
    )

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 1
    assert result["results"][0]["appSent"] is False
    assert result["results"][0]["plushieSent"] is True
    assert "lastDelivered.app.at" not in captured["updates"]
    assert captured["updates"]["lastDelivered.plushie.at"] == now.isoformat()


def test_stale_occurrence_skips_finalize(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "call mom",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
            "deliveryChannel": ["app"],
        }
    )

    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: True)
    monkeypatch.setattr(reminder_push_job, "_finalize_if_occurrence_matches", lambda **kwargs: False)

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "stale_occurrence"


def test_reminder_allowlist_filters_non_test_user(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "call mom",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
            "deliveryChannel": ["app"],
        }
    )

    monkeypatch.setenv("REMINDER_USER_ALLOWLIST", "+11111111111")
    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: True)

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "user_filtered"


def test_reminder_lateness_cap_skips_old_occurrence(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "stale reminder",
            "nextOccurrenceUTC": "2026-04-25T07:56:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "07:56", "days": ["Fri"]},
            "deliveryChannel": ["app"],
        }
    )

    monkeypatch.setenv("REMINDER_MAX_LATENESS_SECONDS", "180")
    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: True)

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "too_late"


def test_reminder_lateness_cap_allows_recent_occurrence(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "recent reminder",
            "nextOccurrenceUTC": "2026-04-25T07:58:30Z",
            "schedule": {"repeat": "weekly", "timeLocal": "07:58", "days": ["Fri"]},
            "deliveryChannel": ["app"],
        }
    )

    monkeypatch.setenv("REMINDER_MAX_LATENESS_SECONDS", "180")
    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: True)
    monkeypatch.setattr(
        reminder_push_job,
        "_finalize_if_occurrence_matches",
        lambda **kwargs: True,
    )

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 1
    assert result["skipped"] == 0


def test_recurring_reminder_requires_user_timezone(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    client = _make_client(
        {
            "uid": "+1",
            "label": "timezone required",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
            "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Fri"]},
            "deliveryChannel": ["app"],
        },
        user_payload={
            "name": "Yan",
            "timezone": "",
            "fcm": "ExponentPushToken[test]",
            "characterIds": ["char-1"],
        },
    )

    monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
    monkeypatch.setattr(reminder_push_job, "_send_app_notification", lambda **kwargs: True)

    result = reminder_push_job.run_send_reminder_push_job(
        execute=True,
        now=now,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "missing_user_timezone"


def test_plushie_session_hydration_includes_reminder_title(monkeypatch):
    now = datetime(2026, 4, 25, 8, 0, tzinfo=timezone.utc)
    created = {}

    class _FakeStore:
        def get_session(self, device_id, now=None):
            return None

        def create_session(
            self,
            *,
            device_id,
            session_type,
            ttl,
            triggered_at,
            session_config,
        ):
            created.update(
                {
                    "device_id": device_id,
                    "session_type": session_type,
                    "ttl": ttl,
                    "triggered_at": triggered_at,
                    "session_config": session_config,
                }
            )
            return type("Session", (), {"device_id": device_id})()

        def delete_session(self, device_id):
            raise AssertionError("delete_session should not be called on publish success")

    monkeypatch.setenv("ALARM_WS_URL", "wss://example.com/ws")
    monkeypatch.setenv("ALARM_MQTT_URL", "mqtt://example.com")
    monkeypatch.setattr(reminder_push_job, "session_context_store", _FakeStore())
    monkeypatch.setattr(reminder_push_job, "publish_ws_start", lambda *args, **kwargs: True)

    sent = reminder_push_job._send_plushie_notification(
        reminder_id="rem-123",
        reminder_data={
            "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "reminder"}],
            "context": "drink water",
        },
        uid="+1",
        label="Drink water",
        first_message="Don't forget to drink water.",
        now=now,
    )

    assert sent is True
    assert created["session_type"] == "alarm"
    assert created["session_config"]["mode"] == "scheduled_conversation"
    assert created["session_config"]["reminderId"] == "rem-123"
    assert created["session_config"]["label"] == "Drink water"
    assert created["session_config"]["title"] == "Drink water"
    assert created["session_config"]["context"] == "drink water"
    assert created["session_config"]["firstMessage"] == "Don't forget to drink water."


def test_android_expo_push_preserves_legacy_data_fields_with_rich_content(monkeypatch):
    published = {}

    class _FakeResponse:
        def validate_response(self):
            return None

    class _FakePushClient:
        @staticmethod
        def is_exponent_push_token(token):
            return True

        def publish(self, push_message):
            published["payload"] = push_message.get_payload()
            return _FakeResponse()

    monkeypatch.setattr(reminder_push_job, "PushClient", _FakePushClient)

    sent = reminder_push_job._send_app_notification(
        reminder_id="rem-123",
        reminder_data={},
        uid="+1",
        user_data={
            "fcm": "ExponentPushToken[test]",
            "system": "android",
        },
        character_data={
            "profile": {"name": "Milu"},
            "emotionUrls": {"normal": {"thumbnail": "https://example.com/avatar.png"}},
        },
        label="Drink water",
        next_occurrence_str="2026-04-25T08:00:00Z",
        ai_message="Time to drink water.",
    )

    assert sent is True
    assert published["payload"] == {
        "to": "ExponentPushToken[test]",
        "data": {
            "type": "reminder",
            "title": "Milu",
            "body": "Time to drink water.",
            "largeIcon": "https://example.com/avatar.png",
            "reminderId": "rem-123",
            "action": "custom_display",
            "label": "Drink water",
            "nextOccurrenceUTC": "2026-04-25T08:00:00Z",
        },
        "title": "Milu",
        "body": "Time to drink water.",
        "priority": "high",
        "sound": "reminder_sound.wav",
        "channelId": "reminders",
        "richContent": {"image": "https://example.com/avatar.png"},
    }
