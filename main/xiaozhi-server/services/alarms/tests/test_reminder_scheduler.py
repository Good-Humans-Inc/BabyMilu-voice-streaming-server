from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.alarms import reminder_scheduler


class _FakeParent:
    def __init__(self, parent=None, id_value="", payload=None, exists=True):
        self.parent = parent
        self.id = id_value
        self._payload = payload or {}
        self.exists = exists

    def get(self):
        return self

    def to_dict(self):
        return dict(self._payload)


class _FakeRef:
    def __init__(self, path, user_id, user_payload=None):
        self.path = path
        user_parent = _FakeParent(
            parent=None,
            id_value=user_id,
            payload=user_payload or {"timezone": "UTC"},
            exists=True,
        )
        self.parent = _FakeParent(parent=user_parent)
        self._writes = []

    def set(self, payload, merge=False):
        self._writes.append((payload, merge))


class _FakeDoc:
    def __init__(self, path, doc_id, user_id, data, user_payload=None):
        self.reference = _FakeRef(path, user_id, user_payload=user_payload)
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


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
        self._refs = {doc.reference.path: doc.reference for doc in docs}

    def collection_group(self, name):
        assert name == "reminders"
        return _FakeQuery(self._docs)

    def document(self, path):
        return self._refs[path]


def test_fetch_due_reminders_reads_collection_group(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "nextTriggerUTC": now.isoformat(),
        "schedule": {"repeat": "none", "timeLocal": "08:00"},
        "label": "Take vitamins",
    }
    docs = [
        _FakeDoc(
            path="users/user-1/reminders/rem-1",
            doc_id="rem-1",
            user_id="user-1",
            data=data,
        )
    ]
    client = _FakeClient(docs)
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    results = reminder_scheduler.fetch_due_reminders(
        now=now, lookahead=timedelta(minutes=2), client=client
    )

    assert len(results) == 1
    assert results[0].reminder_id == "rem-1"
    assert results[0].user_id == "user-1"
    assert results[0].repeat == "none"


def test_process_due_reminders_dry_run_does_not_write(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "nextTriggerUTC": now.isoformat(),
        "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Mon"]},
        "label": "Workout",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-2",
        doc_id="rem-2",
        user_id="user-1",
        data=data,
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(minutes=2),
        execute=False,
        client=client,
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["triggered"] == 0
    assert result["results"][0]["dryRun"] is True
    assert doc.reference._writes == []


def test_process_due_reminders_uses_next_occurrence_for_last_processed(monkeypatch):
    now = datetime.now(timezone.utc)
    next_occurrence = now - timedelta(minutes=5)
    next_trigger = next_occurrence - timedelta(minutes=30)
    data = {
        "status": "on",
        "nextOccurrenceUTC": next_occurrence.isoformat(),
        "nextTriggerUTC": next_trigger.isoformat(),
        "schedule": {"repeat": "none", "timeLocal": "08:00"},
        "label": "Medication",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-3",
        doc_id="rem-3",
        user_id="user-1",
        data=data,
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(minutes=10),
        execute=True,
        trigger_fn=lambda reminder: True,
        client=client,
    )

    assert result["triggered"] == 1
    assert len(doc.reference._writes) == 1
    payload, merge = doc.reference._writes[0]
    assert merge is True
    assert payload["status"] == "off"
    expected = reminder_scheduler._format_datetime(next_occurrence)
    assert payload["lastProcessedUTC"] == expected


def test_process_due_reminders_advances_recurring_weekly(monkeypatch):
    now = datetime(2026, 3, 16, 8, 30, tzinfo=timezone.utc)  # Monday
    due = datetime(2026, 3, 16, 8, 0, tzinfo=timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": due.isoformat(),
        "schedule": {"repeat": "weekly", "timeLocal": "08:00", "days": ["Mon", "Wed"]},
        "label": "Workout",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-4",
        doc_id="rem-4",
        user_id="user-1",
        data=data,
        user_payload={"timezone": "UTC"},
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(minutes=2),
        execute=True,
        trigger_fn=lambda reminder: True,
        client=client,
    )

    assert result["triggered"] == 1
    payload, merge = doc.reference._writes[0]
    assert merge is True
    assert "status" not in payload
    expected_next = datetime(2026, 3, 18, 8, 0, tzinfo=timezone.utc)
    assert payload["nextOccurrenceUTC"] == reminder_scheduler._format_datetime(expected_next)
    assert "nextTriggerUTC" in payload


def test_process_due_reminders_daily_stale_next_jumps_past_now(monkeypatch):
    """One fire for a backlog daily; nextOccurrenceUTC must land after now, not one day ahead of due."""
    now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 3, 17, 8, 0, 0, tzinfo=timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": due.isoformat(),
        "schedule": {"repeat": "daily", "timeLocal": "08:00"},
        "label": "Pills",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-daily-stale",
        doc_id="rem-daily-stale",
        user_id="user-1",
        data=data,
        user_payload={"timezone": "UTC"},
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(days=7),
        execute=True,
        trigger_fn=lambda reminder: True,
        client=client,
    )

    assert result["triggered"] == 1
    payload, merge = doc.reference._writes[0]
    assert merge is True
    expected_next = datetime(2026, 3, 21, 8, 0, 0, tzinfo=timezone.utc)
    assert payload["nextOccurrenceUTC"] == reminder_scheduler._format_datetime(expected_next)
    assert "nextTriggerUTC" in payload


def test_process_due_reminders_recurring_requires_user_timezone(monkeypatch):
    now = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 3, 20, 8, 0, 0, tzinfo=timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": due.isoformat(),
        "schedule": {"repeat": "daily", "timeLocal": "08:00"},
        "label": "Pills",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-missing-tz",
        doc_id="rem-missing-tz",
        user_id="user-1",
        data=data,
        user_payload={"timezone": ""},
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(minutes=2),
        execute=True,
        trigger_fn=lambda reminder: True,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "missing_user_timezone"
    assert doc.reference._writes == []


def test_process_due_reminders_skips_not_yet_due_inside_lookahead(monkeypatch):
    """Lookahead may include future slots; reminders only fire when due <= now."""
    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 1, 1, 10, 15, 0, tzinfo=timezone.utc)
    data = {
        "status": "on",
        "nextOccurrenceUTC": due.isoformat(),
        "schedule": {"repeat": "daily", "timeLocal": "10:15"},
        "label": "Soon",
    }
    doc = _FakeDoc(
        path="users/user-1/reminders/rem-future",
        doc_id="rem-future",
        user_id="user-1",
        data=data,
        user_payload={"timezone": "UTC"},
    )
    client = _FakeClient([doc])
    monkeypatch.setattr(
        reminder_scheduler, "FieldFilter", lambda field_path, op, value: (field_path, op, value)
    )

    result = reminder_scheduler.process_due_reminders(
        now=now,
        lookahead=timedelta(minutes=30),
        execute=True,
        trigger_fn=lambda reminder: True,
        client=client,
    )

    assert result["triggered"] == 0
    assert result["results"][0]["skipped"] == "not_yet_due"
    assert doc.reference._writes == []
