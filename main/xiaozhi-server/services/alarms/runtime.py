from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from core.utils.util import get_local_ip
from services.alarms.cloud import functions as cloud_functions
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()

_FALSEY = {"0", "false", "no", "off", ""}


def _looks_like_real_ws_url(value: str) -> bool:
    lowered = value.lower()
    if not lowered.startswith(("ws://", "wss://")):
        return False
    placeholders = ("your", "浣", "<", "example")
    return not any(token in lowered for token in placeholders)


def alarm_scanner_enabled() -> bool:
    raw = os.environ.get("ALARM_SCANNER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def resolve_alarm_ws_url(config: Optional[Dict[str, Any]]) -> str:
    explicit = (os.environ.get("ALARM_WS_URL") or os.environ.get("DEFAULT_WS_URL") or "").strip()
    if explicit:
        return explicit

    server_config = (config or {}).get("server", {}) if isinstance(config, dict) else {}
    configured = str(server_config.get("websocket", "") or "").strip()
    if configured and _looks_like_real_ws_url(configured):
        return configured

    host = (
        os.environ.get("SERVER_EXTERNAL_IP")
        or os.environ.get("SERVER_PUBLIC_HOST")
        or get_local_ip()
    )
    port = int(server_config.get("port", 8000) or 8000)
    return f"ws://{host}:{port}/xiaozhi/v1/"


def configure_alarm_runtime_env(config: Optional[Dict[str, Any]]) -> str:
    ws_url = resolve_alarm_ws_url(config)
    if ws_url and not os.environ.get("DEFAULT_WS_URL"):
        os.environ["DEFAULT_WS_URL"] = ws_url
    if os.environ.get("MQTT_URL") and not os.environ.get("ALARM_MQTT_URL"):
        os.environ["ALARM_MQTT_URL"] = os.environ["MQTT_URL"]
    logger.bind(tag=TAG).info(
        f"Alarm runtime configured: ws_url={ws_url}, mqtt_url={os.environ.get('ALARM_MQTT_URL') or os.environ.get('MQTT_URL') or ''}"
    )
    return ws_url


def run_alarm_scan_once(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    configure_alarm_runtime_env(config)
    return cloud_functions.scan_due_alarms(request={})


async def run_alarm_scanner_loop(
    config: Optional[Dict[str, Any]] = None,
    *,
    interval_seconds: Optional[float] = None,
) -> None:
    interval = interval_seconds
    if interval is None:
        interval = float(os.environ.get("ALARM_SCANNER_INTERVAL_SECONDS", "15"))
    logger.bind(tag=TAG).info(
        f"Alarm scanner loop started (interval={interval}s, enabled={alarm_scanner_enabled()})"
    )
    try:
        while True:
            try:
                result = await asyncio.to_thread(run_alarm_scan_once, config)
                count = int(result.get("count", 0))
                triggered = int(result.get("triggered", 0))
                if count or triggered:
                    logger.bind(tag=TAG).info(
                        f"Alarm scanner tick complete: count={count}, triggered={triggered}"
                    )
                else:
                    logger.bind(tag=TAG).debug(
                        "Alarm scanner tick complete: no due alarms"
                    )
            except Exception as exc:
                logger.bind(tag=TAG).warning(f"Alarm scanner tick failed: {exc}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.bind(tag=TAG).info("Alarm scanner loop stopped")
        raise
