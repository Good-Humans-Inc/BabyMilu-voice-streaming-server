from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from services.logging import setup_logging
from services.alarms import scheduler, tasks
from services.messaging.mqtt import publish_ws_start
from services.alarms.config import ALARM_TIMING
from core.utils.mac import normalize_mac

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
        if not _is_device_allowed(wake_request.target.device_id):
            logger.bind(tag=TAG).info(
                f"Skipping wake for filtered device {wake_request.target.device_id}"
            )
            try:
                scheduler.rollback_wake_request(wake_request)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to rollback filtered wake request for {wake_request.target.device_id}: {exc}"
                )
            results.append(
                {
                    "alarmId": wake_request.alarm.alarm_id,
                    "label": wake_request.alarm.label,
                    "deviceId": wake_request.target.device_id,
                    "mode": wake_request.target.mode,
                    "fired": False,
                    "skipped": "device_filter",
                }
            )
            continue
        fired = _wake_device(wake_request)
        if fired:
            triggered += 1
            try:
                scheduler.finalize_wake_request(wake_request, now=now)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to finalize alarm {wake_request.alarm.alarm_id}: {exc}"
                )
        else:
            try:
                scheduler.rollback_wake_request(wake_request)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to rollback wake request for {wake_request.target.device_id}: {exc}"
                )
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


def _is_device_allowed(device_id: str) -> bool:
    allowed = _parse_device_set(os.environ.get("ALARM_DEVICE_ALLOWLIST", ""))
    denied = _parse_device_set(os.environ.get("ALARM_DEVICE_DENYLIST", ""))
    try:
        normalized = normalize_mac(device_id)
    except Exception:
        normalized = (device_id or "").lower()
    if normalized in denied:
        return False
    if not allowed:
        return True
    return normalized in allowed


def _parse_device_set(raw: str) -> set[str]:
    tokens = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            tokens.add(normalize_mac(token))
        except Exception:
            tokens.add(token.lower())
    return tokens

