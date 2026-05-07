"""Tests for scheduled conversation reminder delivery on ConnectionHandler."""
from __future__ import annotations

from datetime import datetime, timezone

from core.connection import ConnectionHandler, FollowupState, ModeRuntimeState
from services.session_context import models as session_models


class _DummyLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


_SCHEDULED_CONVERSATION_MODE_CONFIG = {
    "server_initiate_chat": True,
    "followup_enabled": True,
    "followup_delay": 10,
    "followup_max": 1,
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
    monkeypatch.setattr(
        "core.connection.MODE_CONFIG",
        {
            "morning_alarm": _ALARM_MODE_CONFIG,
            "scheduled_conversation": _SCHEDULED_CONVERSATION_MODE_CONFIG,
        },
    )

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


def test_scheduled_conversation_mode_followup_uses_priority_default(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context="drink water",
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_enabled is True
    assert conn.followup_max == 1


def test_scheduled_conversation_mode_server_initiates_chat(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context="take vitamins",
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.server_initiate_chat is True


def test_scheduled_conversation_mode_uses_separate_conversation(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context="call mom",
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.use_mode_conversation is True


def test_scheduled_conversation_mode_active_mode_is_set(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context="stretch",
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode == "scheduled_conversation"


def test_scheduled_conversation_context_injected_into_instructions(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context="drink water",
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'Reminder title (as titled by the user): "drink water"' in conn.mode_specific_instructions
    assert "[CONVERSATION OUTLINE]" in conn.mode_specific_instructions
    assert "Your very first spoken sentence must already contain the reminder reason." in conn.mode_specific_instructions


def test_scheduled_conversation_no_context_still_loads_base_instructions(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context=None,
        monkeypatch=monkeypatch,
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert "[CHARACTER REMINDER]" in conn.mode_specific_instructions
    assert "[OUTCOME INSTRUCTION]" in conn.mode_specific_instructions
    assert 'Reminder title (as titled by the user): ""' in conn.mode_specific_instructions


def test_scheduled_conversation_title_used_when_context_missing(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context=None,
        monkeypatch=monkeypatch,
        title="take vitamins",
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'Reminder title (as titled by the user): "take vitamins"' in conn.mode_specific_instructions


def test_scheduled_conversation_label_used_when_context_and_title_missing(monkeypatch):
    conn = _build_conn_for_mode(
        "scheduled_conversation",
        context=None,
        monkeypatch=monkeypatch,
        label="drink water",
    )
    ConnectionHandler._apply_mode_session_settings(conn)

    assert 'Reminder title (as titled by the user): "drink water"' in conn.mode_specific_instructions


def test_alarm_mode_followup_still_enabled_after_scheduled_conversation_config(monkeypatch):
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_enabled is True
    assert conn.followup_max == 2
    assert conn.active_mode == "morning_alarm"


def test_alarm_mode_instructions_not_contaminated_by_scheduled_conversation(monkeypatch):
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert "WAKE UP" in conn.mode_specific_instructions
    assert "[CHARACTER REMINDER]" not in conn.mode_specific_instructions


def test_alarm_mode_followup_max_is_2(monkeypatch):
    conn = _build_conn_for_mode("morning_alarm", context=None, monkeypatch=monkeypatch)
    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.followup_max == 2
