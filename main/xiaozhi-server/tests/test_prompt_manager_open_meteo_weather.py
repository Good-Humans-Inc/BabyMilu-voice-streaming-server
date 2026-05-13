from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.utils import prompt_manager as prompt_manager_module
from core.utils.cache.manager import CacheType, cache_manager
from core.utils.prompt_manager import PromptManager
from plugins_func.functions import get_weather as weather_tool


class _Logger:
    def bind(self, **_kwargs):
        return self

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


def _reset_prompt_weather_caches():
    cache_manager.clear(CacheType.DEVICE_PROMPT)
    cache_manager.clear(CacheType.LOCATION)
    cache_manager.clear(CacheType.WEATHER)
    cache_manager.clear(CacheType.CONFIG)


def _patch_open_meteo_helpers(monkeypatch):
    calls = []

    def fake_fetch_city_info(location):
        calls.append(("city", location))
        return {
            "name": "San Francisco",
            "admin1": "California",
            "country": "United States",
            "latitude": 37.7749,
            "longitude": -122.4194,
            "timezone": "America/Los_Angeles",
        }

    def fake_fetch_weather_forecast(latitude, longitude, timezone="auto", forecast_days=7):
        calls.append(("forecast", latitude, longitude, timezone, forecast_days))
        return {
            "current": {
                "temperature_2m": 18.4,
                "relative_humidity_2m": 72,
                "weather_code": 2,
                "wind_speed_10m": 14.2,
            },
            "daily": {
                "time": [
                    "2026-05-12",
                    "2026-05-13",
                    "2026-05-14",
                ],
                "weather_code": [2, 61, 0],
                "temperature_2m_max": [20.0, 17.5, 22.25],
                "temperature_2m_min": [12.5, 11.0, 13.75],
            },
        }

    monkeypatch.setattr(weather_tool, "fetch_city_info", fake_fetch_city_info)
    monkeypatch.setattr(
        weather_tool,
        "fetch_weather_forecast",
        fake_fetch_weather_forecast,
    )

    if hasattr(prompt_manager_module, "fetch_city_info"):
        monkeypatch.setattr(prompt_manager_module, "fetch_city_info", fake_fetch_city_info)
    if hasattr(prompt_manager_module, "fetch_weather_forecast"):
        monkeypatch.setattr(
            prompt_manager_module,
            "fetch_weather_forecast",
            fake_fetch_weather_forecast,
        )

    return calls


def _new_prompt_manager():
    pm = PromptManager({"prompt": "base", "selected_module": {}}, _Logger())
    pm.base_prompt_template = (
        "Base: {{base_prompt}}\n"
        "Local: {{local_address}}\n"
        "Weather:\n{{weather_info}}\n"
    )
    return pm


def _patch_prompt_dependencies(monkeypatch):
    monkeypatch.setattr(prompt_manager_module, "get_timezone_for_device", lambda _id: None)
    monkeypatch.setattr(
        prompt_manager_module,
        "get_active_character_for_device",
        lambda _id: "character-open-meteo",
    )
    monkeypatch.setattr(
        prompt_manager_module,
        "get_owner_phone_for_device",
        lambda _id: "+15555550100",
    )
    monkeypatch.setattr(
        prompt_manager_module,
        "get_user_profile_by_phone",
        lambda _phone: {"name": "Milo"},
    )
    monkeypatch.setattr(
        prompt_manager_module,
        "extract_user_profile_fields",
        lambda doc: {"name": doc.get("name", "user")},
    )


def test_enhanced_prompt_weather_uses_open_meteo_helpers_without_openweather_key(
    monkeypatch,
):
    _reset_prompt_weather_caches()
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    calls = _patch_open_meteo_helpers(monkeypatch)
    _patch_prompt_dependencies(monkeypatch)

    def fail_if_openweather_is_used(url, *_args, **_kwargs):
        raise AssertionError(f"unexpected direct weather HTTP call: {url}")

    monkeypatch.setattr(weather_tool.requests, "get", fail_if_openweather_is_used)
    monkeypatch.setattr(
        PromptManager,
        "_get_user_city_from_profile",
        lambda self, device_id: "San Francisco, CA",
    )

    enhanced = _new_prompt_manager().build_enhanced_prompt(
        "Tell Milo what to wear outside.",
        device_id="device-open-meteo",
        client_ip="203.0.113.5",
    )

    assert "Local: San Francisco, CA" in enhanced
    assert "Location queried: San Francisco, California, United States" in enhanced
    assert "Current weather: Partly cloudy" in enhanced
    assert "Current temperature: 18.4\u00b0C" in enhanced
    assert "Humidity: 72%" in enhanced
    assert "Wind speed: 14.2 km/h" in enhanced
    assert "7-day forecast:" in enhanced
    assert "May 12: Partly cloudy, temperature 12.5\u00b0C~20.0\u00b0C" in enhanced
    assert "May 13: Rain (slight), temperature 11.0\u00b0C~17.5\u00b0C" in enhanced
    assert calls == [
        ("city", "San Francisco, CA"),
        ("forecast", 37.7749, -122.4194, "America/Los_Angeles", 7),
    ]


def test_prompt_weather_info_matches_get_weather_forecast_format(monkeypatch):
    _reset_prompt_weather_caches()
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    calls = _patch_open_meteo_helpers(monkeypatch)

    weather_info = _new_prompt_manager()._get_weather_info("San Francisco, CA")

    assert weather_info.startswith(
        "Location queried: San Francisco, California, United States\n\n"
    )
    assert "Current weather: Partly cloudy\n" in weather_info
    assert "Current temperature: 18.4\u00b0C\n" in weather_info
    assert "\n7-day forecast:\n" in weather_info
    assert "May 14: Clear sky, temperature 13.75\u00b0C~22.25\u00b0C\n" in weather_info
    assert weather_info.endswith(
        "(If you need specific weather for a particular day, please tell me the date)"
    )
    assert calls == [
        ("city", "San Francisco, CA"),
        ("forecast", 37.7749, -122.4194, "America/Los_Angeles", 7),
    ]
