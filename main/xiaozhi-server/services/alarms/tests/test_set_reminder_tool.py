"""Tests for the set_reminder LLM tool call.

Mirrors test_set_alarm_tool.py but verifies:
- create_reminder() is called (not create_alarm)
- deliveryChannel / reminder path are correct at the call site
- No side effects on the alarm path
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# Import will fail until the module is created — that's intentional:
# these tests are written first so the module is developed against them.
from plugins_func.functions import set_reminder as set_reminder_module
from plugins_func.register import Action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn(device_id: str = "AA:BB:CC:DD:EE:FF") -> SimpleNamespace:
    return SimpleNamespace(device_id=device_id)


# ---------------------------------------------------------------------------
# Time parse failures
# ---------------------------------------------------------------------------

def test_set_reminder_returns_clarification_when_time_parse_fails(monkeypatch):
    monkeypatch.setattr(set_reminder_module, "get_timezone_for_device", lambda _: "UTC")
    monkeypatch.setattr(set_reminder_module.dateparser, "parse", lambda *a, **kw: None)

    create_called = False

    def fake_create(**kwargs):
        nonlocal create_called
        create_called = True
        return "unused"

    monkeypatch.setattr(set_reminder_module, "create_reminder", fake_create)

    result = set_reminder_module.set_reminder(
        _conn(), time_expression="whenever", reason="drink water"
    )

    assert result.action == Action.RESPONSE
    assert "couldn't understand that time" in result.response
    assert create_called is False


# ---------------------------------------------------------------------------
# Happy path — uid found → reminder persisted
# ---------------------------------------------------------------------------

def test_set_reminder_persists_reminder_when_uid_exists(monkeypatch):
    resolved = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    monkeypatch.setattr(
        set_reminder_module, "get_timezone_for_device",
        lambda _: "America/Los_Angeles",
    )
    monkeypatch.setattr(
        set_reminder_module, "get_owner_phone_for_device",
        lambda _: "+15551234567",
    )
    monkeypatch.setattr(
        set_reminder_module.dateparser, "parse",
        lambda *a, **kw: resolved,
    )

    recorded: dict = {}

    def fake_create(**kwargs):
        recorded.update(kwargs)
        return "reminder-abc"

    monkeypatch.setattr(set_reminder_module, "create_reminder", fake_create)

    result = set_reminder_module.set_reminder(
        _conn(), time_expression="today at 2pm", reason="drink water"
    )

    assert result.action == Action.REQLLM
    assert "Reminder set: 'drink water'" in result.result

    # Verify the correct arguments are passed to create_reminder
    assert recorded["uid"] == "+15551234567"
    assert recorded["device_id"] == "AA:BB:CC:DD:EE:FF"
    assert recorded["resolved_dt"] == resolved
    assert recorded["label"] == "drink water"
    assert recorded["context"] == "drink water"
    assert recorded["tz_str"] == "America/Los_Angeles"


def test_set_reminder_does_not_call_create_alarm(monkeypatch):
    """Guarantee set_reminder never touches the alarm path."""
    resolved = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr(set_reminder_module, "get_timezone_for_device", lambda _: "UTC")
    monkeypatch.setattr(set_reminder_module, "get_owner_phone_for_device", lambda _: "+1")
    monkeypatch.setattr(set_reminder_module.dateparser, "parse", lambda *a, **kw: resolved)
    monkeypatch.setattr(set_reminder_module, "create_reminder", lambda **kw: "r-1")

    # If set_reminder_module references create_alarm at all, patch it to fail loudly
    if hasattr(set_reminder_module, "create_alarm"):
        monkeypatch.setattr(
            set_reminder_module, "create_alarm",
            lambda **kw: (_ for _ in ()).throw(AssertionError("create_alarm must not be called from set_reminder")),
        )

    result = set_reminder_module.set_reminder(_conn(), time_expression="now", reason="test")
    assert result.action == Action.REQLLM


# ---------------------------------------------------------------------------
# No uid found → reminder NOT persisted, but response still returned
# ---------------------------------------------------------------------------

def test_set_reminder_skips_persistence_when_no_uid(monkeypatch):
    resolved = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr(set_reminder_module, "get_timezone_for_device", lambda _: "UTC")
    monkeypatch.setattr(set_reminder_module, "get_owner_phone_for_device", lambda _: None)
    monkeypatch.setattr(set_reminder_module.dateparser, "parse", lambda *a, **kw: resolved)

    create_called = False

    def fake_create(**kwargs):
        nonlocal create_called
        create_called = True
        return "r-1"

    monkeypatch.setattr(set_reminder_module, "create_reminder", fake_create)

    result = set_reminder_module.set_reminder(
        _conn(), time_expression="in 1 hour", reason="call mom"
    )

    # Still returns a response (LLM confirms the reminder to user)
    assert result.action == Action.REQLLM
    assert create_called is False


# ---------------------------------------------------------------------------
# Firestore write error → response still returned (graceful degradation)
# ---------------------------------------------------------------------------

def test_set_reminder_handles_firestore_error_gracefully(monkeypatch):
    resolved = datetime(2026, 6, 1, 14, 0, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr(set_reminder_module, "get_timezone_for_device", lambda _: "UTC")
    monkeypatch.setattr(set_reminder_module, "get_owner_phone_for_device", lambda _: "+1")
    monkeypatch.setattr(set_reminder_module.dateparser, "parse", lambda *a, **kw: resolved)
    monkeypatch.setattr(
        set_reminder_module, "create_reminder",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("Firestore unavailable")),
    )

    result = set_reminder_module.set_reminder(
        _conn(), time_expression="in 30 mins", reason="stretch"
    )

    # Must still return REQLLM so the LLM can confirm to the user
    assert result.action == Action.REQLLM
