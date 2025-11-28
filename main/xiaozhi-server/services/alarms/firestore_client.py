from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config.settings import get_gcp_credentials_path
from services.logging import setup_logging
from services.alarms import models

TAG = __name__
logger = setup_logging()


def _build_client() -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        import os

        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)
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
        schedule_payload = data["schedule"]
        repeat = models.AlarmRepeat(str(schedule_payload["repeat"]).lower())
        schedule = models.AlarmSchedule(
            repeat=repeat,
            time_local=schedule_payload["timeLocal"],
            days=schedule_payload.get("days") or [],
        )

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
        schedule=schedule,
        status=models.AlarmStatus(str(data["status"]).lower()),
        next_occurrence_utc=_parse_datetime(data["nextOccurrenceUTC"]),
        targets=targets,
        updated_at=data.get("updatedAt"),
        raw=data,
        doc_path=doc.reference.path,
        last_processed_utc=_parse_datetime(data.get("lastProcessedUTC")),
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
    return value.lower()


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


