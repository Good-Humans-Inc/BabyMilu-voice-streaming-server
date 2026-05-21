"""Tests for V1.3 outcome tracking across all four outcome paths."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from services.session_context import models as session_models
from services.session_context import store as session_store


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_delivery_session(
    alarm_id: str = "alarm-123",
    uid: str = "user-456",
    has_user_response: bool = False,
    expired: bool = False,
) -> session_models.ModeSession:
    triggered_at = datetime.now(timezone.utc) - timedelta(minutes=10 if expired else 0)
    ttl = 60 if expired else 300
    return session_models.ModeSession(
        device_id="DEV1",
        session_type="alarm",
        triggered_at=triggered_at,
        ttl_seconds=ttl,
        session_config={
            "mode": "scheduled_conversation",
            "alarmId": alarm_id,
            "userId": uid,
        },
        has_user_response=has_user_response,
    )


def _make_conn(alarm_id: str = "alarm-123", uid: str = "user-456") -> SimpleNamespace:
    return SimpleNamespace(
        device_id="DEV1",
        mode_session=_make_delivery_session(alarm_id=alarm_id, uid=uid),
    )


# ─────────────────────────────────────────────────────────────────────────────
# write_alarm_outcome
# ─────────────────────────────────────────────────────────────────────────────

def test_write_alarm_outcome_valid_outcomes():
    from services.alarms.firestore_client import write_alarm_outcome, _VALID_OUTCOMES
    mock_client = MagicMock()
    doc_ref = MagicMock()
    mock_client.collection.return_value.document.return_value.collection.return_value.document.return_value = doc_ref
    for outcome in _VALID_OUTCOMES:
        write_alarm_outcome("uid1", "alarm1", outcome, client=mock_client)
        doc_ref.update.assert_called()
        patch_arg = doc_ref.update.call_args[0][0]
        assert patch_arg["lastOutcome"] == outcome
        assert "lastOutcomeAt" in patch_arg


def test_write_alarm_outcome_rejects_invalid():
    from services.alarms.firestore_client import write_alarm_outcome
    mock_client = MagicMock()
    with pytest.raises(ValueError, match="Invalid outcome"):
        write_alarm_outcome("uid1", "alarm1", "finished", client=mock_client)


def test_write_alarm_outcome_timestamp_used_when_provided():
    from services.alarms.firestore_client import write_alarm_outcome
    mock_client = MagicMock()
    doc_ref = MagicMock()
    mock_client.collection.return_value.document.return_value.collection.return_value.document.return_value = doc_ref
    fixed = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    write_alarm_outcome("uid1", "alarm1", "done", now=fixed, client=mock_client)
    patch_arg = doc_ref.update.call_args[0][0]
    assert "2026-04-01" in patch_arg["lastOutcomeAt"]


# ─────────────────────────────────────────────────────────────────────────────
# Outcome: done / resisting  (complete_reminder tool)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("outcome", ["done", "resisting"])
def test_complete_reminder_writes_outcome(outcome):
    from plugins_func.functions.complete_reminder import complete_reminder
    conn = _make_conn()
    with patch("plugins_func.functions.complete_reminder.write_alarm_outcome") as mock_write:
        result = complete_reminder(conn, outcome=outcome)
    mock_write.assert_called_once_with("user-456", "alarm-123", outcome)
    assert result.action.name in ("REQLLM", "RESPONSE")


def test_complete_reminder_noop_outside_delivery_session():
    from plugins_func.functions.complete_reminder import complete_reminder
    conn = SimpleNamespace(
        device_id="DEV1",
        mode_session=session_models.ModeSession(
            device_id="DEV1",
            session_type="alarm",
            triggered_at=datetime.now(timezone.utc),
            ttl_seconds=300,
            session_config={"mode": "morning_alarm"},
        ),
    )
    with patch("plugins_func.functions.complete_reminder.write_alarm_outcome") as mock_write:
        complete_reminder(conn, outcome="done")
    mock_write.assert_not_called()


def test_complete_reminder_noop_when_no_mode_session():
    from plugins_func.functions.complete_reminder import complete_reminder
    conn = SimpleNamespace(device_id="DEV1", mode_session=None)
    with patch("plugins_func.functions.complete_reminder.write_alarm_outcome") as mock_write:
        complete_reminder(conn, outcome="done")
    mock_write.assert_not_called()


def test_complete_reminder_rejects_invalid_outcome():
    from plugins_func.functions.complete_reminder import complete_reminder
    conn = _make_conn()
    with patch("plugins_func.functions.complete_reminder.write_alarm_outcome") as mock_write:
        result = complete_reminder(conn, outcome="snoozed")
    mock_write.assert_not_called()


def test_complete_reminder_resisting_then_done_writes_done():
    """If user resisted but ultimately complied, caller passes 'done' — tool just forwards it."""
    from plugins_func.functions.complete_reminder import complete_reminder
    conn = _make_conn()
    with patch("plugins_func.functions.complete_reminder.write_alarm_outcome") as mock_write:
        complete_reminder(conn, outcome="done")
    mock_write.assert_called_once_with("user-456", "alarm-123", "done")


# ─────────────────────────────────────────────────────────────────────────────
# Outcome: snoozed  (schedule_conversation side effect)
# ─────────────────────────────────────────────────────────────────────────────

def test_schedule_conversation_writes_snoozed_during_delivery():
    from plugins_func.functions.schedule_conversation import _maybe_write_snoozed_outcome
    conn = _make_conn()
    with patch("plugins_func.functions.schedule_conversation.write_alarm_outcome") as mock_write:
        _maybe_write_snoozed_outcome(conn)
    mock_write.assert_called_once_with("user-456", "alarm-123", "snoozed")


def test_schedule_conversation_no_snoozed_outside_delivery():
    from plugins_func.functions.schedule_conversation import _maybe_write_snoozed_outcome
    conn = SimpleNamespace(
        device_id="DEV1",
        mode_session=session_models.ModeSession(
            device_id="DEV1",
            session_type="alarm",
            triggered_at=datetime.now(timezone.utc),
            ttl_seconds=300,
            session_config={"mode": "morning_alarm"},
        ),
    )
    with patch("plugins_func.functions.schedule_conversation.write_alarm_outcome") as mock_write:
        _maybe_write_snoozed_outcome(conn)
    mock_write.assert_not_called()


def test_schedule_conversation_no_snoozed_when_no_mode_session():
    from plugins_func.functions.schedule_conversation import _maybe_write_snoozed_outcome
    conn = SimpleNamespace(device_id="DEV1", mode_session=None)
    with patch("plugins_func.functions.schedule_conversation.write_alarm_outcome") as mock_write:
        _maybe_write_snoozed_outcome(conn)
    mock_write.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Outcome: ignored  (store expiry callback)
# ─────────────────────────────────────────────────────────────────────────────

def test_expiry_callback_writes_ignored_on_no_response():
    from services.alarms.scheduler import _on_session_expire
    expired_session = _make_delivery_session(expired=True, has_user_response=False)
    with patch("services.alarms.scheduler.firestore_client.write_alarm_outcome") as mock_write:
        _on_session_expire(expired_session)
    mock_write.assert_called_once_with("user-456", "alarm-123", "ignored")


def test_expiry_callback_skips_non_scheduled_conversation():
    from services.alarms.scheduler import _on_session_expire
    session = session_models.ModeSession(
        device_id="DEV1",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=60,
        session_config={"mode": "morning_alarm", "alarmId": "alarm-123", "userId": "user-456"},
        has_user_response=False,
    )
    with patch("services.alarms.scheduler.firestore_client.write_alarm_outcome") as mock_write:
        _on_session_expire(session)
    mock_write.assert_not_called()


def test_expiry_callback_skips_missing_alarm_id():
    from services.alarms.scheduler import _on_session_expire
    session = session_models.ModeSession(
        device_id="DEV1",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=60,
        session_config={"mode": "scheduled_conversation", "userId": "user-456"},
        has_user_response=False,
    )
    with patch("services.alarms.scheduler.firestore_client.write_alarm_outcome") as mock_write:
        _on_session_expire(session)
    mock_write.assert_not_called()


def test_store_fires_expiry_callback_when_session_expires_with_no_response():
    """get_session detects expiry → fires callback → deletes session."""
    expired_session = _make_delivery_session(expired=True, has_user_response=False)
    mock_collection = MagicMock()
    mock_doc_ref = MagicMock()
    mock_collection.document.return_value = mock_doc_ref
    mock_doc_ref.get.return_value.exists = True
    mock_doc_ref.get.return_value.to_dict.return_value = {
        "sessionType": "alarm",
        "triggeredAt": expired_session.triggered_at,
        "ttlSeconds": expired_session.ttl_seconds,
        "expiresAt": expired_session.expires_at,
        "sessionConfig": expired_session.session_config,
        "hasUserResponse": False,
        "isSnoozeFollowUp": False,
        "conversation": {},
    }

    callback_calls = []
    def fake_callback(s):
        callback_calls.append(s)

    store = session_store.SessionContextStore()
    store._firestore_client = MagicMock()
    store._firestore_client.collection.return_value = mock_collection

    old_callback = session_store._on_session_expire
    session_store.set_expiry_callback(fake_callback)
    try:
        result = store.get_session("DEV1", now=datetime.now(timezone.utc))
    finally:
        session_store.set_expiry_callback(old_callback)

    assert result is None
    assert len(callback_calls) == 1
    assert callback_calls[0].has_user_response is False
    mock_doc_ref.delete.assert_called_once()


def test_store_does_not_fire_callback_when_user_responded():
    """Expiry with has_user_response=True → callback NOT called."""
    expired_session = _make_delivery_session(expired=True, has_user_response=True)
    mock_collection = MagicMock()
    mock_doc_ref = MagicMock()
    mock_collection.document.return_value = mock_doc_ref
    mock_doc_ref.get.return_value.exists = True
    mock_doc_ref.get.return_value.to_dict.return_value = {
        "sessionType": "alarm",
        "triggeredAt": expired_session.triggered_at,
        "ttlSeconds": expired_session.ttl_seconds,
        "expiresAt": expired_session.expires_at,
        "sessionConfig": expired_session.session_config,
        "hasUserResponse": True,
        "isSnoozeFollowUp": False,
        "conversation": {},
    }

    callback_calls = []
    def fake_callback(s):
        callback_calls.append(s)

    store = session_store.SessionContextStore()
    store._firestore_client = MagicMock()
    store._firestore_client.collection.return_value = mock_collection

    old_callback = session_store._on_session_expire
    session_store.set_expiry_callback(fake_callback)
    try:
        store.get_session("DEV1", now=datetime.now(timezone.utc))
    finally:
        session_store.set_expiry_callback(old_callback)

    assert callback_calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Outcome instruction present in delivery system prompt
# ─────────────────────────────────────────────────────────────────────────────

def test_delivery_instructions_include_outcome_instruction(monkeypatch):
    from core.connection import ConnectionHandler
    from services.session_context import models as session_models
    from core.connection import ModeRuntimeState, FollowupState

    class _DummyLogger:
        def bind(self, **kwargs): return self
        def info(self, *a, **kw): pass
        def warning(self, *a, **kw): pass

    monkeypatch.setattr("core.connection.MODE_CONFIG", {
        "scheduled_conversation": {
            "server_initiate_chat": True,
            "followup_enabled": True,
            "followup_delay": 10,
            "followup_max": 1,
        }
    })

    conn = ConnectionHandler.__new__(ConnectionHandler)
    conn.config = {}
    conn.device_id = "DEV1"
    conn.logger = _DummyLogger()
    conn._mode_state = ModeRuntimeState()
    conn._followup_state = FollowupState()
    conn.mode_session = session_models.ModeSession(
        device_id="DEV1",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={
            "mode": "scheduled_conversation",
            "alarmId": "alarm-99",
            "typeHint": "habit",
            "priority": "medium",
            "completionSignal": "Done: user confirmed. Snoozed: wants later.",
        },
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert "[OUTCOME INSTRUCTION]" in conn.mode_specific_instructions
    assert "complete_reminder" in conn.mode_specific_instructions
