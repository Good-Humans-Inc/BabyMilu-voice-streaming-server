from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from plugins_func.functions import schedule_conversation as sc_module
from plugins_func.register import Action

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FULL_KWARGS = dict(
    time_expression="tomorrow at 9am",
    label="Take vitamins daily",
    content="take vitamins",
    type_hint="habit",
    priority="medium",
    conversation_outline="1. Open gently. 2. Ask if done. 3. Done when user confirms.",
    character_reminder="Character checking on a habit — be warm, not preachy.",
    emotional_context="User seemed tired but motivated when setting this.",
    completion_signal="Done: user confirms. Snoozed: asks for later. Resisting: pushes back.",
    delivery_preference="be gentle",
)

_RESOLVED = datetime(2026, 3, 24, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def _patch_common(monkeypatch, *, tz="America/Los_Angeles", uid="15551234567", resolved=_RESOLVED):
    monkeypatch.setattr(sc_module, "get_timezone_for_device", lambda device_id: tz)
    monkeypatch.setattr(sc_module, "get_owner_phone_for_device", lambda device_id: uid)
    monkeypatch.setattr(sc_module.dateparser, "parse", lambda *args, **kwargs: resolved)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_schedule_conversation_returns_clarification_when_time_parse_fails(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(sc_module, "get_timezone_for_device", lambda device_id: "UTC")
    monkeypatch.setattr(sc_module.dateparser, "parse", lambda *args, **kwargs: None)
    create_called = False

    def fake_create(**kwargs):
        nonlocal create_called
        create_called = True
        return "unused"

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    result = sc_module.schedule_conversation(conn, **_FULL_KWARGS)

    assert result.action == Action.RESPONSE
    assert "couldn't understand that time" in result.response
    assert create_called is False


def test_schedule_conversation_persists_all_fields_when_uid_exists(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch)

    recorded = {}

    def fake_create(**kwargs):
        recorded.update(kwargs)
        return "reminder-abc"

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    result = sc_module.schedule_conversation(conn, **_FULL_KWARGS)

    assert result.action == Action.REQLLM
    assert "Take vitamins daily" in result.result
    assert recorded["uid"] == "15551234567"
    assert recorded["device_id"] == conn.device_id
    assert recorded["resolved_dt"] == _RESOLVED
    assert recorded["label"] == "Take vitamins daily"
    assert recorded["tz_str"] == "America/Los_Angeles"
    assert recorded["content"] == "take vitamins"
    assert recorded["type_hint"] == "habit"
    assert recorded["priority"] == "medium"
    assert recorded["conversation_outline"] == _FULL_KWARGS["conversation_outline"]
    assert recorded["character_reminder"] == _FULL_KWARGS["character_reminder"]
    assert recorded["emotional_context"] == _FULL_KWARGS["emotional_context"]
    assert recorded["completion_signal"] == _FULL_KWARGS["completion_signal"]
    assert recorded["delivery_preference"] == "be gentle"


def test_schedule_conversation_returns_error_response_on_firestore_exception(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch)

    def fake_create(**kwargs):
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    result = sc_module.schedule_conversation(conn, **_FULL_KWARGS)

    assert result.action == Action.RESPONSE
    assert "went wrong" in result.response


def test_schedule_conversation_skips_firestore_when_uid_missing(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch, uid=None)
    create_called = False

    def fake_create(**kwargs):
        nonlocal create_called
        create_called = True
        return "unused"

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    result = sc_module.schedule_conversation(conn, **_FULL_KWARGS)

    assert result.action == Action.REQLLM
    assert create_called is False


def test_schedule_conversation_recurrence_forwarded_to_firestore(monkeypatch):
    """recurrence kwarg must be passed through to create_scheduled_conversation."""
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch)

    recorded = {}

    def fake_create(**kwargs):
        recorded.update(kwargs)
        return "reminder-rec"

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    sc_module.schedule_conversation(conn, **{**_FULL_KWARGS, "recurrence": "daily"})

    assert recorded["recurrence"] == "daily"


def test_schedule_conversation_result_includes_reminder_id(monkeypatch):
    """reminder_id must appear in result so LLM can use it for immediate cancel."""
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        sc_module, "create_scheduled_conversation", lambda **kwargs: "reminder-xyz-123"
    )

    result = sc_module.schedule_conversation(conn, **_FULL_KWARGS)

    assert "reminder-xyz-123" in result.result


def test_schedule_conversation_optional_params_default_to_none(monkeypatch):
    """recurrence and delivery_preference are optional — omitting them should not error."""
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    _patch_common(monkeypatch)

    recorded = {}

    def fake_create(**kwargs):
        recorded.update(kwargs)
        return "reminder-xyz"

    monkeypatch.setattr(sc_module, "create_scheduled_conversation", fake_create)

    required_only = {k: v for k, v in _FULL_KWARGS.items() if k not in ("delivery_preference",)}
    result = sc_module.schedule_conversation(conn, **required_only)

    assert result.action == Action.REQLLM
    assert recorded["delivery_preference"] is None
