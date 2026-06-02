from __future__ import annotations

import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from plugins_func.functions import get_current_time as time_tool
from plugins_func.register import all_function_registry
from core.providers.tools.server_plugins.plugin_executor import ServerPluginExecutor
from core.utils.current_time import get_current_time_info


class _Conn:
    device_id = "device-123"


def test_get_current_time_info_accepts_timezone():
    current_time, today_date, today_weekday, lunar_date = get_current_time_info(
        "America/Los_Angeles"
    )

    assert current_time
    assert today_date
    assert today_weekday
    assert lunar_date


def test_get_current_time_tool_uses_device_timezone(monkeypatch):
    monkeypatch.setattr(
        time_tool,
        "get_timezone_for_device",
        lambda device_id: "America/Los_Angeles",
    )
    monkeypatch.setattr(
        time_tool,
        "format_current_time",
        lambda timezone=None: f"time in {timezone}",
    )
    monkeypatch.setattr(
        time_tool,
        "get_current_date",
        lambda timezone=None: f"date in {timezone}",
    )
    monkeypatch.setattr(
        time_tool,
        "get_current_weekday",
        lambda timezone=None: f"weekday in {timezone}",
    )

    response = time_tool.get_current_time(_Conn())
    payload = json.loads(response.result)

    assert payload["current_time"] == "time in America/Los_Angeles"
    assert payload["today_date"] == "date in America/Los_Angeles"
    assert payload["today_weekday"] == "weekday in America/Los_Angeles"
    assert payload["timezone"] == "America/Los_Angeles"
    assert "older time/date values" in payload["instruction"]


def test_get_current_time_tool_is_registered_and_enabled_in_config():
    config_path = pathlib.Path(__file__).resolve().parents[1] / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert "get_current_time" in all_function_registry
    assert (
        "get_current_time"
        in config["Intent"]["function_call"]["functions"]
    )


def test_get_current_time_tool_is_always_exposed_by_server_plugins():
    conn = type("Conn", (), {"config": {}})()
    tools = ServerPluginExecutor(conn).get_tools()

    assert "get_current_time" in tools
