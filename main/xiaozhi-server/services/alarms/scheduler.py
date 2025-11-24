from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Tuple

from services.logging import setup_logging
from services.alarms import firestore_client, models, tasks
from services.alarms.config import ALARM_TIMING
from services.session_context import store as session_context_store

TAG = __name__
logger = setup_logging()
SESSION_TYPE = "alarm"
SESSION_TTL = ALARM_TIMING["session_ttl"]


def prepare_wake_requests(
    now: datetime,
    lookahead: timedelta,
) -> List[tasks.WakeRequest]:
    wake_requests: List[tasks.WakeRequest] = []
    alarms = firestore_client.fetch_due_alarms(now, lookahead=lookahead)
    for alarm in alarms:
        if not alarm.targets:
            logger.bind(tag=TAG).warning(
                f"Alarm {alarm.alarm_id} has no targets; skipping"
            )
            continue
        for target in alarm.targets:
            if not target.device_id:
                logger.bind(tag=TAG).warning(
                    f"Alarm {alarm.alarm_id} target is missing device_id; skipping"
                )
                continue
            existing = session_context_store.get_session(target.device_id, now=now)
            if existing:
                logger.bind(tag=TAG).warning(
                    f"Skipping device {target.device_id}: existing session active ({existing.session_type})"
                )
                continue
            session_config = {
                "mode": target.mode,
                "alarmId": alarm.alarm_id,
                "userId": alarm.user_id,
                "label": alarm.label,
            }
            new_session = session_context_store.create_session(
            device_id=target.device_id,
                session_type=SESSION_TYPE,
                ttl=SESSION_TTL,
                triggered_at=now,
                session_config=session_config,
        )
        wake_requests.append(
            tasks.WakeRequest(alarm=alarm, target=target, session=new_session)
        )
    logger.bind(tag=TAG).info(f"Prepared {len(wake_requests)} wake requests")
    return wake_requests


def compute_next_occurrence(alarm: models.AlarmDoc) -> datetime:
    """Placeholder for repeat logic."""
    # TODO: implement recurrence per TDD
    return alarm.next_occurrence_utc + timedelta(days=1)