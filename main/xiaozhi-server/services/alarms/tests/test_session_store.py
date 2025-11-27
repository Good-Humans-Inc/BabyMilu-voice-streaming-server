from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.session_context import store as session_store


class _FakeDocSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return self._data


class _FakeDocument:
    def __init__(self, storage: dict, key: str):
        self.storage = storage
        self.key = key

    def set(self, data: dict, merge: bool = True):
        self.storage[self.key] = data

    def get(self):
        return _FakeDocSnapshot(self.storage.get(self.key))

    def delete(self):
        self.storage.pop(self.key, None)


class _FakeCollection:
    def __init__(self):
        self.storage = {}

    def document(self, key: str):
        return _FakeDocument(self.storage, key)


def test_create_session_persists_payload(monkeypatch):
    fake_collection = _FakeCollection()
    store = session_store.SessionContextStore()
    monkeypatch.setattr(store, "_collection", lambda: fake_collection)

    triggered = datetime(2024, 1, 1, tzinfo=timezone.utc)
    session = store.create_session(
        device_id="DEV123",
        session_type="alarm",
        ttl=timedelta(minutes=5),
        triggered_at=triggered,
        session_config={"mode": "morning_alarm"},
    )

    stored = fake_collection.storage["DEV123"]
    assert stored["sessionType"] == "alarm"
    assert stored["ttlSeconds"] == 300
    assert stored["sessionConfig"]["mode"] == "morning_alarm"
    assert session.session_config["mode"] == "morning_alarm"


def test_get_session_removes_expired(monkeypatch):
    fake_collection = _FakeCollection()
    store = session_store.SessionContextStore()
    monkeypatch.setattr(store, "_collection", lambda: fake_collection)

    expired_session = {
        "sessionType": "alarm",
        "triggeredAt": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "ttlSeconds": 60,
        "expiresAt": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "sessionConfig": {"mode": "morning_alarm"},
    }
    fake_collection.storage["DEV999"] = expired_session

    result = store.get_session("DEV999", now=datetime(2024, 1, 2, tzinfo=timezone.utc))
    assert result is None
    assert "DEV999" not in fake_collection.storage

