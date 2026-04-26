from __future__ import annotations

import uuid
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config.settings import get_gcp_credentials_path
from services.logging import setup_logging
from core.utils.mac import normalize_mac
from services.alarms import models

TAG = __name__
logger = setup_logging()

_REPEAT_ALIASES = {
    "daily": models.AlarmRepeat.WEEKLY,
    "weekly": models.AlarmRepeat.WEEKLY,
    "none": models.AlarmRepeat.NONE,
    "once": models.AlarmRepeat.NONE,
    "one_time": models.AlarmRepeat.NONE,
    "one-time": models.AlarmRepeat.NONE,
    "no_repeat": models.AlarmRepeat.NONE,
}


def _build_client() -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        import os
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    else:
        # If no valid credentials found, clear the env var if it points to a directory
        # to prevent "Is a directory" errors from Firestore
        import os
        env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if env_creds and os.path.isdir(env_creds):
            # Directory detected but no JSON file found inside - clear it to avoid errors
            if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    
    return firestore.Client()


def _collection_group(client: firestore.Client):
    return client.collection_group("alarms")


def fetch_due_alarms(
    now: datetime,
    lookahead: timedelta,
    client: Optional[firestore.Client] = None,
) -> List[models.AlarmDoc]:
    client = client or _build_client()
    user_cache: Dict[str, Dict[str, Any]] = {}
    device_cache: Dict[str, List[str]] = {}
    upper_bound = now + lookahead
    upper_bound_str = _format_datetime(upper_bound)
    window_start = _format_datetime(now)
    logger.bind(tag=TAG).debug(
        "Scanning alarms where status='on' and nextOccurrenceUTC <= "
        f"{upper_bound_str} (window start={window_start}, lookahead={lookahead})"
    )
    query = _collection_group(client)
    query = query.where(filter=FieldFilter("status", "==", "on"))
    query = query.where(filter=FieldFilter("nextOccurrenceUTC", "<=", upper_bound_str))

    docs: List[models.AlarmDoc] = []
    for doc in query.stream():
        user_id = _resolve_user_id(doc)
        data = doc.to_dict() or {}
        user_meta = _get_user_metadata(doc, user_cache)
        if user_meta:
            data = dict(data)
            data["user"] = user_meta
        raw_next_occurrence = data.get("nextOccurrenceUTC")
        logger.bind(tag=TAG).debug(
            (
                f"Alarm {doc.reference.path} status={data.get('status')} "
                f"nextOccurrenceUTC={raw_next_occurrence} "
                f"(type={type(raw_next_occurrence).__name__}) "
                f"label={data.get('label')} targets={len(data.get('targets', []))}"
            )
        )
        schedule_payload = data.get("schedule")
        if not isinstance(schedule_payload, dict):
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}): "
                    f"missing or invalid schedule payload ({schedule_payload})"
                )
            )
            continue
        try:
            schedule = _build_schedule(schedule_payload)
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}): "
                    f"invalid schedule payload ({schedule_payload}) ({exc})"
                )
            )
            continue

        targets_payload = data.get("targets")
        try:
            targets = _build_alarm_targets(
                data=data,
                user_id=user_id,
                targets_payload=targets_payload,
                client=client,
                device_cache=device_cache,
            )
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}) "
                    f"due to malformed target payload: {targets_payload} ({exc})"
                )
            )
            continue
        if not targets:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}): "
                    f"targets payload missing or empty ({targets_payload})"
                )
            )
            continue

        docs.append(_build_alarm_doc(doc, data, schedule, targets))
    logger.bind(tag=TAG).info(f"Fetched {len(docs)} due alarms")
    return docs


def _build_alarm_doc(
    doc, data: dict, schedule: models.AlarmSchedule, targets: List[models.AlarmTarget]
) -> models.AlarmDoc:
    user_block = data.get("user") if isinstance(data.get("user"), dict) else {}
    return models.AlarmDoc(
        alarm_id=doc.id,
        user_id=_resolve_user_id(doc),
        uid=data.get("uid"),
        label=data.get("label"),
        context=data.get("context"),
        schedule=schedule,
        status=models.AlarmStatus(str(data["status"]).lower()),
        next_occurrence_utc=_parse_datetime(data["nextOccurrenceUTC"]),
        targets=targets,
        updated_at=data.get("updatedAt"),
        raw=data,
        doc_path=doc.reference.path,
        last_processed_utc=_parse_datetime(data.get("lastProcessedUTC")),
        user_timezone=user_block.get("timezone"),
    )


