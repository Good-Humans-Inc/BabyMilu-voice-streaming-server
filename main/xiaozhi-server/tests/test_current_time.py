from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from zoneinfo import ZoneInfo

from core.utils import current_time
from plugins_func.functions import get_current_time as current_time_tool


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc)
        if tz is not None:
            return instant.astimezone(tz)
        return instant.replace(tzinfo=None)


def test_get_current_time_info_uses_explicit_timezone(monkeypatch):
    monkeypatch.setattr(current_time, "datetime", _FrozenDateTime)
    monkeypatch.setattr(current_time, "get_current_lunar_date", lambda: "lunar-stub")

    assert current_time.get_current_time_info("America/Los_Angeles") == (
        "11:30 PM",
        "2026-05-11",
        "Monday",
        "lunar-stub",
    )

    assert current_time.get_current_time_info("Asia/Tokyo") == (
        "3:30 PM",
        "2026-05-12",
        "Tuesday",
        "lunar-stub",
    )


def test_get_current_time_helpers_fall_back_for_invalid_timezone(monkeypatch):
    monkeypatch.setattr(current_time, "datetime", _FrozenDateTime)

    assert current_time.get_current_time("Not/AZone") == "6:30 AM"
    assert current_time.get_current_date("Not/AZone") == "2026-05-12"
    assert current_time.get_current_weekday("Not/AZone") == "Tuesday"


def test_get_current_time_tool_resolves_device_timezone(monkeypatch):
    timezone_calls = []

    def fake_get_timezone_for_device(device_id):
        timezone_calls.append(device_id)
        return "America/Los_Angeles"

    monkeypatch.setattr(
        current_time_tool,
        "get_timezone_for_device",
        fake_get_timezone_for_device,
    )
    monkeypatch.setattr(
        current_time_tool,
        "format_current_time",
        lambda timezone=None: f"time in {timezone}",
    )
    monkeypatch.setattr(
        current_time_tool,
        "get_current_date",
        lambda timezone=None: f"date in {timezone}",
    )
    monkeypatch.setattr(
        current_time_tool,
        "get_current_weekday",
        lambda timezone=None: f"weekday in {timezone}",
    )

    response = current_time_tool.get_current_time(
        SimpleNamespace(device_id="device-123")
    )
    payload = json.loads(response.result)

    assert timezone_calls == ["device-123"]
    assert payload["current_time"] == "time in America/Los_Angeles"
    assert payload["today_date"] == "date in America/Los_Angeles"
    assert payload["today_weekday"] == "weekday in America/Los_Angeles"
    assert payload["timezone"] == "America/Los_Angeles"
