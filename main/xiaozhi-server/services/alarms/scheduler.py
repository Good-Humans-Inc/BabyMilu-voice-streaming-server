from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from services.logging import setup_logging
from services.alarms import firestore_client, models, tasks
from services.alarms.config import ALARM_TIMING
from services.session_context import store as session_context_store

TAG = __name__
logger = setup_logging()
SESSION_TYPE = "alarm"
SESSION_TTL = ALARM_TIMING["session_ttl"]

_DAY_TO_INDEX = {name: idx for idx, name in enumerate(models.DAY_NAMES)}


def prepare_wake_requests(
    now: datetime,
    lookahead: timedelta,
) -> List[tasks.WakeRequest]:
    wake_requests: List[tasks.WakeRequest] = []
    alarms = firestore_client.fetch_due_alarms(now, lookahead=lookahead)
    for alarm in alarms:
        if not alarm.next_occurrence_utc:
            logger.bind(tag=TAG).warning(
                f"Alarm {alarm.alarm_id} missing next_occurrence_utc; skipping"
            )
            continue
        if (
            alarm.last_processed_utc
            and alarm.last_processed_utc >= alarm.next_occurrence_utc
        ):
            logger.bind(tag=TAG).info(
                f"Alarm {alarm.alarm_id} already processed at "
                f"{alarm.last_processed_utc.isoformat()}; skipping"
            )
            continue
        alarm_triggered = False
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
            alarm_triggered = True
        if alarm_triggered:
            try:
                next_occurrence = compute_next_occurrence(alarm, now=now)
                firestore_client.mark_alarm_processed(
                    alarm,
                    last_processed=alarm.next_occurrence_utc,
                    next_occurrence=next_occurrence,
                )
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to advance alarm {alarm.alarm_id}: {exc}"
                )
    logger.bind(tag=TAG).info(f"Prepared {len(wake_requests)} wake requests")
    return wake_requests


def compute_next_occurrence(
    alarm: models.AlarmDoc, *, now: Optional[datetime] = None
) -> datetime:
    tzinfo = ZoneInfo(_resolve_timezone(alarm))
    alarm_time = datetime.strptime(alarm.schedule.time_local, "%H:%M").time()
    allowed_days = sorted({_DAY_TO_INDEX[day] for day in alarm.schedule.days})
    if not allowed_days:
        allowed_days = list(range(7))

    now_utc = now or datetime.now(timezone.utc)
    start_utc = max(alarm.next_occurrence_utc or now_utc, now_utc)
    start_local = start_utc.astimezone(tzinfo)

    for delta in range(0, 8):
        candidate_date = start_local.date() + timedelta(days=delta)
        if candidate_date.weekday() not in allowed_days:
            continue
        candidate_local = datetime.combine(candidate_date, alarm_time, tzinfo=tzinfo)
        if candidate_local > start_local:
            result = candidate_local.astimezone(timezone.utc)
            logger.bind(tag=TAG).info(
                "Next occurrence for alarm %s (user=%s, tz=%s, local=%s) is %s UTC",
                alarm.alarm_id,
                alarm.user_id,
                tzinfo.key,
                candidate_local.isoformat(),
                result.isoformat(),
            )
            return result

    candidate_date = start_local.date() + timedelta(days=7)
    candidate_local = datetime.combine(candidate_date, alarm_time, tzinfo=tzinfo)
    fallback = candidate_local.astimezone(timezone.utc)
    logger.bind(tag=TAG).info(
        "Fallback next occurrence for alarm %s (user=%s, tz=%s, local=%s) is %s UTC",
        alarm.alarm_id,
        alarm.user_id,
        tzinfo.key,
        candidate_local.isoformat(),
        fallback.isoformat(),
    )
    return fallback


def _resolve_timezone(alarm: models.AlarmDoc) -> str:
    raw = alarm.raw or {}
    tz_name = raw.get("timezone")
    if not tz_name:
        user_block = raw.get("user")
        if isinstance(user_block, dict):
            tz_name = user_block.get("timezone")
    if not tz_name:
        raise ValueError(
            f"Alarm {alarm.alarm_id} (user={alarm.user_id}) missing timezone metadata."
        )
    return tz_name