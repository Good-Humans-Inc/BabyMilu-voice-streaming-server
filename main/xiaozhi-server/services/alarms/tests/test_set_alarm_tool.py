from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from plugins_func.functions import set_alarm as set_alarm_module
from plugins_func.register import Action


def test_set_alarm_returns_clarification_when_time_parse_fails(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")

    monkeypatch.setattr(
        set_alarm_module, "get_timezone_for_device", lambda device_id: "UTC"
    )
    monkeypatch.setattr(set_alarm_module.dateparser, "parse", lambda *args, **kwargs: None)
    create_called = False

    def fake_create_alarm(**kwargs):
        nonlocal create_called
        create_called = True
        return "unused"

    monkeypatch.setattr(set_alarm_module, "create_alarm", fake_create_alarm)

    result = set_alarm_module.set_alarm(
        conn, time_expression="tomorrow morning", reason="take vitamins"
    )

    assert result.action == Action.RESPONSE
    assert "couldn't understand that time" in result.response
    assert create_called is False


def test_set_alarm_persists_alarm_when_uid_exists(monkeypatch):
    conn = SimpleNamespace(device_id="AA:BB:CC:DD:EE:FF")
    resolved = datetime(2026, 3, 3, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    monkeypatch.setattr(
        set_alarm_module,
        "get_timezone_for_device",
        lambda device_id: "America/Los_Angeles",
    )
    monkeypatch.setattr(
        set_alarm_module, "get_owner_phone_for_device", lambda device_id: "15551234567"
    )
    monkeypatch.setattr(
        set_alarm_module.dateparser, "parse", lambda *args, **kwargs: resolved
    )

    recorded = {}

    def fake_create_alarm(**kwargs):
        recorded.update(kwargs)
        return "alarm-123"

    monkeypatch.setattr(set_alarm_module, "create_alarm", fake_create_alarm)

    result = set_alarm_module.set_alarm(
        conn, time_expression="tomorrow at 9:30am", reason="take vitamins"
    )

    assert result.action == Action.REQLLM
    assert "Alarm set: 'take vitamins'" in result.result
    assert recorded["uid"] == "15551234567"
    assert recorded["device_id"] == conn.device_id
    assert recorded["resolved_dt"] == resolved
    assert recorded["label"] == "take vitamins"
    assert recorded["context"] == "take vitamins"
    assert recorded["tz_str"] == "America/Los_Angeles"
