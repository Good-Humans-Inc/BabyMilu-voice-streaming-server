from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

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
    results: List[Dict[str, Any]] = []
    for wake_request in wake_requests:
        fired = _wake_device(wake_request)
        if fired:
            triggered += 1
        results.append(
            {
                "alarmId": wake_request.alarm.alarm_id,
                "label": wake_request.alarm.label,
                "deviceId": wake_request.target.device_id,
                "mode": wake_request.target.mode,
                "fired": bool(fired),
            }
        )
    logger.bind(tag=TAG).info(
        f"Processed {len(wake_requests)} wake requests; fired {triggered}"
    )
    return {
        "ok": True,
        "count": len(wake_requests),
        "triggered": triggered,
        "results": results,
    }


def _wake_device(wake_request: tasks.WakeRequest) -> bool:
    endpoints = _resolve_alarm_endpoints()
    fired = False
    for endpoint in endpoints:
        ok = publish_ws_start(
            endpoint["mqtt"], 
            wake_request.target.device_id, 
            endpoint["ws"]
        )
        if ok:
            fired = True
            logger.bind(tag=TAG).info(
                f"Published ws_start to {wake_request.target.device_id} "
                f"(endpoint={endpoint['name']}) for alarm {wake_request.alarm.alarm_id}"
            )
        else:
            logger.bind(tag=TAG).warning(
                f"Failed to publish ws_start to {wake_request.target.device_id} "
                f"(endpoint={endpoint['name']})"
            )
    return fired


def _resolve_alarm_endpoints() -> List[Dict[str, Any]]:
    """Return configured websocket/mqtt endpoint pairs."""
    raw = os.environ.get("ALARM_ENDPOINTS", "")
    return json.loads(raw)

