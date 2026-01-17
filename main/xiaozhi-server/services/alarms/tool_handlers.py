from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from services.logging import setup_logging
from services.alarms import firestore_client, models, scheduler

TAG = __name__
logger = setup_logging()


class AlarmNotFound(Exception):
    pass


def snooze_alarm(alarm_id: str, user_id: str, delta: timedelta) -> models.AlarmDoc:
    """Server-side handler called via LLM tool."""
    # TODO: implement Firestore snooze logic
    logger.bind(tag=TAG).info(f"Snoozing alarm {alarm_id} by {delta}")
    raise NotImplementedError


def dismiss_alarm(alarm_id: str, user_id: str) -> None:
    logger.bind(tag=TAG).info(f"Dismissing alarm {alarm_id}")
    raise NotImplementedError


