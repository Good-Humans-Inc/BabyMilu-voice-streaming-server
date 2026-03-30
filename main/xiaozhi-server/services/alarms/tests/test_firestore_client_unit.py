from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.alarms import firestore_client


class _FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return self._docs


class _FakeClient:
    def __init__(self, docs):
        self._docs = docs

    def collection_group(self, name):
        assert name == "alarms"
        return _FakeQuery(self._docs)


class _FakeDoc:
    def __init__(self, path, data):
        self._data = data
        self.reference = type("Ref", (), {"path": path, "parent": type("Parent", (), {"parent": None})()})
        self.id = path.split("/")[-1]

    def to_dict(self):
        return dict(self._data)


class _FakeWriteRef:
    """Captures the doc dict passed to .set() or .update()."""
    def __init__(self):
        self.written = None
        self.merge = None
        self.updated = None

    def set(self, doc, **kwargs):
        self.written = doc
        self.merge = kwargs.get("merge")

    def update(self, doc):
        self.updated = doc


class _FakeWriteClient:
    """Minimal fake that supports collection().document().collection().document().set/.update()."""
    def __init__(self):
        self._ref = _FakeWriteRef()

    def collection(self, name):
        return self

    def document(self, name):
        return self

    def set(self, doc, **kwargs):
        self._ref.set(doc, **kwargs)

    def update(self, doc):
        self._ref.update(doc)

    @property
    def written(self):
        return self._ref.written

    @property
    def merge(self):
        return self._ref.merge

    @property
    def updated(self):
        return self._ref.updated


class _FakeUserScopedClient:
    """Supports client.collection().document().collection().where().stream()."""
    def __init__(self, docs):
        self._docs = docs

    def collection(self, *args, **kwargs):
        return self

    def document(self, *args, **kwargs):
        return self

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return self._docs


def test_create_scheduled_conversation_writes_correct_mode_and_fields():
    now = datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc)
    fake_client = _FakeWriteClient()

    alarm_id = firestore_client.create_scheduled_conversation(
        uid="15551234567",
        device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=now,
        label="take vitamins",
        context="take vitamins",
        tz_str="UTC",
        content="take vitamins",
        type_hint="habit",
        priority="medium",
        conversation_outline="1. Open gently.",
        character_reminder="Be warm.",
        emotional_context="User was tired.",
        completion_signal="Done: user confirms.",
        delivery_preference="be gentle",
        client=fake_client,
    )

    assert isinstance(alarm_id, str) and len(alarm_id) == 36  # UUID
    doc = fake_client.written
    assert doc is not None
    assert doc["targets"] == [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "scheduled_conversation"}]
    assert doc["content"] == "take vitamins"
    assert doc["typeHint"] == "habit"
    assert doc["priority"] == "medium"
    assert doc["conversationOutline"] == "1. Open gently."
    assert doc["characterReminder"] == "Be warm."
    assert doc["emotionalContext"] == "User was tired."
    assert doc["completionSignal"] == "Done: user confirms."
    assert doc["deliveryPreference"] == "be gentle"
    assert doc["status"] == "on"
    assert doc["label"] == "take vitamins"


def test_create_scheduled_conversation_content_defaults_to_label():
    """When content is None, it should fall back to label."""
    now = datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc)
    fake_client = _FakeWriteClient()

    firestore_client.create_scheduled_conversation(
        uid="15551234567",
        device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=now,
        label="check in",
        context="check in",
        tz_str="UTC",
        content=None,
        client=fake_client,
    )

    assert fake_client.written["content"] == "check in"


def test_fetch_due_alarms_skips_docs_without_targets(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "weekly", "timeLocal": "07:00", "days": ["Mon"]},
        # intentionally omit "targets"
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert results == []


def test_fetch_due_alarms_skips_docs_with_invalid_repeat(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "everyday", "timeLocal": "07:00", "days": ["Mon"]},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "morning_alarm"}],
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert results == []


def test_fetch_due_alarms_supports_none_repeat(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "none", "timeLocal": "07:00", "days": ["2026-03-02"]},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "morning_alarm"}],
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert len(results) == 1
    assert results[0].schedule.repeat == firestore_client.models.AlarmRepeat.NONE


def test_fetch_due_alarms_reads_scheduled_conversation_fields(monkeypatch):
    """AlarmDoc is populated with V0 fields when present in the Firestore doc."""
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "once", "timeLocal": "09:00", "days": ["2026-03-24"]},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "scheduled_conversation"}],
        "content": "take vitamins",
        "typeHint": "habit",
        "priority": "medium",
        "conversationOutline": "1. Open gently.",
        "characterReminder": "Be warm.",
        "emotionalContext": "User was tired.",
        "completionSignal": "Done: user confirms.",
        "deliveryPreference": "be gentle",
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert len(results) == 1
    alarm = results[0]
    assert alarm.content == "take vitamins"
    assert alarm.type_hint == "habit"
    assert alarm.priority == "medium"
    assert alarm.conversation_outline == "1. Open gently."
    assert alarm.character_reminder == "Be warm."
    assert alarm.emotional_context == "User was tired."
    assert alarm.completion_signal == "Done: user confirms."
    assert alarm.delivery_preference == "be gentle"


