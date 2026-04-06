from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config.settings import get_gcp_credentials_path
from services.alarms import reminder_advancement
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()

ONE_TIME_REPEATS = {"none", "once", "one_time", "one-time", "no_repeat"}


@dataclass
class ReminderDoc:
    reminder_id: str
    user_id: str
    doc_path: str
    next_occurrence_utc: Optional[datetime]
    next_trigger_utc: Optional[datetime]
    last_processed_utc: Optional[datetime]
    repeat: str
    status: str
    label: Optional[str] = None
    timezone: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


TriggerFn = Callable[[ReminderDoc], bool]


def _build_client() -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        import os

        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    return firestore.Client()


def _collection_group(client: firestore.Client):
    return client.collection_group("reminders")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_user_id(doc) -> str:
    parent = doc.reference.parent.parent
    return parent.id if parent else ""


def fetch_due_reminders(
    now: datetime,
    lookahead: timedelta,
    client: Optional[firestore.Client] = None,
) -> List[ReminderDoc]:
    client = client or _build_client()
    upper_bound = now + lookahead
    upper_bound_str = _format_datetime(upper_bound)

    query = _collection_group(client)
    query = query.where(filter=FieldFilter("status", "==", "on"))
    # For now, scheduler due-time is nextOccurrenceUTC.
    # nextTriggerUTC is kept in schema for future snooze behaviors.
    query = query.where(filter=FieldFilter("nextOccurrenceUTC", "<=", upper_bound_str))

    reminders: List[ReminderDoc] = []
    user_tz_cache: Dict[str, str] = {}
    for doc in query.stream():
        data = doc.to_dict() or {}
        user_id = _resolve_user_id(doc)
        if not user_id:
            logger.bind(tag=TAG).warning(
                f"Skipping reminder {doc.reference.path}: cannot resolve user_id"
            )
            continue
        schedule = data.get("schedule") or {}
        repeat = str(schedule.get("repeat", "")).strip().lower()
        user_timezone = _resolve_user_timezone(doc, user_tz_cache)
        reminders.append(
            ReminderDoc(
                reminder_id=doc.id,
                user_id=user_id,
                doc_path=doc.reference.path,
                next_occurrence_utc=_parse_datetime(data.get("nextOccurrenceUTC")),
                next_trigger_utc=_parse_datetime(data.get("nextTriggerUTC")),
                last_processed_utc=_parse_datetime(data.get("lastProcessedUTC")),
                repeat=repeat,
                status=str(data.get("status", "")).strip().lower(),
                label=data.get("label"),
                timezone=user_timezone,
                raw=data,
            )
        )

    logger.bind(tag=TAG).info(f"Fetched {len(reminders)} due reminders")
    return reminders


def process_due_reminders(
    now: datetime,
    lookahead: timedelta,
    *,
    execute: bool = False,
    trigger_fn: Optional[TriggerFn] = None,
    client: Optional[firestore.Client] = None,
) -> Dict[str, Any]:
    """
    Current behavior:
    - execute=False (default): dry-run only, no writes.
    - execute=True:
      - one-time reminders: send + status=off
      - recurring reminders: send + advance nextOccurrenceUTC
    """
    client = client or _build_client()
    reminders = fetch_due_reminders(now, lookahead, client=client)
    doc_client = client

    triggered = 0
    skipped = 0
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for reminder in reminders:
        try:
            due_time = reminder.next_occurrence_utc
            if not due_time:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "skipped": "invalid_next_occurrence",
                    }
                )
                continue

            if (
                reminder.last_processed_utc
                and reminder.last_processed_utc >= due_time
            ):
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "skipped": "already_processed",
                    }
                )
                continue

            # Match sendReminderPush: only fire when occurrence time has arrived
            # (lookahead widens the query for batching, not early delivery).
            if due_time > _as_utc(now):
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "skipped": "not_yet_due",
                    }
                )
                continue

            if not execute:
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "dryRun": True,
                    }
                )
                continue

            if trigger_fn is None:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "skipped": "missing_trigger_fn",
                    }
                )
                continue

            sent = bool(trigger_fn(reminder))
            if not sent:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder.reminder_id,
                        "userId": reminder.user_id,
                        "processed": False,
                        "skipped": "trigger_failed",
                    }
                )
                continue

            triggered += 1
            patch: Dict[str, Any] = {
                "lastProcessedUTC": _format_datetime(due_time),
                "updatedAt": _format_datetime(now),
            }
            if reminder.repeat in ONE_TIME_REPEATS:
                patch["status"] = "off"
            else:
                tz_name = reminder.timezone or "UTC"
                advanced = reminder_advancement.compute_advance_after_firing(
                    reminder.raw,
                    tz_name,
                    due_occurrence_utc=due_time,
                    now_utc=_as_utc(now),
                )
                if advanced is None:
                    skipped += 1
                    results.append(
                        {
                            "reminderId": reminder.reminder_id,
                            "userId": reminder.user_id,
                            "processed": False,
                            "skipped": "cannot_advance_recurring",
                        }
                    )
                    continue
                next_occurrence, next_trigger = advanced
                patch["nextOccurrenceUTC"] = _format_datetime(next_occurrence)
                patch["nextTriggerUTC"] = _format_datetime(next_trigger)
            doc_client.document(reminder.doc_path).set(patch, merge=True)

            results.append(
                {
                    "reminderId": reminder.reminder_id,
                    "userId": reminder.user_id,
                    "processed": True,
                    "oneTime": reminder.repeat in ONE_TIME_REPEATS,
                    "nextOccurrenceUTC": patch.get("nextOccurrenceUTC"),
                }
            )
        except Exception as exc:
            errors.append({"reminderId": reminder.reminder_id, "error": str(exc)})

    return {
        "ok": True,
        "count": len(reminders),
        "triggered": triggered,
        "skipped": skipped,
        "execute": execute,
        "results": results,
        "errors": errors,
    }


def _resolve_user_timezone(doc, cache: Dict[str, str]) -> str:
    parent = doc.reference.parent.parent
    if parent is None:
        return "UTC"
    user_id = parent.id
    if user_id in cache:
        return cache[user_id]
    try:
        snap = parent.get()
        if not snap.exists:
            cache[user_id] = "UTC"
            return "UTC"
        payload = snap.to_dict() or {}
        timezone_value = (
            payload.get("timezone")
            or payload.get("timeZone")
            or payload.get("timezoneId")
            or payload.get("userTimezone")
            or "UTC"
        )
        timezone_name = str(timezone_value)
        cache[user_id] = timezone_name
        return timezone_name
    except Exception:
        cache[user_id] = "UTC"
        return "UTC"

