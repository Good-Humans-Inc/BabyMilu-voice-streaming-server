from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
    assert "Your very first spoken sentence must already contain the reminder reason." in conn.mode_specific_instructions


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


def test_apply_mode_session_settings_scheduled_conversation_completion_signal_still_omitted(monkeypatch):
    """completionSignal has no fallback — its section is still omitted when absent."""
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "typeHint": "habit",
            "priority": "medium",
            "emotionalContext": "User is in a good mood",
            # completionSignal intentionally absent
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    instr = conn.mode_specific_instructions
    assert "[CONTEXT FOR THIS CONVERSATION]" in instr
    assert "[COMPLETION SIGNAL]" not in instr
    # Fallback sections ARE now present even without explicit values
    assert "[CHARACTER REMINDER]" in instr
    assert "[CONVERSATION OUTLINE]" in instr


# ---------------------------------------------------------------------------
# V1.1 — followup_max driven by priority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("priority,expected_max", [
    ("critical", 3),
    ("high",     2),
    ("medium",   1),
    ("low",      0),
])
def test_scheduled_conversation_followup_max_driven_by_priority(monkeypatch, priority, expected_max):
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "priority": priority,
            "typeHint": "habit",
            "emotionalContext": "test",
            "completionSignal": "Done: confirmed.",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == expected_max


def test_scheduled_conversation_missing_priority_defaults_to_medium(monkeypatch):
    """No priority field → treated as 'medium' → followup_max == 1."""
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            # priority intentionally absent
            "typeHint": "habit",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == 1


def test_scheduled_conversation_unknown_priority_leaves_config_default(monkeypatch):
    """Unrecognized priority value → no override → MODE_CONFIG default of 1."""
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "priority": "urgent",  # not in PRIORITY_FOLLOWUP_MAX
            "typeHint": "habit",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == 1  # MODE_CONFIG["scheduled_conversation"]["followup_max"]


def test_morning_alarm_followup_max_unaffected_by_priority_logic(monkeypatch):
    """morning_alarm mode must not be affected by the priority override path."""
    conn = _build_conn(
        {
            "instructions": "WAKE UP",
            "server_initiate_chat": True,
            "followup_enabled": True,
            "followup_max": 1,
        },
        monkeypatch,
    )
    conn.mode_session.session_config = {
        "mode": "morning_alarm",
        "priority": "critical",  # should be ignored for morning_alarm
    }

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == 1


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


def test_scheduled_conversation_instructions_include_snooze_section(monkeypatch):
    """Delivery instructions must include [SNOOZE INSTRUCTION] with embedded schedule_conversation guidance."""
    alarm_id = "alarm-snooze-test-99"
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "alarmId": alarm_id,
            "typeHint": "habit",
            "priority": "medium",
            "emotionalContext": "user is tired",
            "completionSignal": "Done: confirmed. Snoozed: asks for later.",
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    instr = conn.mode_specific_instructions
    assert "[SNOOZE INSTRUCTION]" in instr
    assert "schedule_conversation" in instr


def test_morning_alarm_instructions_do_not_include_snooze_section(monkeypatch):
    """morning_alarm delivery must NOT inject the snooze instruction."""
    conn = _build_conn(
        {
            "instructions": "WAKE UP",
            "server_initiate_chat": True,
            "followup_enabled": True,
        },
        monkeypatch,
    )
    conn.mode_session.session_config = {"mode": "morning_alarm"}

    ConnectionHandler._apply_mode_session_settings(conn)

    assert "[SNOOZE INSTRUCTION]" not in conn.mode_specific_instructions


# ---------------------------------------------------------------------------
# Fallback instructions for app-created reminders (empty context fields)
# ---------------------------------------------------------------------------

def test_scheduled_conversation_fallback_character_reminder(monkeypatch):
    """characterReminder absent → fallback text appears in [CHARACTER REMINDER] block."""
    conn = _build_scheduled_conn(
        {"mode": "scheduled_conversation", "label": "Take vitamins"},
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    instr = conn.mode_specific_instructions
    assert "[CHARACTER REMINDER]" in instr
    assert "character setting" in instr


def test_scheduled_conversation_fallback_conversation_outline(monkeypatch):
    """conversationOutline absent → fallback text appears in [CONVERSATION OUTLINE] block."""
    conn = _build_scheduled_conn(
        {"mode": "scheduled_conversation", "label": "Morning run"},
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    instr = conn.mode_specific_instructions
    assert "[CONVERSATION OUTLINE]" in instr
    assert "reminder title" in instr


def test_scheduled_conversation_fallback_emotional_context_is_none(monkeypatch):
    """emotionalContext absent → shows 'none' in context block."""
    conn = _build_scheduled_conn(
        {"mode": "scheduled_conversation"},
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    assert "Emotional context: none" in conn.mode_specific_instructions


def test_scheduled_conversation_fallback_delivery_preference_is_none(monkeypatch):
    """deliveryPreference absent → shows 'none' (was 'none stated') in context block."""
    conn = _build_scheduled_conn(
        {"mode": "scheduled_conversation"},
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    assert "Delivery preference: none" in conn.mode_specific_instructions


def test_scheduled_conversation_explicit_fields_override_fallbacks(monkeypatch):
    """When all fields are provided, explicit values replace fallbacks throughout."""
    conn = _build_scheduled_conn(
        {
            "mode": "scheduled_conversation",
            "label": "Gym session",
            "characterReminder": "Custom character note",
            "emotionalContext": "User is pumped up",
            "deliveryPreference": "be energetic",
            "conversationOutline": "1. Fire them up. 2. Done when they leave.",
            "completionSignal": "Done: user heads to gym.",
        },
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    instr = conn.mode_specific_instructions
    assert "Custom character note" in instr
    assert "character setting" not in instr
    assert "User is pumped up" in instr
    assert "Emotional context: none" not in instr
    assert "be energetic" in instr
    assert "Delivery preference: none" not in instr
    assert "Fire them up" in instr
    assert "reminder title" not in instr
    assert "Done: user heads to gym." in instr


def test_scheduled_conversation_label_appears_in_context_block(monkeypatch):
    """label is always written into the context block regardless of other fields."""
    conn = _build_scheduled_conn(
        {"mode": "scheduled_conversation", "label": "Evening meditation"},
        monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)
    assert 'Reminder title (as titled by the user): "Evening meditation"' in conn.mode_specific_instructions

