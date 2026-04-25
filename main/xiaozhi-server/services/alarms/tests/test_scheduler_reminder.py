"""Tests for scheduler behaviour when reminders are present.

Verifies:
- prepare_wake_requests merges alarms + reminders
- Reminders always get the short (1-min) session TTL regardless of repeat
- Reminder session_config contains mode: "reminder"
- Alarms and reminders don't interfere with each other
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.alarms import models, scheduler
from services.session_context import models as session_models


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_scheduler.py helpers)
# ---------------------------------------------------------------------------

class _FakeSessionStore:
    def __init__(self):
        self.sessions: dict = {}
        self.created: list = []

    def get_session(self, device_id: str, now: datetime | None = None):
        return self.sessions.get(device_id)

    def create_session(
        self, *, device_id, session_type, ttl, triggered_at, session_config
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


def _make_alarm_doc(
    device_id: str,
    mode: str,
    *,
    repeat: models.AlarmRepeat = models.AlarmRepeat.NONE,
    alarm_id: str = "alarm-001",
    doc_path_collection: str = "alarms",
    next_occurrence: datetime | None = None,
    last_processed: datetime | None = None,
    label: str = "Test",
    context: str | None = None,
) -> models.AlarmDoc:
    schedule = models.AlarmSchedule(
        repeat=repeat,
        time_local="14:00",
        days=["2026-06-01"] if repeat == models.AlarmRepeat.NONE else ["Mon"],
    )
    target = models.AlarmTarget(device_id=device_id, mode=mode)
    return models.AlarmDoc(
        alarm_id=alarm_id,
        user_id="+1",
        uid="+1",
        label=label,
        context=context,
        schedule=schedule,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=next_occurrence or datetime.now(timezone.utc),
        targets=[target],
        updated_at=None,
        raw={"timezone": "UTC"},
        doc_path=f"users/+1/{doc_path_collection}/{alarm_id}",
        last_processed_utc=last_processed,
    )


# ---------------------------------------------------------------------------
# Reminders use the short (1-min) session TTL
# ---------------------------------------------------------------------------

def test_reminder_wake_request_uses_short_session_ttl(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    reminder = _make_alarm_doc(
        "DEV-R1", "reminder",
        repeat=models.AlarmRepeat.NONE,
        alarm_id="reminder-001",
        doc_path_collection="reminders",
    )

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", lambda now, lookahead: [])
    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_reminders", lambda now, lookahead: [reminder])

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert len(wake_requests) == 1
    ttl = wake_requests[0].session.ttl_seconds
    assert ttl == int(scheduler.ONE_TIME_SESSION_TTL.total_seconds()), (
        f"Reminder should use ONE_TIME_SESSION_TTL ({scheduler.ONE_TIME_SESSION_TTL}) "
        f"but got {ttl}s"
    )


# ---------------------------------------------------------------------------
# Reminder session_config has mode: "reminder"
# ---------------------------------------------------------------------------

def test_reminder_session_config_has_reminder_mode(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    reminder = _make_alarm_doc(
        "DEV-R2", "reminder",
        alarm_id="reminder-002",
        doc_path_collection="reminders",
        label="drink water",
        context="drink water",
    )

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", lambda now, lookahead: [])
    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_reminders", lambda now, lookahead: [reminder])

    scheduler.prepare_wake_requests(datetime.now(timezone.utc), lookahead=timedelta(minutes=1))

    assert "DEV-R2" in fake_store.sessions
    cfg = fake_store.sessions["DEV-R2"].session_config
    assert cfg["mode"] == "reminder"
    assert cfg["label"] == "drink water"
    assert cfg["context"] == "drink water"


# ---------------------------------------------------------------------------
# Alarms and reminders are merged without interfering
# ---------------------------------------------------------------------------

def test_prepare_wake_requests_merges_alarms_and_reminders(monkeypatch):
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    alarm = _make_alarm_doc(
        "DEV-A1", "morning_alarm",
        repeat=models.AlarmRepeat.WEEKLY,
        alarm_id="alarm-001",
        doc_path_collection="alarms",
    )
    reminder = _make_alarm_doc(
        "DEV-R1", "reminder",
        repeat=models.AlarmRepeat.NONE,
        alarm_id="reminder-001",
        doc_path_collection="reminders",
    )

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", lambda now, lookahead: [alarm])
    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_reminders", lambda now, lookahead: [reminder])

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert len(wake_requests) == 2
    device_ids = {r.target.device_id for r in wake_requests}
    assert "DEV-A1" in device_ids
    assert "DEV-R1" in device_ids


def test_alarm_session_ttl_unaffected_by_reminders(monkeypatch):
    """Recurring alarm still gets SESSION_TTL (5 min), not the short reminder TTL."""
    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    alarm = _make_alarm_doc(
        "DEV-A2", "morning_alarm",
        repeat=models.AlarmRepeat.WEEKLY,
        alarm_id="alarm-002",
        doc_path_collection="alarms",
    )

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", lambda now, lookahead: [alarm])
    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_reminders", lambda now, lookahead: [])

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert len(wake_requests) == 1
    ttl = wake_requests[0].session.ttl_seconds
    assert ttl == int(scheduler.SESSION_TTL.total_seconds())


# ---------------------------------------------------------------------------
# Existing session on device blocks reminder (same dedup logic as alarms)
# ---------------------------------------------------------------------------

def test_prepare_wake_requests_skips_reminder_if_device_has_active_session(monkeypatch):
    fake_store = _FakeSessionStore()
    fake_store.sessions["DEV-R3"] = session_models.ModeSession(
        device_id="DEV-R3",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={"mode": "morning_alarm"},
    )
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    reminder = _make_alarm_doc("DEV-R3", "reminder", alarm_id="r-3", doc_path_collection="reminders")

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", lambda now, lookahead: [])
    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_reminders", lambda now, lookahead: [reminder])

    wake_requests = scheduler.prepare_wake_requests(
        datetime.now(timezone.utc), lookahead=timedelta(minutes=1)
    )

    assert wake_requests == []


# ---------------------------------------------------------------------------
# finalize_wake_request for a reminder marks it complete (status → off)
# ---------------------------------------------------------------------------

def test_finalize_wake_request_marks_reminder_complete(monkeypatch):
    """Reminder docs (repeat=NONE) must call mark_one_time_alarm_complete."""
    next_occurrence = datetime(2026, 6, 1, 14, tzinfo=timezone.utc)
    reminder = _make_alarm_doc(
        "DEV-R4", "reminder",
        repeat=models.AlarmRepeat.NONE,
        alarm_id="reminder-004",
        doc_path_collection="reminders",
        next_occurrence=next_occurrence,
    )
    target = models.AlarmTarget(device_id="DEV-R4", mode="reminder")
    fake_session = session_models.ModeSession(
        device_id="DEV-R4",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=60,
        session_config={"mode": "reminder"},
    )
    wake_request = scheduler.tasks.WakeRequest(alarm=reminder, target=target, session=fake_session)

    completed = {}
    mark_processed_called = False

    def fake_complete(alarm, *, last_processed):
        completed["alarm_id"] = alarm.alarm_id
        completed["last_processed"] = last_processed

    def fake_mark(*args, **kwargs):
        nonlocal mark_processed_called
        mark_processed_called = True

    monkeypatch.setattr(scheduler.firestore_client, "mark_one_time_alarm_complete", fake_complete)
    monkeypatch.setattr(scheduler.firestore_client, "mark_alarm_processed", fake_mark)

    scheduler.finalize_wake_request(wake_request, now=datetime.now(timezone.utc))

    assert completed["alarm_id"] == "reminder-004"
    assert completed["last_processed"] == next_occurrence
    assert mark_processed_called is False  # must NOT call recurring path
