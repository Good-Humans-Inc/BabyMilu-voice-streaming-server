from __future__ import annotations

from datetime import datetime, timezone

from services.alarms import repair_next_occurrences


class _FakeUser:
    def __init__(self, user_id: str, payload: dict | None = None, exists: bool = True):
        self.id = user_id
        self._payload = payload or {"timezone": "UTC"}
        self.exists = exists
        self.docs = []
        self.reference = _FakeUserRef(self)

    def get(self):
        return self

    def to_dict(self):
        return dict(self._payload)


class _FakeUserRef:
    def __init__(self, user: _FakeUser):
        self._user = user

    def collection(self, name: str):
        assert name == "reminders"
        return _FakeQuery(self._user.docs)


class _FakeCollectionRef:
    def __init__(self, parent):
        self.parent = parent


class _FakeRef:
    def __init__(self, path: str, user: _FakeUser):
        self.path = path
        self.parent = _FakeCollectionRef(user)
        self.writes = []

    def set(self, payload: dict, merge: bool = False):
        self.writes.append((payload, merge))


class _FakeDoc:
    def __init__(self, path: str, data: dict, user: _FakeUser | None = None):
        self.id = path.split("/")[-1]
        self._data = data
        self._user = user or _FakeUser("user-1")
        self.reference = _FakeRef(path, self._user)
        self._user.docs.append(self)

    def to_dict(self):
        return dict(self._data)


class _FakeQuery:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return list(self._docs)


class _FakeClient:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs
        self._users = []
        seen = set()
        for doc in docs:
            user = doc.reference.parent.parent
            if user.id not in seen:
                self._users.append(user)
                seen.add(user.id)
        self._refs = {doc.reference.path: doc.reference for doc in docs}

    def collection_group(self, name: str):
        assert name == "reminders"
        return _FakeQuery(self._docs)

    def collection(self, name: str):
        assert name == "users"
        return _FakeQuery(self._users)

    def document(self, path: str):
        return self._refs[path]


def test_repair_stale_recurring_reminder_dry_run_does_not_write():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    doc = _FakeDoc(
        "users/user-1/reminders/rem-1",
        {
            "status": "on",
            "label": "Daily check-in",
            "nextOccurrenceUTC": "2026-06-06T07:00:00.000Z",
            "schedule": {"repeat": "daily", "timeLocal": "07:00"},
        },
    )

    result = repair_next_occurrences.repair_stale_next_occurrences(
        now=now,
        execute=False,
        client=_FakeClient([doc]),
    )

    assert result["impacted"] == 1
    assert result["repairable"] == 1
    assert result["updated"] == 0
    assert result["results"][0]["kind"] == "reminder"
    assert (
        result["results"][0]["update"]["nextOccurrenceUTC"]
        == "2026-06-10T07:00:00.000Z"
    )
    assert "nextTriggerUTC" in result["results"][0]["update"]
    assert doc.reference.writes == []


def test_repair_stale_recurring_alarm_execute_writes_cursor_only():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    doc = _FakeDoc(
        "users/user-1/reminders/alarm-1",
        {
            "status": "on",
            "typeHint": "alarm",
            "label": "Wake up",
            "nextOccurrenceUTC": "2026-06-06T07:00:00.000Z",
            "schedule": {"repeat": "daily", "timeLocal": "07:00"},
        },
    )

    result = repair_next_occurrences.repair_stale_next_occurrences(
        now=now,
        execute=True,
        client=_FakeClient([doc]),
    )

    assert result["impacted"] == 1
    assert result["repairable"] == 1
    assert result["updated"] == 1
    payload, merge = doc.reference.writes[0]
    assert merge is True
    assert payload == {
        "nextOccurrenceUTC": "2026-06-10T07:00:00.000Z",
        "updatedAt": "2026-06-09T12:00:00.000Z",
    }


def test_repair_stale_one_time_alarm_reports_no_future_occurrence():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    doc = _FakeDoc(
        "users/user-1/reminders/alarm-1",
        {
            "status": "on",
            "typeHint": "alarm",
            "label": "Past one-time alarm",
            "nextOccurrenceUTC": "2026-06-06T07:00:00.000Z",
            "schedule": {
                "repeat": "once",
                "timeLocal": "07:00",
                "days": ["2026-06-06"],
            },
        },
    )

    result = repair_next_occurrences.repair_stale_next_occurrences(
        now=now,
        execute=True,
        client=_FakeClient([doc]),
    )

    assert result["impacted"] == 1
    assert result["repairable"] == 0
    assert result["updated"] == 0
    assert result["results"][0]["reason"] == "no_future_occurrence"
    assert doc.reference.writes == []


def test_repair_missing_recurring_reminder_with_all_active_scan():
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    doc = _FakeDoc(
        "users/user-1/reminders/rem-missing",
        {
            "status": "on",
            "label": "Missing cursor",
            "schedule": {"repeat": "daily", "timeLocal": "07:00"},
        },
    )

    result = repair_next_occurrences.repair_stale_next_occurrences(
        now=now,
        execute=False,
        client=_FakeClient([doc]),
        scan_mode="all-active",
    )

    assert result["scanMode"] == "all-active"
    assert result["impacted"] == 1
    assert result["repairable"] == 1
    assert result["results"][0]["currentNextOccurrenceUTC"] is None
    assert (
        result["results"][0]["update"]["nextOccurrenceUTC"]
        == "2026-06-10T07:00:00.000Z"
    )
