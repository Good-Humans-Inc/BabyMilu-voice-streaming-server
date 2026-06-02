"""
Reminder next-occurrence math aligned with babymilu-backend
`services/reminder_helper.py` (used by the reminder advancement flow).

Uses zoneinfo.ZoneInfo instead of pytz to avoid an extra dependency.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

DAY_MAP = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}


def parse_time_local(time_str: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = time_str.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except Exception as exc:
        raise ValueError(
            f"Invalid time format: {time_str}. Expected HH:MM"
        ) from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {time_str}")
    return hour, minute


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_repeat(schedule: Dict[str, Any]) -> Optional[str]:
    raw = schedule.get("repeat")
    if raw is not None:
        normalized = str(raw).strip().lower()
        return normalized or None

    days = schedule.get("days") or []
    if not isinstance(days, list) or not days:
        return None
    if all(isinstance(day, int) for day in days):
        return "monthly"
    weekday_days = [day for day in days if isinstance(day, str)]
    if len(weekday_days) == len(DAY_MAP):
        return "daily"
    if weekday_days:
        return "weekly"
    return None


def get_next_occurrence_utc(
    schedule: Dict[str, Any],
    user_timezone: str,
    from_date: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Same contract as backend get_next_occurrence_utc: next instant strictly after
    `from_date` for recurring schedules (or valid one-time in the future).
    """
    if from_date is None:
        from_date = datetime.now(timezone.utc)
    from_date = _as_utc(from_date)

    repeat = _normalize_repeat(schedule)
    time_local = schedule.get("timeLocal")
    if not time_local:
        raise ValueError("Missing timeLocal in schedule")
    if not isinstance(time_local, str):
        raise ValueError("timeLocal must be a string")

    try:
        tz = ZoneInfo(str(user_timezone).strip() or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")

    hour, minute = parse_time_local(time_local)

    if repeat in (None, "none"):
        date_local = schedule.get("dateLocal")
        if not date_local:
            raise ValueError("Missing dateLocal for non-repeating reminder")
        try:
            year_s, month_s, day_s = str(date_local).split("-")
            local_dt = datetime(
                int(year_s),
                int(month_s),
                int(day_s),
                hour,
                minute,
                tzinfo=tz,
            )
        except Exception as exc:
            raise ValueError(f"Invalid dateLocal: {date_local}") from exc
        utc_dt = local_dt.astimezone(timezone.utc)
        if utc_dt < from_date:
            return None
        return utc_dt

    if repeat == "daily":
        from_local = from_date.astimezone(tz)
        candidate = datetime(
            from_local.year,
            from_local.month,
            from_local.day,
            hour,
            minute,
            tzinfo=tz,
        )
        if candidate.astimezone(timezone.utc) <= from_date:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    if repeat == "monthly":
        days = schedule.get("days", [])
        if not days or not isinstance(days, list):
            raise ValueError("Missing or invalid 'days' for monthly reminder")
        target_day = int(days[0])
        from_local = from_date.astimezone(tz)
        # Iterate up to 13 months forward to find the next valid occurrence.
        # Clamp to the last day of the month for short months (e.g. day 31 → April 30).
        for month_offset in range(13):
            total_months = (from_local.year * 12 + from_local.month - 1) + month_offset
            year = total_months // 12
            month = total_months % 12 + 1
            last_day = calendar.monthrange(year, month)[1]
            actual_day = min(target_day, last_day)
            try:
                candidate = datetime(year, month, actual_day, hour, minute, tzinfo=tz)
                if candidate.astimezone(timezone.utc) > from_date:
                    return candidate.astimezone(timezone.utc)
            except ValueError:
                continue
        raise ValueError("Failed to find next monthly occurrence")

    if repeat == "weekly":
        days = schedule.get("days", [])
        if not days or not isinstance(days, list):
            raise ValueError("Missing or invalid 'days' for weekly reminder")
        day_map = {
            "Mon": 0,
            "Tue": 1,
            "Wed": 2,
            "Thu": 3,
            "Fri": 4,
            "Sat": 5,
            "Sun": 6,
        }
        target_weekdays: list[int] = []
        for day in days:
            if not isinstance(day, str) or day not in day_map:
                raise ValueError(f"Invalid day name: {day}")
            target_weekdays.append(day_map[day])
        target_weekdays.sort()

        from_local = from_date.astimezone(tz)
        for day_offset in range(8):
            candidate_date = from_local.date() + timedelta(days=day_offset)
            if candidate_date.weekday() not in target_weekdays:
                continue
            candidate = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
                tzinfo=tz,
            )
            if candidate.astimezone(timezone.utc) > from_date:
                return candidate.astimezone(timezone.utc)
        raise ValueError("Failed to find next weekly occurrence")

    raise ValueError(f"Invalid repeat value: {repeat}")


def get_trigger_time(
    next_occurrence_utc: datetime,
    advance_minutes: int = 30,
    from_time: Optional[datetime] = None,
) -> datetime:
    """Same as backend get_trigger_time."""
    if from_time is None:
        from_time = datetime.now(timezone.utc)
    T = _as_utc(next_occurrence_utc)
    now = _as_utc(from_time)
    standard_trigger = T - timedelta(minutes=advance_minutes)
    T_minus_30 = T - timedelta(minutes=30)
    T_minus_15 = T - timedelta(minutes=15)
    T_minus_10 = T - timedelta(minutes=10)
    if T_minus_30 <= now <= T_minus_15:
        return T_minus_10
    if T_minus_15 < now < T:
        return T
    return standard_trigger


def compute_advance_after_firing(
    reminder_data: Dict[str, Any],
    user_timezone: str,
    *,
    due_occurrence_utc: datetime,
    now_utc: datetime,
) -> Optional[Tuple[datetime, datetime]]:
    """
    Mirror backend advance_recurring_reminder:
    from_time = current_occurrence + 1s, then get_next_occurrence_utc.

    Additionally loops like the voice scheduler fix: if the next slot is still
    <= now (stale backlog), advance again until strictly in the future so a
    single send does not leave nextOccurrenceUTC in the past.
    """
    schedule = reminder_data.get("schedule") or {}
    repeat = _normalize_repeat(schedule)
    if repeat in (None, "none"):
        return None

    due_occurrence_utc = _as_utc(due_occurrence_utc)
    now_utc = _as_utc(now_utc)
    from_time = due_occurrence_utc + timedelta(seconds=1)

    try:
        next_occurrence = get_next_occurrence_utc(
            schedule, user_timezone, from_date=from_time
        )
    except (ValueError, TypeError):
        return None

    if next_occurrence is None:
        return None

    max_steps = 400
    for _ in range(max_steps):
        next_occurrence = _as_utc(next_occurrence)
        if next_occurrence > now_utc:
            next_trigger = get_trigger_time(next_occurrence, from_time=now_utc)
            return (next_occurrence, _as_utc(next_trigger))
        from_time = next_occurrence + timedelta(seconds=1)
        try:
            nxt = get_next_occurrence_utc(
                schedule, user_timezone, from_date=from_time
            )
        except (ValueError, TypeError):
            return None
        if nxt is None:
            return None
        next_occurrence = nxt

    return None
