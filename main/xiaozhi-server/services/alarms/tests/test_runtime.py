from __future__ import annotations

from services.alarms import runtime


def test_alarm_scanner_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("ALARM_SCANNER_ENABLED", raising=False)
    assert runtime.alarm_scanner_enabled() is True


def test_alarm_scanner_enabled_falsey_values(monkeypatch):
    monkeypatch.setenv("ALARM_SCANNER_ENABLED", "false")
    assert runtime.alarm_scanner_enabled() is False


def test_resolve_alarm_ws_url_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("ALARM_WS_URL", "ws://explicit:8000/xiaozhi/v1/")
    assert (
        runtime.resolve_alarm_ws_url({"server": {"port": 8000}})
        == "ws://explicit:8000/xiaozhi/v1/"
    )


def test_resolve_alarm_ws_url_uses_server_external_ip(monkeypatch):
    monkeypatch.delenv("ALARM_WS_URL", raising=False)
    monkeypatch.delenv("DEFAULT_WS_URL", raising=False)
    monkeypatch.setenv("SERVER_EXTERNAL_IP", "136.111.52.199")

    ws_url = runtime.resolve_alarm_ws_url(
        {"server": {"port": 8000, "websocket": "ws://浣犵殑ip鎴栬€呭煙鍚?绔彛鍙?xiaozhi/v1/"}}
    )

    assert ws_url == "ws://136.111.52.199:8000/xiaozhi/v1/"


def test_run_alarm_scan_once_configures_env_and_calls_scanner(monkeypatch):
    monkeypatch.delenv("ALARM_WS_URL", raising=False)
    monkeypatch.delenv("DEFAULT_WS_URL", raising=False)
    monkeypatch.setenv("MQTT_URL", "mqtt://broker:1883")
    monkeypatch.setenv("SERVER_EXTERNAL_IP", "136.111.52.199")

    captured = {}

    def fake_scan_due_alarms(request):
        captured["request"] = request
        captured["default_ws_url"] = runtime.os.environ.get("DEFAULT_WS_URL")
        captured["alarm_mqtt_url"] = runtime.os.environ.get("ALARM_MQTT_URL")
        return {"ok": True, "count": 0, "triggered": 0}

    monkeypatch.setattr(runtime.cloud_functions, "scan_due_alarms", fake_scan_due_alarms)

    result = runtime.run_alarm_scan_once({"server": {"port": 8000}})

    assert result == {"ok": True, "count": 0, "triggered": 0}
    assert captured["request"] == {}
    assert captured["default_ws_url"] == "ws://136.111.52.199:8000/xiaozhi/v1/"
    assert captured["alarm_mqtt_url"] == "mqtt://broker:1883"
