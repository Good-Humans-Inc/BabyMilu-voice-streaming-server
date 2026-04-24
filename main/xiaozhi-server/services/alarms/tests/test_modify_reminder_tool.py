from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from plugins_func.functions.modify_reminder import modify_reminder
from plugins_func.register import Action


def _make_conn(device_id="aa:bb:cc:dd:ee:ff"):
    conn = MagicMock()
    conn.device_id = device_id
    return conn


def _patch_device(monkeypatch, *, uid="user-1", tz="America/Los_Angeles"):
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.get_owner_phone_for_device",
        lambda device_id: uid,
    )
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.get_timezone_for_device",
        lambda device_id: tz,
    )


def test_modify_reminder_time_only(monkeypatch):
    """Passing a valid time_expression resolves and forwards resolved_dt."""
    calls = []
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42", time_expression="tomorrow at 9am")

    assert response.action == Action.REQLLM
    assert "alarm-42" in response.result
    assert len(calls) == 1
    assert calls[0]["alarm_id"] == "alarm-42"
    assert calls[0]["resolved_dt"] is not None
    assert calls[0]["content"] is None


def test_modify_reminder_content_only(monkeypatch):
    """Passing content updates label without touching time fields."""
    calls = []
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42", content="morning run")

    assert response.action == Action.REQLLM
    assert "'morning run'" in response.result
    assert calls[0]["content"] == "morning run"
    assert calls[0]["resolved_dt"] is None


def test_modify_reminder_missing_uid_returns_response(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.get_owner_phone_for_device",
        lambda device_id: None,
    )
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.get_timezone_for_device",
        lambda device_id: "UTC",
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42", content="anything")

    assert response.action == Action.RESPONSE
    assert response.response is not None


def test_modify_reminder_invalid_time_returns_response(monkeypatch):
    _patch_device(monkeypatch)

    response = modify_reminder(
        _make_conn(), alarm_id="alarm-42", time_expression="xyzzy not a time"
    )

    assert response.action == Action.RESPONSE
    assert response.response is not None


def test_modify_reminder_firestore_error_returns_response(monkeypatch):
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Firestore down")),
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42", priority="high")

    assert response.action == Action.RESPONSE
    assert response.response is not None


def test_modify_reminder_no_fields_changed(monkeypatch):
    """Calling with only alarm_id still succeeds and returns a sensible result string."""
    calls = []
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42")

    assert response.action == Action.REQLLM
    assert "no fields changed" in response.result
    assert len(calls) == 1


def test_modify_reminder_with_context_regeneration(monkeypatch):
    """When purpose changes, all context fields are forwarded."""
    calls = []
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    response = modify_reminder(
        _make_conn(),
        alarm_id="alarm-42",
        content="evening meditation",
        conversation_outline="1. Open softly.",
        character_reminder="Be calm.",
        emotional_context="User is stressed.",
        completion_signal="Done: user says they're relaxed.",
    )

    assert response.action == Action.REQLLM
    assert "conversation_outline regenerated" in response.result
    assert calls[0]["conversation_outline"] == "1. Open softly."
    assert calls[0]["character_reminder"] == "Be calm."
    assert calls[0]["emotional_context"] == "User is stressed."
    assert calls[0]["completion_signal"] == "Done: user says they're relaxed."


def test_modify_reminder_recurrence_only_forwards_tz_str(monkeypatch):
    """tz_str must be forwarded even when no time_expression is given — needed for next-occurrence recompute."""
    calls = []
    _patch_device(monkeypatch, tz="America/Los_Angeles")
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    modify_reminder(_make_conn(), alarm_id="alarm-42", recurrence="weekly:Mon")

    assert len(calls) == 1
    assert calls[0]["tz_str"] == "America/Los_Angeles"
    assert calls[0]["resolved_dt"] is None


def test_modify_reminder_label_only(monkeypatch):
    """Passing label updates the title without touching content or time fields."""
    calls = []
    _patch_device(monkeypatch)
    monkeypatch.setattr(
        "plugins_func.functions.modify_reminder.modify_scheduled_conversation",
        lambda **kwargs: calls.append(kwargs),
    )

    response = modify_reminder(_make_conn(), alarm_id="alarm-42", label="Evening Walk")

    assert response.action == Action.REQLLM
    assert "label → 'Evening Walk'" in response.result
    assert calls[0]["label"] == "Evening Walk"
    assert calls[0]["content"] is None
