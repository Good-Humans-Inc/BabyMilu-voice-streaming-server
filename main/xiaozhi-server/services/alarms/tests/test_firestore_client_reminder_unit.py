"""Unit tests for the reminder-specific Firestore functions.

Tests:
  create_reminder()       — path, deliveryChannel, mode field in targets
  fetch_due_reminders()   — collection group name, deliveryChannel filter, skip logic

All Firestore I/O is faked via _FakeClient / _FakeDoc — no real network calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.alarms import firestore_client


# ---------------------------------------------------------------------------
# Shared fake Firestore infrastructure
# ---------------------------------------------------------------------------

class _FakeQuery:
    """Captures every .where() call so tests can assert on filters."""

    def __init__(self, docs):
        self._docs = docs
        self.filters: list = []

    def where(self, filter=None, *args, **kwargs):
        # Store whatever filter tuple/object is passed for inspection
        self.filters.append(filter)
        return self

    def stream(self):
        return iter(self._docs)


class _FakeCollectionGroupClient:
    """Accepts only the 'reminders' collection group (rejects 'alarms')."""

    def __init__(self, docs, expected_group: str = "reminders"):
        self._docs = docs
        self._expected_group = expected_group
        self.last_query: _FakeQuery | None = None

    def collection_group(self, name: str) -> _FakeQuery:
        assert name == self._expected_group, (
            f"Expected collection_group('{self._expected_group}') but got '{name}'"
        )
        self.last_query = _FakeQuery(self._docs)
        return self.last_query


class _FakeWriteClient:
    """Captures set() calls so create_reminder tests can assert on the written doc."""

    def __init__(self):
        self.written: list[tuple[str, dict]] = []  # [(path, data)]
        self._last_doc_ref = None

    def collection(self, name: str) -> "_FakeCollRef":
        return _FakeCollRef(self, name)

    def get_last_written(self):
        assert self.written, "No document was written"
        return self.written[-1]


class _FakeCollRef:
    def __init__(self, client: _FakeWriteClient, name: str):
        self._client = client
        self._path = name

    def document(self, doc_id: str) -> "_FakeCollRef":
        self._path = f"{self._path}/{doc_id}"
        return self

    def collection(self, name: str) -> "_FakeCollRef":
        self._path = f"{self._path}/{name}"
        return self

    def set(self, data: dict):
        self._client.written.append((self._path, data))


class _FakeDoc:
    """Minimal Firestore DocumentSnapshot stub."""

    def __init__(self, path: str, data: dict):
        self._data = data
        parent_path = "/".join(path.split("/")[:-1])
        grandparent_path = "/".join(path.split("/")[:-2])
        uid = path.split("/")[1]

        _parent_parent = type("PP", (), {"id": uid})()
        _parent = type("P", (), {"parent": _parent_parent})()
        self.reference = type("Ref", (), {
            "path": path,
            "parent": _parent,
        })()
        self.id = path.split("/")[-1]

    def to_dict(self):
        return dict(self._data)


# ---------------------------------------------------------------------------
# create_reminder() — write path & document shape
# ---------------------------------------------------------------------------

def test_create_reminder_writes_to_reminders_collection():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    firestore_client.create_reminder(
        uid="+15551234567",
        device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt,
        label="drink water",
        context="drink water",
        tz_str="America/Los_Angeles",
        client=client,
    )

    path, data = client.get_last_written()
    # Path must include "reminders", NOT "alarms"
    assert "reminders" in path
    assert "alarms" not in path
    assert "+15551234567" in path


def test_create_reminder_does_not_write_to_alarms_collection():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1",
        device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt,
        label="test",
        context="test",
        tz_str="UTC",
        client=client,
    )

    path, _ = client.get_last_written()
    assert "alarms" not in path


def test_create_reminder_includes_delivery_channel_plushie():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        client=client,
    )

    _, data = client.get_last_written()
    assert "deliveryChannel" in data
    assert "plushie" in data["deliveryChannel"]


def test_create_reminder_sets_mode_to_reminder_in_targets():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        client=client,
    )

    _, data = client.get_last_written()
    assert data["targets"][0]["mode"] == "reminder"


def test_create_reminder_sets_correct_target_device_id():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        client=client,
    )

    _, data = client.get_last_written()
    assert data["targets"][0]["deviceId"] == "aa:bb:cc:dd:ee:ff"


def test_create_reminder_sets_status_on_and_source_voice():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="take vitamins", context="take vitamins", tz_str="UTC",
        client=client,
    )

    _, data = client.get_last_written()
    assert data["status"] == "on"
    assert data["source"] == "voice"
    assert data["label"] == "take vitamins"


def test_create_reminder_sets_repeat_none():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        client=client,
    )

    _, data = client.get_last_written()
    assert data["schedule"]["repeat"] == "none"
    assert data["schedule"]["dateLocal"] == "2026-06-01"


def test_create_reminder_custom_delivery_channels():
    """delivery_channels param overrides default."""
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        delivery_channels=["plushie", "app"],
        client=client,
    )

    _, data = client.get_last_written()
    assert data["deliveryChannel"] == ["plushie", "app"]


def test_create_reminder_returns_reminder_id():
    client = _FakeWriteClient()
    resolved_dt = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    reminder_id = firestore_client.create_reminder(
        uid="+1", device_id="aa:bb:cc:dd:ee:ff",
        resolved_dt=resolved_dt, label="test", context="test", tz_str="UTC",
        client=client,
    )

    assert isinstance(reminder_id, str)
    assert len(reminder_id) > 0


# ---------------------------------------------------------------------------
# fetch_due_reminders() — collection group and filter assertions
# ---------------------------------------------------------------------------

def _reminder_data(now: datetime, *, repeat: str = "none") -> dict:
    return {
        "status": "on",
        "nextOccurrenceUTC": now.isoformat(),
        "deliveryChannel": ["plushie"],
        "schedule": {
            "repeat": repeat,
            "timeLocal": "14:00",
            "dateLocal": "2026-06-01",
            "days": ["2026-06-01"],
        },
        "targets": [{"deviceId": "aa:bb:cc:dd:ee:ff", "mode": "reminder"}],
    }


def test_fetch_due_reminders_queries_reminders_collection_group(monkeypatch):
    now = datetime.now(timezone.utc)
    docs = [_FakeDoc("users/+1/reminders/r-1", _reminder_data(now))]
    client = _FakeCollectionGroupClient(docs, expected_group="reminders")

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    # This would raise AssertionError if it queries "alarms" instead of "reminders"
    results = firestore_client.fetch_due_reminders(
        now, lookahead=timedelta(minutes=1), client=client
    )
    assert len(results) == 1


def test_fetch_due_reminders_does_not_query_alarms_collection(monkeypatch):
    """Explicitly assert that fetch_due_reminders never touches the alarms collection."""
    now = datetime.now(timezone.utc)
    alarms_queried = []

    class _StrictClient:
        def collection_group(self, name):
            if name == "alarms":
                alarms_queried.append(name)
            return _FakeQuery([])

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    firestore_client.fetch_due_reminders(now, lookahead=timedelta(minutes=1), client=_StrictClient())
    assert alarms_queried == [], "fetch_due_reminders must never query the alarms collection"


def test_fetch_due_reminders_skips_docs_without_targets(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {**_reminder_data(now)}
    del data["targets"]
    docs = [_FakeDoc("users/+1/reminders/r-1", data)]
    client = _FakeCollectionGroupClient(docs)

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_reminders(now, lookahead=timedelta(minutes=1), client=client)
    assert results == []


def test_fetch_due_reminders_skips_docs_with_invalid_schedule(monkeypatch):
    now = datetime.now(timezone.utc)
    data = {**_reminder_data(now), "schedule": {"repeat": "bogus", "timeLocal": "14:00", "days": []}}
    docs = [_FakeDoc("users/+1/reminders/r-1", data)]
    client = _FakeCollectionGroupClient(docs)

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_reminders(now, lookahead=timedelta(minutes=1), client=client)
    assert results == []


def test_fetch_due_reminders_returns_alarm_doc_with_correct_fields(monkeypatch):
    now = datetime.now(timezone.utc)
    docs = [_FakeDoc("users/+15551234567/reminders/r-abc", _reminder_data(now))]
    client = _FakeCollectionGroupClient(docs)

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_reminders(now, lookahead=timedelta(minutes=1), client=client)

    assert len(results) == 1
    doc = results[0]
    assert doc.alarm_id == "r-abc"
    assert doc.user_id == "+15551234567"
    assert doc.targets[0].mode == "reminder"
    assert doc.targets[0].device_id is not None
    assert "reminders" in doc.doc_path


def test_fetch_due_reminders_preserves_doc_path_for_mark_complete(monkeypatch):
    """doc_path must use 'reminders/' so mark_one_time_alarm_complete writes back correctly."""
    now = datetime.now(timezone.utc)
    docs = [_FakeDoc("users/+1/reminders/r-xyz", _reminder_data(now))]
    client = _FakeCollectionGroupClient(docs)

    monkeypatch.setattr(firestore_client, "FieldFilter", lambda *a, **kw: a)
    monkeypatch.setattr(firestore_client, "_get_user_metadata", lambda doc, cache: {})

    results = firestore_client.fetch_due_reminders(now, lookahead=timedelta(minutes=1), client=client)

    assert results[0].doc_path == "users/+1/reminders/r-xyz"
