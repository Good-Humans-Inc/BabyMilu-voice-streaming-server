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

