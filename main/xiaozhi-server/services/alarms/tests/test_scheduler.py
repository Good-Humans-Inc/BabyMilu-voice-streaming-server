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

    def delete_session(self, device_id: str):
        self.sessions.pop(device_id, None)


def _make_alarm(
    device_id: str,
    mode: str,
    *,
    repeat: models.AlarmRepeat = models.AlarmRepeat.WEEKLY,
    days: list[str] | None = None,
    next_occurrence: datetime | None = None,
    last_processed: datetime | None = None,
    time_local: str = "07:00",
    timezone_name: str | None = "UTC",
) -> models.AlarmDoc:
    schedule = models.AlarmSchedule(
        repeat=repeat,
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

    now = datetime.now(timezone.utc)
    wake_requests = scheduler.prepare_wake_requests(now, lookahead=timedelta(minutes=1))

    assert len(wake_requests) == 1
    request = wake_requests[0]
    assert request.target.device_id == "DEV123"
    assert request.session is fake_store.sessions["DEV123"]
    cfg = fake_store.sessions["DEV123"].session_config
    assert cfg["mode"] == "morning_alarm"
    assert cfg["alarmId"] == "alarm-123"
    assert cfg["userId"] == "user-xyz"
    assert cfg["label"] == "Morning Wake"
    assert cfg["context"] is None
    # V0 fields are None for morning_alarm docs that don't have them
    assert cfg["content"] is None
    assert cfg["typeHint"] is None
    assert cfg["priority"] is None
    assert cfg["conversationOutline"] is None
    assert cfg["characterReminder"] is None
    assert cfg["emotionalContext"] is None
    assert cfg["completionSignal"] is None
    assert cfg["deliveryPreference"] is None


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
    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert wake_requests == []


def test_finalize_wake_request_marks_one_time_alarm_complete(monkeypatch):
    next_occurrence = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)
    alarm = _make_alarm(
        "DEV123",
        "morning_alarm",
        repeat=models.AlarmRepeat.NONE,
        days=["2024-01-01"],
        next_occurrence=next_occurrence,
    )
    target = models.AlarmTarget(device_id="DEV123", mode="morning_alarm")
    fake_session = session_models.ModeSession(
        device_id="DEV123",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={"mode": "morning_alarm"},
    )
    wake_request = scheduler.tasks.WakeRequest(alarm=alarm, target=target, session=fake_session)
    completed = {}
    processed_called = False

    def fake_complete(alarm, *, last_processed):
        completed["alarm_id"] = alarm.alarm_id
        completed["last_processed"] = last_processed

    def fake_mark(*args, **kwargs):
        nonlocal processed_called
        processed_called = True

    monkeypatch.setattr(
        scheduler.firestore_client, "mark_one_time_alarm_complete", fake_complete
    )
    monkeypatch.setattr(
        scheduler.firestore_client, "mark_alarm_processed", fake_mark
    )

    scheduler.finalize_wake_request(wake_request, now=datetime.now(timezone.utc))
    assert completed["alarm_id"] == "alarm-123"
    assert completed["last_processed"] == next_occurrence
    assert processed_called is False


def test_rollback_wake_request_deletes_session(monkeypatch):
    fake_store = _FakeSessionStore()
    fake_store.sessions["DEV123"] = session_models.ModeSession(
        device_id="DEV123",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={"mode": "morning_alarm"},
    )
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)
    wake_request = scheduler.tasks.WakeRequest(
        alarm=_make_alarm("DEV123", "morning_alarm"),
        target=models.AlarmTarget(device_id="DEV123", mode="morning_alarm"),
        session=fake_store.sessions["DEV123"],
    )

    scheduler.rollback_wake_request(wake_request)

    assert "DEV123" not in fake_store.sessions


def test_prepare_wake_requests_one_time_uses_short_session_ttl(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    def fake_fetch(now, lookahead):
        return [
            _make_alarm(
                "DEV123",
                "morning_alarm",
                repeat=models.AlarmRepeat.NONE,
                days=["2024-01-01"],
            )
        ]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert len(wake_requests) == 1
    assert wake_requests[0].session.ttl_seconds == int(
        scheduler.ONE_TIME_SESSION_TTL.total_seconds()
    )


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


def test_compute_next_occurrence_daily_all_days_advances_one_day():
    """Daily alarm (all 7 days) — next occurrence is exactly 24 hours later."""
    reference = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)  # Monday 07:00 UTC
    alarm = _make_alarm(
        "DEV1",
        "mode",
        days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        next_occurrence=reference,
        time_local="07:00",
    )

    next_dt = scheduler.compute_next_occurrence(alarm, now=reference)

    assert next_dt == reference + timedelta(days=1)


def test_compute_next_occurrence_daily_empty_days_defaults_to_all():
    """Empty days list is treated as all 7 days — advances one day."""
    reference = datetime(2024, 1, 1, 7, tzinfo=timezone.utc)  # Monday 07:00 UTC
    alarm = _make_alarm(
        "DEV1",
        "mode",
        days=[],
        next_occurrence=reference,
        time_local="07:00",
    )

    next_dt = scheduler.compute_next_occurrence(alarm, now=reference)

    assert next_dt == reference + timedelta(days=1)


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
