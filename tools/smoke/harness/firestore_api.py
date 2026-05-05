from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from google.cloud import firestore


WEEKDAY_CODES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class CreatedDoc:
    path: str
    doc_id: str
    payload: dict


class FirestoreDataAdapter:
    def __init__(self, client: firestore.Client) -> None:
        self.client = client

    def get_user(self, uid: str) -> dict | None:
        snapshot = self.client.collection("users").document(uid).get()
        return snapshot.to_dict() if snapshot.exists else None

    def delete_path(self, path: str) -> None:
        parts = path.split("/")
        ref = self.client
        if len(parts) % 2 != 0:
            raise ValueError(f"Invalid document path: {path}")
        doc_ref = self.client.collection(parts[0]).document(parts[1])
        index = 2
        while index < len(parts):
            doc_ref = doc_ref.collection(parts[index]).document(parts[index + 1])
            index += 2
        doc_ref.delete()

    def get_document(self, path: str) -> dict | None:
        parts = path.split("/")
        if len(parts) % 2 != 0:
            raise ValueError(f"Invalid document path: {path}")
        doc_ref = self.client.collection(parts[0]).document(parts[1])
        index = 2
        while index < len(parts):
            doc_ref = doc_ref.collection(parts[index]).document(parts[index + 1])
            index += 2
        snapshot = doc_ref.get()
        return snapshot.to_dict() if snapshot.exists else None

    def create_alarm(
        self,
        *,
        uid: str,
        device_id: str,
        label: str,
        due_utc: datetime,
        repeat: str,
        user_timezone: str,
    ) -> CreatedDoc:
        local_due = due_utc.astimezone(ZoneInfo(user_timezone))
        alarm_id = f"smoke-alarm-{uuid.uuid4().hex[:10]}"
        payload = {
            "label": label,
            "status": "on",
            "nextOccurrenceUTC": due_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "schedule": {
                "timeLocal": local_due.strftime("%H:%M"),
                "repeat": repeat,
                "days": [WEEKDAY_CODES[local_due.weekday()]] if repeat == "weekly" else [],
            },
            "targets": [
                {
                    "deviceId": device_id,
                    "mode": "morning_alarm",
                }
            ],
            "userId": uid,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        path = f"users/{uid}/alarms/{alarm_id}"
        self.client.collection("users").document(uid).collection("alarms").document(alarm_id).set(
            payload,
            merge=True,
        )
        return CreatedDoc(path=path, doc_id=alarm_id, payload=payload)

    def create_reminder(
        self,
        *,
        uid: str,
        device_id: str | None,
        label: str,
        due_utc: datetime,
        repeat: str,
        user_timezone: str,
        channel: str,
    ) -> CreatedDoc:
        local_due = due_utc.astimezone(ZoneInfo(user_timezone))
        reminder_id = f"smoke-reminder-{uuid.uuid4().hex[:10]}"
        channels = {
            "app": ["app"],
            "plushie": ["plushie"],
            "both": ["app", "plushie"],
        }[channel]
        targets = []
        if "plushie" in channels and device_id:
            targets = [
                {
                    "deviceId": device_id,
                    "mode": "reminder",
                }
            ]
        schedule = {
            "timeLocal": local_due.strftime("%H:%M"),
            "repeat": repeat,
        }
        if repeat == "none":
            schedule["dateLocal"] = local_due.date().isoformat()
        else:
            schedule["days"] = [WEEKDAY_CODES[local_due.weekday()]]
        payload = {
            "label": label,
            "uid": uid,
            "status": "on",
            "deliveryChannel": channels,
            "targets": targets,
            "schedule": schedule,
            "nextOccurrenceUTC": due_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        path = f"users/{uid}/reminders/{reminder_id}"
        self.client.collection("users").document(uid).collection("reminders").document(reminder_id).set(
            payload,
            merge=True,
        )
        return CreatedDoc(path=path, doc_id=reminder_id, payload=payload)
