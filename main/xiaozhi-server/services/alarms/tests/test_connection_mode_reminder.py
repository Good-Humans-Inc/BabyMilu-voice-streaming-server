"""Tests for how the 'reminder' mode is applied to a ConnectionHandler session.

Verifies:
- followup_enabled is False for reminder mode (only 1 voice message)
- server_initiate_chat is True (plushie speaks first)
- Context is injected into instructions ("reminded about X")
- morning_alarm mode is NOT affected by the reminder mode config
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.connection import ConnectionHandler, ModeRuntimeState, FollowupState
from services.session_context import models as session_models


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _DummyLogger:
    def bind(self, **kwargs): return self
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass


_REMINDER_MODE_CONFIG = {
    "instructions": (
        "You have one job: deliver the reminder stated in the context below "
        "as a natural spoken reminder. "
        "Your very first spoken sentence must already include the reminder reason. "
        "Do not begin with filler, small talk, or a standalone greeting before the reminder reason. "
        "Treat the reminder context as the meaning to convey, not as text to quote verbatim unless quoting is necessary. "
        "Keep it clear, warm, and concise. After delivering it, wish the user well and end naturally. "
        "Do not ask follow-up questions or continue the conversation."
    ),
    "server_initiate_chat": True,
    "followup_enabled": False,
    "use_separate_conversation": True,
}

_ALARM_MODE_CONFIG = {
    "instructions": "WAKE UP",
    "server_initiate_chat": True,
    "followup_enabled": True,
    "followup_delay": 10,
    "followup_max": 2,
    "use_separate_conversation": True,
}


def _build_conn_for_mode(
    mode: str,
    context: str | None,
    monkeypatch,
    *,
    title: str | None = None,
    label: str | None = None,
) -> ConnectionHandler:
    mode_config_map = {
        "morning_alarm": _ALARM_MODE_CONFIG,
        "reminder": _REMINDER_MODE_CONFIG,
    }
    monkeypatch.setattr("core.connection.MODE_CONFIG", mode_config_map)

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
        ttl_seconds=60,
        session_config={
            "mode": mode,
            "context": context,
            "title": title,
            "label": label,
        },
    )
    return conn


# ---------------------------------------------------------------------------
# Reminder mode: core settings
# ---------------------------------------------------------------------------

def test_reminder_mode_followup_disabled(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="drink water", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_enabled is False, (
        "Reminders must NOT have follow-ups — only 1 voice message allowed"
    )


def test_reminder_mode_server_initiates_chat(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="take vitamins", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.server_initiate_chat is True, (
        "Reminder must speak first without waiting for user input"
    )


def test_reminder_mode_uses_separate_conversation(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="call mom", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.use_mode_conversation is True


def test_reminder_mode_active_mode_is_set(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="stretch", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode == "reminder"


# ---------------------------------------------------------------------------
# Reminder mode: context injection
# ---------------------------------------------------------------------------

def test_reminder_mode_context_injected_into_instructions(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="drink water", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'The user asked to be reminded about: "drink water".' in conn.mode_specific_instructions
    assert "Use this as the reminder meaning, not as raw text to parrot back." in conn.mode_specific_instructions
    assert "Your very first spoken sentence must already contain the reminder reason." in conn.mode_specific_instructions


def test_reminder_mode_base_instructions_present(monkeypatch):
    conn = _build_conn_for_mode("reminder", context="any", monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert "You have one job" in conn.mode_specific_instructions


def test_reminder_mode_no_context_still_loads_base_instructions(monkeypatch):
    """If context is somehow missing, the base instructions still load cleanly."""
    conn = _build_conn_for_mode("reminder", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert "You have one job" in conn.mode_specific_instructions
    # Context block must NOT be injected when context is empty/None
    assert "The user asked to be reminded about" not in conn.mode_specific_instructions


def test_reminder_mode_title_used_when_context_missing(monkeypatch):
    conn = _build_conn_for_mode(
        "reminder",
        context=None,
        monkeypatch=monkeypatch,
        title="take vitamins",
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'The user asked to be reminded about: "take vitamins".' in conn.mode_specific_instructions


def test_reminder_mode_label_used_when_context_and_title_missing(monkeypatch):
    conn = _build_conn_for_mode(
        "reminder",
        context=None,
        monkeypatch=monkeypatch,
        label="drink water",
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'The user asked to be reminded about: "drink water".' in conn.mode_specific_instructions


# ---------------------------------------------------------------------------
# Isolation: morning_alarm mode is NOT affected by reminder mode config
# ---------------------------------------------------------------------------

def test_alarm_mode_followup_still_enabled_after_reminder_config_added(monkeypatch):
    """Ensures adding 'reminder' to MODE_CONFIG doesn't break morning_alarm."""
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_enabled is True
    assert conn.followup_max == 2
    assert conn.active_mode == "morning_alarm"


def test_alarm_mode_instructions_not_contaminated_by_reminder(monkeypatch):
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert "WAKE UP" in conn.mode_specific_instructions
    assert "You have one job" not in conn.mode_specific_instructions


# ---------------------------------------------------------------------------
# followup_max default for alarm: 2 nudges = 3 total voice messages
# ---------------------------------------------------------------------------

def test_alarm_mode_followup_max_is_2(monkeypatch):
    """3 total messages = 1 initial + 2 nudges → followup_max must be 2."""
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == 2, (
        f"morning_alarm followup_max should be 2 (for 3 total messages) but got {conn.followup_max}"
    )
