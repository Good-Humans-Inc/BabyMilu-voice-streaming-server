from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config.settings import get_gcp_credentials_path
from config.logger import setup_logging
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
    upper_bound = now + lookahead
    query = _collection_group(client)
    query = query.where(filter=FieldFilter("status", "==", "on"))
    query = query.where(filter=FieldFilter("nextOccurrenceUTC", "<=", upper_bound))

    docs: List[models.AlarmDoc] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        schedule_payload = data["schedule"]
        repeat = models.AlarmRepeat(str(schedule_payload["repeat"]).lower())
        schedule = models.AlarmSchedule(
            repeat=repeat,
            time_local=schedule_payload["timeLocal"],
            days=schedule_payload.get("days") or [],
        )

        targets = [
            models.AlarmTarget(
                device_id=target["deviceID"],
                mode=target["mode"],
            )
            for target in data["targets"]
        ]

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
    )


def _parse_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _resolve_user_id(doc) -> str:
    parent = doc.reference.parent.parent
    return parent.id if parent else ""


