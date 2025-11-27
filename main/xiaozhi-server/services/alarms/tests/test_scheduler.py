from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.alarms import models, scheduler
from services.session_context import models as session_models


class _FakeSessionStore:
    def __init__(self):
        self.sessions = {}
        self.created = []

    def get_session(self, device_id: str, now: datetime | None = None):
        return self.sessions.get(device_id)

    def create_session(
        self,
        *,
        device_id: str,
        session_type: str,
        ttl: timedelta,
        triggered_at: datetime,
        session_config: dict,
    ):
        session = session_models.ModeSession(
            device_id=device_id,
            session_type=session_type,
            triggered_at=triggered_at,
            ttl_seconds=int(ttl.total_seconds()),
            session_config=session_config,
        )
        self.sessions[device_id] = session
        self.created.append((device_id, session_config))
        return session


def _make_alarm(device_id: str, mode: str) -> models.AlarmDoc:
    schedule = models.AlarmSchedule(
        repeat=models.AlarmRepeat.WEEKLY,
        time_local="07:00",
        days=["Mon"],
    )
    target = models.AlarmTarget(device_id=device_id, mode=mode)
    return models.AlarmDoc(
        alarm_id="alarm-123",
        user_id="user-xyz",
        uid="user-xyz",
        label="Morning Wake",
        schedule=schedule,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=datetime.now(timezone.utc),
        targets=[target],
        updated_at=None,
        raw={},
    )


def test_prepare_wake_requests_creates_session(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    def fake_fetch(now, lookahead):
        return [_make_alarm("DEV123", "morning_alarm")]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)

    now = datetime.now(timezone.utc)
    wake_requests = scheduler.prepare_wake_requests(now, lookahead=timedelta(minutes=1))

    assert len(wake_requests) == 1
    request = wake_requests[0]
    assert request.target.device_id == "DEV123"
    assert request.session is fake_store.sessions["DEV123"]
    assert fake_store.sessions["DEV123"].session_config == {
        "mode": "morning_alarm",
        "alarmId": "alarm-123",
        "userId": "user-xyz",
        "label": "Morning Wake",
    }


def test_prepare_wake_requests_skips_existing_session(monkeypatch):
    fake_store = _FakeSessionStore()
    existing_session = session_models.ModeSession(
        device_id="DEV123",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={"mode": "morning_alarm"},
    )
    fake_store.sessions["DEV123"] = existing_session
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    def fake_fetch(now, lookahead):
        return [_make_alarm("DEV123", "morning_alarm")]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert wake_requests == []
    assert fake_store.created == []

