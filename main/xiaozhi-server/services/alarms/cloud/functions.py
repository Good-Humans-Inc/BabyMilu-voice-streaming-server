from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from services.logging import setup_logging
from services.alarms import scheduler, tasks
from services.messaging.mqtt import publish_ws_start
from services.alarms.config import ALARM_TIMING

TAG = __name__
logger = setup_logging()


def scan_due_alarms(request) -> Dict[str, Any]:
    """HTTP entrypoint for Cloud Scheduler."""
    now = datetime.now(timezone.utc)
    wake_requests = scheduler.prepare_wake_requests(
        now, lookahead=ALARM_TIMING["lookahead"]
    )
    triggered = 0
    for wake_request in wake_requests:
        if _wake_device(wake_request):
            triggered += 1
    logger.bind(tag=TAG).info(
        f"Processed {len(wake_requests)} wake requests; fired {triggered}"
    )
    return {"ok": True, "count": len(wake_requests), "triggered": triggered}


def _wake_device(wake_request: tasks.WakeRequest) -> bool:
    ws_url = _resolve_ws_url()
    if not ws_url:
        logger.bind(tag=TAG).warning("ALARM_WS_URL not configured; skipping wake")
        return False
    broker = _resolve_broker_url()
    ok = publish_ws_start(broker, wake_request.target.device_id, ws_url)
    if ok:
        logger.bind(tag=TAG).info(
            f"Published ws_start to {wake_request.target.device_id} "
            f"for alarm {wake_request.alarm.alarm_id}"
        )
    else:
        logger.bind(tag=TAG).warning(
            f"Failed to publish ws_start to {wake_request.target.device_id}"
        )
    return bool(ok)


def _resolve_ws_url() -> str:
    return os.environ.get("ALARM_WS_URL") or os.environ.get("DEFAULT_WS_URL", "")


def _resolve_broker_url() -> str:
    return os.environ.get("ALARM_MQTT_URL") or os.environ.get("MQTT_URL", "")

