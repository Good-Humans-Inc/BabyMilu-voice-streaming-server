from __future__ import annotations

import uuid
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
            repeat = _parse_repeat(schedule_payload["repeat"])
            schedule = models.AlarmSchedule(
                repeat=repeat,
                time_local=schedule_payload["timeLocal"],
                days=schedule_payload.get("days") or [],
            )
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}): "
                    f"invalid schedule payload ({schedule_payload}) ({exc})"
                )
            )
            continue

        targets_payload = data.get("targets")
        if not isinstance(targets_payload, list) or not targets_payload:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}): "
                    f"targets payload missing or empty ({targets_payload})"
                )
            )
            continue
        try:
            targets = [
                models.AlarmTarget(
                    device_id=_normalize_device_id(target["deviceId"]),
                    mode=target["mode"],
                )
                for target in targets_payload
            ]
        except (KeyError, ValueError) as exc:
            logger.bind(tag=TAG).warning(
                (
                    f"Skipping alarm {doc.reference.path} (user={user_id}) "
                    f"due to malformed target payload: {targets_payload} ({exc})"
                )
            )
            continue

        docs.append(_build_alarm_doc(doc, data, schedule, targets))
    logger.bind(tag=TAG).info(f"Fetched {len(docs)} due alarms")
    return docs


def _build_alarm_doc(
    doc, data: dict, schedule: models.AlarmSchedule, targets: List[models.AlarmTarget]
) -> models.AlarmDoc:
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
        content=data.get("content"),
        type_hint=data.get("typeHint"),
        priority=data.get("priority"),
        conversation_outline=data.get("conversationOutline"),
        character_reminder=data.get("characterReminder"),
        emotional_context=data.get("emotionalContext"),
        completion_signal=data.get("completionSignal"),
        delivery_preference=data.get("deliveryPreference"),
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
    return normalize_mac(value)


def _parse_repeat(raw_repeat) -> models.AlarmRepeat:
    key = str(raw_repeat).strip().lower()
    if key in _REPEAT_ALIASES:
        return _REPEAT_ALIASES[key]
    raise ValueError(f"Unsupported repeat value: {raw_repeat}")


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


def create_scheduled_conversation(
    uid: str,
    device_id: str,
    resolved_dt: datetime,
    label: str,
    context: str,
    tz_str: str,
    *,
    content: Optional[str] = None,
    type_hint: Optional[str] = None,
    priority: Optional[str] = None,
    conversation_outline: Optional[str] = None,
    character_reminder: Optional[str] = None,
    emotional_context: Optional[str] = None,
    completion_signal: Optional[str] = None,
    delivery_preference: Optional[str] = None,
    client: Optional[firestore.Client] = None,
) -> str:
    """Write a scheduled-conversation alarm doc to /users/{uid}/alarms/{alarm_id}.

    Like create_alarm() but uses mode='scheduled_conversation' and stores the
    LLM-generated intake fields (outline, character reminder, emotional context, etc.)
    that are assembled into dynamic instructions at delivery time.

    Returns the alarm_id UUID string.
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
            "repeat": "once",
            "timeLocal": time_local,
            "days": [date_local],
        },
        "nextOccurrenceUTC": _format_datetime(resolved_dt),
        "status": models.AlarmStatus.ON.value,
        "targets": [{"deviceId": device_id, "mode": "scheduled_conversation"}],
        "uid": uid,
        "source": "voice",
        "createdAt": _format_datetime(now_utc),
        "updatedAt": _format_datetime(now_utc),
        # V0 scheduled_conversation fields
        "content": content or label,
        "typeHint": type_hint,
        "priority": priority,
        "conversationOutline": conversation_outline,
        "characterReminder": character_reminder,
        "emotionalContext": emotional_context,
        "completionSignal": completion_signal,
        "deliveryPreference": delivery_preference,
    }

    client.collection("users").document(uid).collection("alarms").document(alarm_id).set(doc)
    logger.bind(tag=TAG).info(
        f"Created scheduled conversation {alarm_id} for user {uid} device {device_id} "
        f"at {_format_datetime(resolved_dt)} (local {time_local} {tz_str}): '{label}'"
    )
    return alarm_id


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


