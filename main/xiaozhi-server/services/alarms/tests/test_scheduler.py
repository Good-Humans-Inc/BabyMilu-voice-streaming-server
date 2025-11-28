from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


def _make_alarm(
    device_id: str,
    mode: str,
    *,
    days: list[str] | None = None,
    next_occurrence: datetime | None = None,
    last_processed: datetime | None = None,
    time_local: str = "07:00",
    timezone_name: str | None = "UTC",
) -> models.AlarmDoc:
    schedule = models.AlarmSchedule(
        repeat=models.AlarmRepeat.WEEKLY,
        time_local=time_local,
        days=days or ["Mon"],
    )
    target = models.AlarmTarget(device_id=device_id, mode=mode)
    raw_payload = {}
    if timezone_name:
        raw_payload["timezone"] = timezone_name
    return models.AlarmDoc(
        alarm_id="alarm-123",
        user_id="user-xyz",
        uid="user-xyz",
        label="Morning Wake",
        schedule=schedule,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=next_occurrence or datetime.now(timezone.utc),
        targets=[target],
        updated_at=None,
        raw=raw_payload,
        doc_path="users/user-xyz/alarms/alarm-123",
        last_processed_utc=last_processed,
    )


def test_prepare_wake_requests_creates_session(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    next_occurrence = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)

    def fake_fetch(now, lookahead):
        return [
            _make_alarm(
                "DEV123",
                "morning_alarm",
                days=["Tue", "Thu"],
                next_occurrence=next_occurrence,
            )
        ]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)
    recorded = {}

    def fake_mark(alarm, *, last_processed, next_occurrence):
        recorded["last_processed"] = last_processed
        recorded["next_occurrence"] = next_occurrence

    monkeypatch.setattr(
        scheduler.firestore_client, "mark_alarm_processed", fake_mark
    )

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
    assert recorded["last_processed"] == next_occurrence
    assert recorded["next_occurrence"] > next_occurrence


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
    called = False

    def fake_mark(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        scheduler.firestore_client, "mark_alarm_processed", fake_mark
    )

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert wake_requests == []
    assert fake_store.created == []
    assert called is False


def test_prepare_wake_requests_skips_when_last_processed_matches(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    reference = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)

    def fake_fetch(now, lookahead):
        return [
            _make_alarm(
                "DEV123",
                "morning_alarm",
                next_occurrence=reference,
                last_processed=reference,
            )
        ]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)
    called = False

    def fake_mark(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        scheduler.firestore_client, "mark_alarm_processed", fake_mark
    )

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert wake_requests == []
    assert called is False


def test_compute_next_occurrence_uses_schedule_days():
    reference = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)  # Monday
    alarm = _make_alarm(
        "DEV1",
        "mode",
        days=["Wed"],
        next_occurrence=reference,
    )

    next_dt = scheduler.compute_next_occurrence(alarm, now=reference)

    assert next_dt == reference + timedelta(days=2)


def test_compute_next_occurrence_uses_timezone_and_local_time():
    reference = datetime(2024, 1, 1, 15, 30, tzinfo=timezone.utc)  # 07:30 PST Monday
    alarm = _make_alarm(
        "DEV1",
        "mode",
        days=["Mon", "Tue", "Wed"],
        next_occurrence=reference,
        time_local="07:30",
        timezone_name="America/Los_Angeles",
    )

    next_dt = scheduler.compute_next_occurrence(alarm, now=reference)

    assert next_dt == datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)


def test_compute_next_occurrence_requires_timezone():
    reference = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)
    alarm = _make_alarm(
        "DEV1",
        "mode",
        timezone_name=None,
        next_occurrence=reference,
    )

    with pytest.raises(ValueError):
        scheduler.compute_next_occurrence(alarm, now=reference)