def test_fetch_due_alarms_v0_fields_are_none_when_absent(monkeypatch):
    """Legacy morning_alarm docs without V0 fields produce None on AlarmDoc."""
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "weekly", "timeLocal": "07:00", "days": ["Mon"]},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "morning_alarm"}],
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert len(results) == 1
    alarm = results[0]
    assert alarm.content is None
    assert alarm.type_hint is None
    assert alarm.conversation_outline is None


def test_fetch_due_alarms_supports_once_repeat_alias(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "once", "timeLocal": "07:00", "days": ["2026-03-02"]},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "morning_alarm"}],
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert len(results) == 1
    assert results[0].schedule.repeat == firestore_client.models.AlarmRepeat.NONE


def test_fetch_due_alarms_supports_daily_repeat(monkeypatch):
    """AlarmRepeat.DAILY is parsed correctly; daily alarm is returned (not skipped)."""
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "daily", "timeLocal": "08:00", "days": []},
        "targets": [{"deviceId": "90:e5:b1:a8:e4:38", "mode": "scheduled_conversation"}],
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-1", data)]
    client = _FakeClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_alarms(
        now, lookahead=timedelta(minutes=1), client=client
    )

    assert len(results) == 1
    assert results[0].schedule.repeat == firestore_client.models.AlarmRepeat.DAILY


def test_fetch_active_alarms_for_user_returns_on_alarms(monkeypatch):
    """Returns scheduled_conversation alarms when stream has a valid doc."""
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "once", "timeLocal": "09:00", "days": ["2026-03-29"]},
        "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "scheduled_conversation"}],
        "content": "take vitamins",
        "label": "take vitamins",
    }
    docs = [_FakeDoc("users/user-1/alarms/alarm-42", data)]
    client = _FakeUserScopedClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    results = firestore_client.fetch_active_alarms_for_user("user-1", client=client)

    assert len(results) == 1
    assert results[0].alarm_id == "alarm-42"
    assert results[0].content == "take vitamins"


def test_fetch_active_alarms_for_user_skips_morning_alarm_mode(monkeypatch):
    """Python-level filter excludes morning_alarm docs; only scheduled_conversation returned."""
    now = datetime.now(timezone.utc)
    base = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "schedule": {"repeat": "once", "timeLocal": "09:00", "days": ["2026-03-29"]},
    }
    docs = [
        _FakeDoc("users/user-1/alarms/alarm-sc", {
            **base,
            "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "scheduled_conversation"}],
            "content": "gym",
        }),
        _FakeDoc("users/user-1/alarms/alarm-ma", {
            **base,
            "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "morning_alarm"}],
        }),
    ]
    client = _FakeUserScopedClient(docs)

    monkeypatch.setattr(
        firestore_client, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    results = firestore_client.fetch_active_alarms_for_user("user-1", client=client)

    assert len(results) == 1
    assert results[0].alarm_id == "alarm-sc"


def test_cancel_scheduled_conversation_writes_status_off():
    """Sets status=off and updatedAt; does NOT write lastProcessedUTC."""
    fake_client = _FakeWriteClient()

    firestore_client.cancel_scheduled_conversation(
        uid="user-1",
        alarm_id="alarm-42",
        client=fake_client,
    )

    doc = fake_client.written
    assert doc is not None
    assert doc["status"] == "off"
    assert "updatedAt" in doc
    assert "lastProcessedUTC" not in doc
    assert fake_client.merge is True


def test_modify_scheduled_conversation_updates_top_level_fields():
    """Non-time fields are written via update(); schedule fields are untouched."""
    fake_client = _FakeWriteClient()

    firestore_client.modify_scheduled_conversation(
        uid="user-1",
        alarm_id="alarm-42",
        content="updated gym session",
        priority="high",
        delivery_preference="be direct",
        client=fake_client,
    )

    doc = fake_client.updated
    assert doc is not None
    assert doc["content"] == "updated gym session"
    assert doc["label"] == "updated gym session"
    assert doc["priority"] == "high"
    assert doc["deliveryPreference"] == "be direct"
    assert "updatedAt" in doc
    # time fields not touched
    assert "nextOccurrenceUTC" not in doc
    assert "schedule.timeLocal" not in doc


def test_modify_scheduled_conversation_updates_time_fields():
    """When resolved_dt is provided, schedule dot-notation keys and nextOccurrenceUTC are written."""
    from datetime import timezone as tz_module
    fake_client = _FakeWriteClient()
    resolved = datetime(2026, 4, 1, 9, 0, tzinfo=tz_module.utc)

    firestore_client.modify_scheduled_conversation(
        uid="user-1",
        alarm_id="alarm-42",
        resolved_dt=resolved,
        tz_str="UTC",
        client=fake_client,
    )

    doc = fake_client.updated
    assert doc is not None
    assert "nextOccurrenceUTC" in doc
    assert doc["schedule.timeLocal"] == "09:00"
    assert doc["schedule.days"] == ["2026-04-01"]
    # non-time fields not present (only updatedAt + time keys)
    assert "content" not in doc
    assert "priority" not in doc