def _get_user_metadata(
    doc: firestore.DocumentSnapshot,
    cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    parent = doc.reference.parent.parent
    if parent is None:
        return {}
    user_id = parent.id
    if user_id in cache:
        return cache[user_id]
    try:
        snapshot = parent.get()
        if not snapshot.exists:
            cache[user_id] = {}
            return {}
        payload = snapshot.to_dict() or {}
        timezone_value = (
            payload.get("timezone")
            or payload.get("timeZone")
            or payload.get("timezoneId")
            or payload.get("userTimezone")
        )
        meta: Dict[str, Any] = {}
        if timezone_value:
            meta["timezone"] = timezone_value
        cache[user_id] = meta
        return meta
    except Exception as exc:
        logger.bind(tag=TAG).warning(
            f"Failed to load user metadata for {parent.path}: {exc}"
        )
        cache[user_id] = {}
        return {}


def fetch_user_timezone(
    user_id: str,
    client: Optional[firestore.Client] = None,
) -> Optional[str]:
    client = client or _build_client()
    try:
        snapshot = client.collection("users").document(user_id).get()
        if not snapshot.exists:
            return None
        payload = snapshot.to_dict() or {}
        timezone_value = (
            payload.get("timezone")
            or payload.get("timeZone")
            or payload.get("timezoneId")
            or payload.get("userTimezone")
        )
        if not timezone_value:
            return None
        return str(timezone_value).strip() or None
    except Exception as exc:
        logger.bind(tag=TAG).warning(
            f"Failed to fetch timezone for users/{user_id}: {exc}"
        )
        return None


def _parse_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_device_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Alarm target deviceId must be a string")
    normalized = normalize_mac(value)
    compact = normalized.replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise ValueError(f"Alarm target deviceId is not a valid MAC address: {value}")
    return normalized


def _parse_repeat(raw_repeat) -> models.AlarmRepeat:
    key = str(raw_repeat).strip().lower()
    if key in _REPEAT_ALIASES:
        return _REPEAT_ALIASES[key]
    raise ValueError(f"Unsupported repeat value: {raw_repeat}")


def _build_schedule(schedule_payload: Dict[str, Any]) -> models.AlarmSchedule:
    repeat_raw = schedule_payload["repeat"]
    repeat = _parse_repeat(repeat_raw)
    days = schedule_payload.get("days") or []
    if str(repeat_raw).strip().lower() == "daily" and not days:
        days = list(models.DAY_NAMES)
    return models.AlarmSchedule(
        repeat=repeat,
        time_local=schedule_payload["timeLocal"],
        days=days,
    )


def _build_alarm_targets(
    *,
    data: Dict[str, Any],
    user_id: str,
    targets_payload: Any,
    client: firestore.Client,
    device_cache: Dict[str, List[str]],
) -> List[models.AlarmTarget]:
    if isinstance(targets_payload, list) and targets_payload:
        return [
            models.AlarmTarget(
                device_id=_normalize_device_id(target["deviceId"]),
                mode=target.get("mode") or "morning_alarm",
            )
            for target in targets_payload
        ]

    # Legacy alarm docs under users/{uid}/alarms/morning did not store targets.
    # Fallback to all devices currently owned by the user so older alarms still fire.
    legacy_device_ids = _get_user_device_ids(user_id, client=client, cache=device_cache)
    return [
        models.AlarmTarget(device_id=device_id, mode="morning_alarm")
        for device_id in legacy_device_ids
        if device_id
    ]


def _get_user_device_ids(
    user_id: str,
    *,
    client: firestore.Client,
    cache: Dict[str, List[str]],
) -> List[str]:
    if user_id in cache:
        return cache[user_id]
    query = client.collection("devices").where(
        filter=FieldFilter("ownerPhone", "==", user_id)
    )
    device_ids: List[str] = []
    for snapshot in query.stream():
        payload = snapshot.to_dict() or {}
        raw_device_id = payload.get("deviceId") or snapshot.id
        if not isinstance(raw_device_id, str) or not raw_device_id.strip():
            continue
        try:
            device_ids.append(_normalize_device_id(raw_device_id))
        except ValueError:
            continue
    cache[user_id] = device_ids
    return device_ids


def mark_alarm_processed(
    alarm: models.AlarmDoc,
    *,
    last_processed: datetime,
    next_occurrence: datetime,
    client: Optional[firestore.Client] = None,
) -> None:
    if not alarm.doc_path:
        logger.bind(tag=TAG).warning(
            f"Alarm {alarm.alarm_id} missing doc_path; cannot update next occurrence"
        )
        return
    client = client or _build_client()
    doc_ref = client.document(alarm.doc_path)
    now = datetime.now(timezone.utc)
    doc_ref.set(
        {
            "lastProcessedUTC": _format_datetime(last_processed),
            "nextOccurrenceUTC": _format_datetime(next_occurrence),
            "updatedAt": _format_datetime(now),
        },
        merge=True,
    )


def _resolve_user_id(doc) -> str:
    parent = doc.reference.parent.parent
    return parent.id if parent else ""


def create_alarm(
    uid: str,
    device_id: str,
    resolved_dt: datetime,
    label: str,
    context: str,
    tz_str: str,
    client: Optional[firestore.Client] = None,
) -> str:
    """Write a one-time alarm doc to /users/{uid}/alarms/{alarm_id} and return the alarm_id.

    Args:
        uid: The user's document ID (ownerPhone).
        device_id: The device MAC address that should ring.
        resolved_dt: Timezone-aware absolute datetime for the alarm.
        label: Short human-readable name (e.g. "take vitamins").
        context: Full reason/purpose used to customize the alarm conversation.
        tz_str: IANA timezone string (e.g. "America/Los_Angeles").
    """
    client = client or _build_client()
    alarm_id = str(uuid.uuid4())

    resolved_local = resolved_dt.astimezone(ZoneInfo(tz_str))
    time_local = resolved_local.strftime("%H:%M")
    date_local = resolved_local.strftime("%Y-%m-%d")

    now_utc = datetime.now(timezone.utc)
    doc = {
        "label": label,
        "context": context,
        "schedule": {
            # Keep "once" for backward compatibility with currently deployed
            # cloud scheduler revisions that may not recognize "none".
            "repeat": "once",
            "timeLocal": time_local,
            "days": [date_local],  # ISO date string for one-time alarms
        },
        "nextOccurrenceUTC": _format_datetime(resolved_dt),
        "status": models.AlarmStatus.ON.value,
        "targets": [{"deviceId": device_id, "mode": "morning_alarm"}],
        "uid": uid,
        "source": "voice",
        "createdAt": _format_datetime(now_utc),
        "updatedAt": _format_datetime(now_utc),
    }

    client.collection("users").document(uid).collection("alarms").document(alarm_id).set(doc)
    logger.bind(tag=TAG).info(
        f"Created one-time alarm {alarm_id} for user {uid} device {device_id} "
        f"at {_format_datetime(resolved_dt)} (local {time_local} {tz_str}): '{label}'"
    )
    return alarm_id


# ---------------------------------------------------------------------------
# Reminder functions  (collection: users/{uid}/reminders/{id})
# ---------------------------------------------------------------------------

def create_reminder(
    uid: str,
    device_id: str,
    resolved_dt: datetime,
    label: str,
    context: str,
    tz_str: str,
    delivery_channels: Optional[List[str]] = None,
    client: Optional[firestore.Client] = None,
) -> str:
    """Write a one-time reminder doc to /users/{uid}/reminders/{reminder_id}.

    Separate from create_alarm() — reminders live in the 'reminders' subcollection,
    carry a deliveryChannel array, and use mode='reminder' (single voice message, no follow-ups).

    Args:
        uid: The user's document ID (ownerPhone).
        device_id: The device MAC address that should deliver the reminder.
        resolved_dt: Timezone-aware absolute datetime for the reminder.
        label: Short human-readable name (e.g. "drink water").
        context: Full reason/purpose used to customise the reminder conversation.
        tz_str: IANA timezone string (e.g. "America/Los_Angeles").
        delivery_channels: List of delivery channels. Defaults to ["plushie"].
    """
    client = client or _build_client()
    reminder_id = str(uuid.uuid4())

    resolved_local = resolved_dt.astimezone(ZoneInfo(tz_str))
    time_local = resolved_local.strftime("%H:%M")
    date_local = resolved_local.strftime("%Y-%m-%d")

    now_utc = datetime.now(timezone.utc)
    doc = {
        "label": label,
        "context": context,
        "schedule": {
            "repeat": "none",
            "timeLocal": time_local,
            "dateLocal": date_local,
            "days": [date_local],
        },
        "nextOccurrenceUTC": _format_datetime(resolved_dt),
        "status": models.AlarmStatus.ON.value,
        "targets": [{"deviceId": device_id, "mode": "reminder"}],
        "uid": uid,
        "source": "voice",
        "deliveryChannel": delivery_channels if delivery_channels is not None else ["plushie"],
        "createdAt": _format_datetime(now_utc),
        "updatedAt": _format_datetime(now_utc),
    }

    client.collection("users").document(uid).collection("reminders").document(reminder_id).set(doc)
    logger.bind(tag=TAG).info(
        f"Created reminder {reminder_id} for user {uid} device {device_id} "
        f"at {_format_datetime(resolved_dt)} (local {time_local} {tz_str}): '{label}'"
    )
    return reminder_id


def fetch_due_reminders(
    now: datetime,
    lookahead: timedelta,
    client: Optional[firestore.Client] = None,
) -> List[models.AlarmDoc]:
    """Fetch due reminder docs from the 'reminders' collection group.

    Only returns reminders that include "plushie" in their deliveryChannel array,
    so app-only reminders are never sent to the plushie.
    """
    client = client or _build_client()
    user_cache: Dict[str, Dict[str, Any]] = {}
    upper_bound = now + lookahead
    upper_bound_str = _format_datetime(upper_bound)
    logger.bind(tag=TAG).debug(
        "Scanning reminders where status='on', nextOccurrenceUTC <= "
        f"{upper_bound_str}, deliveryChannel array_contains 'plushie'"
    )

    query = client.collection_group("reminders")
    query = query.where(filter=FieldFilter("status", "==", "on"))
    query = query.where(filter=FieldFilter("nextOccurrenceUTC", "<=", upper_bound_str))
    query = query.where(filter=FieldFilter("deliveryChannel", "array_contains", "plushie"))

    docs: List[models.AlarmDoc] = []
    for doc in query.stream():
        user_id = _resolve_user_id(doc)
        data = doc.to_dict() or {}
        user_meta = _get_user_metadata(doc, user_cache)
        if user_meta:
            data = dict(data)
            data["user"] = user_meta

        schedule_payload = data.get("schedule")
        if not isinstance(schedule_payload, dict):
            logger.bind(tag=TAG).warning(
                f"Skipping reminder {doc.reference.path} (user={user_id}): "
                f"missing or invalid schedule payload ({schedule_payload})"
            )
            continue
        try:
            repeat = _parse_repeat(schedule_payload["repeat"])
            schedule = models.AlarmSchedule(
                repeat=repeat,
                time_local=schedule_payload["timeLocal"],
                days=schedule_payload.get("days") or [],
            )
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                f"Skipping reminder {doc.reference.path} (user={user_id}): "
                f"invalid schedule payload ({schedule_payload}) ({exc})"
            )
            continue

        targets_payload = data.get("targets")
        if not isinstance(targets_payload, list) or not targets_payload:
            logger.bind(tag=TAG).warning(
                f"Skipping reminder {doc.reference.path} (user={user_id}): "
                f"targets payload missing or empty ({targets_payload})"
            )
            continue
        try:
            targets = [
                models.AlarmTarget(
                    device_id=_normalize_device_id(t["deviceId"]),
                    mode=t["mode"],
                )
                for t in targets_payload
            ]
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                f"Skipping reminder {doc.reference.path} (user={user_id}) "
                f"due to malformed target payload: {targets_payload} ({exc})"
            )
            continue

        docs.append(_build_alarm_doc(doc, data, schedule, targets))

    logger.bind(tag=TAG).info(f"Fetched {len(docs)} due reminders")
    return docs


def mark_one_time_alarm_complete(
    alarm: models.AlarmDoc,
    *,
    last_processed: datetime,
    client: Optional[firestore.Client] = None,
) -> None:
    """Turn off a one-time alarm after it fires (sets status=off, records lastProcessedUTC)."""
    if not alarm.doc_path:
        logger.bind(tag=TAG).warning(
            f"Alarm {alarm.alarm_id} missing doc_path; cannot mark complete"
        )
        return
    client = client or _build_client()
    doc_ref = client.document(alarm.doc_path)
    now = datetime.now(timezone.utc)
    doc_ref.set(
        {
            "status": models.AlarmStatus.OFF.value,
            "lastProcessedUTC": _format_datetime(last_processed),
            "updatedAt": _format_datetime(now),
        },
        merge=True,
    )
    logger.bind(tag=TAG).info(
        f"One-time alarm {alarm.alarm_id} (user={alarm.user_id}) marked complete/off"
    )
