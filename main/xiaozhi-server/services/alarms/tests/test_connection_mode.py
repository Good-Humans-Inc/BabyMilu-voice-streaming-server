from __future__ import annotations

from datetime import datetime, timezone

from core.connection import ConnectionHandler, ModeRuntimeState, FollowupState
from services.session_context import models as session_models


class _DummyLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def _build_conn(mode_config: dict, monkeypatch) -> ConnectionHandler:
    monkeypatch.setattr("core.connection.MODE_CONFIG", {"morning_alarm": mode_config})
    conn = ConnectionHandler.__new__(ConnectionHandler)
    conn.config = {"mode_config": {"morning_alarm": mode_config}}
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
            "mode": "morning_alarm",
        },
    )
    return conn


def test_apply_mode_session_settings_loads_config(monkeypatch):
    conn = _build_conn(
        {
            "instructions": "WAKE UP",
            "server_initiate_chat": True,
            "followup_enabled": True,
            "followup_delay": 5,
            "followup_max": 2,
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode == "morning_alarm"
    assert conn.mode_specific_instructions == "WAKE UP"
    assert conn.server_initiate_chat is True
    assert conn.followup_enabled is True
    assert conn.followup_delay == 5
    assert conn.followup_max == 2


def test_apply_mode_session_settings_without_mode_noops(monkeypatch):
    conn = _build_conn({"instructions": "unused"}, monkeypatch)
    conn.mode_session.session_config = {}

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode is None
    assert conn.mode_specific_instructions == ""


def test_apply_mode_session_settings_injects_reminder_context(monkeypatch):
    conn = _build_conn(
        {
            "instructions": "WAKE UP",
            "server_initiate_chat": True,
            "followup_enabled": True,
        },
        monkeypatch,
    )
    conn.mode_session.session_config = {
        "mode": "morning_alarm",
        "context": "water plants",
    }

    ConnectionHandler._apply_mode_session_settings(conn)

    assert "WAKE UP" in conn.mode_specific_instructions
    assert 'The user asked to be reminded about: "water plants".' in conn.mode_specific_instructions
    assert "Mention this reason explicitly in your very first sentence." in conn.mode_specific_instructions


# ---------------------------------------------------------------------------
# scheduled_conversation mode — dynamic instruction assembly
# ---------------------------------------------------------------------------

_SCHEDULED_CONV_MODE_CONFIG = {
    "morning_alarm": {"instructions": "LEGACY"},
    "scheduled_conversation": {
        "server_initiate_chat": True,
        "followup_enabled": True,
        "followup_delay": 10,
        "followup_max": 1,
        "use_separate_conversation": True,
    },
}


def _build_scheduled_conn(session_config: dict, monkeypatch) -> ConnectionHandler:
    monkeypatch.setattr("core.connection.MODE_CONFIG", _SCHEDULED_CONV_MODE_CONFIG)
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
        session_config=session_config,
    )
    return conn


def test_apply_mode_session_settings_assembles_scheduled_conversation_instructions(monkeypatch):
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "characterReminder": "Bakugou checking on exhausted user — push without being harsh",
            "emotionalContext": "User was anxious about exam, hasn't slept well",
            "deliveryPreference": "be direct but warm",
            "typeHint": "emotional",
            "priority": "high",
            "conversationOutline": "1. Open warm. 2. Ask how they're feeling. 3. Done when user says they're okay.",
            "completionSignal": "Done: user confirms okay. Snoozed: asks for later. Resisting: user deflects.",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    instr = conn.mode_specific_instructions
    assert conn.active_mode == "scheduled_conversation"
    assert conn.server_initiate_chat is True
    assert conn.followup_enabled is True
    assert "[CHARACTER REMINDER]" in instr
    assert "Bakugou checking on exhausted user" in instr
    assert "[CONTEXT FOR THIS CONVERSATION]" in instr
    assert "Type: emotional | Priority: high" in instr
    assert "be direct but warm" in instr
    assert "[CONVERSATION OUTLINE]" in instr
    assert "Open warm" in instr
    assert "[COMPLETION SIGNAL]" in instr
    assert "user confirms okay" in instr


def test_apply_mode_session_settings_scheduled_conversation_omits_missing_fields(monkeypatch):
    """character_reminder and conversation_outline are absent — sections should be skipped."""
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "typeHint": "habit",
            "priority": "medium",
            "emotionalContext": "User is in a good mood",
            "completionSignal": "Done: user confirms task complete.",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    instr = conn.mode_specific_instructions
    assert "[CHARACTER REMINDER]" not in instr
    assert "[CONVERSATION OUTLINE]" not in instr
    assert "[CONTEXT FOR THIS CONVERSATION]" in instr
    assert "[COMPLETION SIGNAL]" in instr


def test_apply_mode_session_settings_morning_alarm_regression(monkeypatch):
    """Legacy morning_alarm path must be completely unaffected by the new branch."""
    conn = _build_conn(
        {
            "instructions": "WAKE UP LEGACY",
            "server_initiate_chat": True,
            "followup_enabled": True,
        },
        monkeypatch,
    )
    conn.mode_session.session_config = {
        "mode": "morning_alarm",
        "context": "take vitamins",
    }

    ConnectionHandler._apply_mode_session_settings(conn)

    instr = conn.mode_specific_instructions
    assert "[CHARACTER REMINDER]" not in instr
    assert "[CONVERSATION OUTLINE]" not in instr
    assert "WAKE UP LEGACY" in instr
    assert 'The user asked to be reminded about: "take vitamins".' in instr

