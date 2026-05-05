from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from google.cloud import firestore
from google.api_core import exceptions as gcloud_exceptions

from services.alarms import reminder_push_job, scheduler
from services.alarms.config import ALARM_TIMING
from core.utils.mac import normalize_mac


def _ensure_firestore() -> firestore.Client:
    try:
        return firestore.Client()
    except Exception:  # pragma: no cover - only triggers when creds missing
        pytest.skip("Firestore client unavailable (credentials missing)")


@pytest.mark.integration
def test_firestore_prepare_wake_smoke():
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and not os.environ.get(
        "FIRESTORE_EMULATOR_HOST"
    ):
        pytest.skip("Set GOOGLE_APPLICATION_CREDENTIALS or FIRESTORE_EMULATOR_HOST")

    client = _ensure_firestore()

    user_id = f"test-user-{uuid.uuid4().hex[:6]}"
    alarm_id = f"alarm-{uuid.uuid4().hex[:6]}"
    device_id = uuid.uuid4().hex[:12]
    normalized_device_id = normalize_mac(device_id)

    alarm_ref = (
        client.collection("users").document(user_id).collection("alarms").document(alarm_id)
    )
    alarm_payload = {
        "status": "on",
        "label": "Smoke Test Alarm",
        "nextOccurrenceUTC": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "schedule": {
            "repeat": "weekly",
            "timeLocal": "07:00",
            "days": ["Sun", "Mon"],
        },
        "targets": [
            {
                "deviceId": device_id,
                "mode": "morning_alarm",
            }
        ],
    }
    alarm_ref.set(alarm_payload, merge=True)
    print(f"[smoke] inserted alarm at users/{user_id}/alarms/{alarm_id} for device {device_id}")

    try:
        now = datetime.now(timezone.utc)
        try:
            wake_requests = scheduler.prepare_wake_requests(
                now, lookahead=ALARM_TIMING["lookahead"]
            )
        except gcloud_exceptions.FailedPrecondition as exc:
            pytest.skip(f"Firestore index missing for query: {exc.message}")
        print(f"[smoke] scheduler returned {len(wake_requests)} requests")
        matching = [
            req for req in wake_requests if req.target.device_id == normalized_device_id
        ]
        print(f"[smoke] matching requests: {[req.target.device_id for req in matching]}")
        assert matching, "Expected at least one wake request for smoke device"

        session_doc = (
            client.collection("sessionContexts").document(normalized_device_id).get()
        )
        assert session_doc.exists, "sessionContexts doc should exist after scheduler runs"
        session_data = session_doc.to_dict()
        assert session_data["sessionConfig"]["mode"] == "morning_alarm"
        assert session_data["sessionConfig"]["alarmId"] == alarm_id
    finally:
        alarm_ref.delete()
        client.collection("sessionContexts").document(normalized_device_id).delete()
        client.collection("users").document(user_id).delete()


@pytest.mark.integration
def test_firestore_prepare_reminder_push_smoke(monkeypatch):
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and not os.environ.get(
        "FIRESTORE_EMULATOR_HOST"
    ):
        pytest.skip("Set GOOGLE_APPLICATION_CREDENTIALS or FIRESTORE_EMULATOR_HOST")

    client = _ensure_firestore()

    user_id = f"test-user-{uuid.uuid4().hex[:6]}"
    reminder_id = f"reminder-{uuid.uuid4().hex[:6]}"
    device_id = uuid.uuid4().hex[:12]
    normalized_device_id = normalize_mac(device_id)

    user_ref = client.collection("users").document(user_id)
    user_ref.set({"name": "Smoke", "timezone": "America/Los_Angeles"}, merge=True)

    reminder_ref = (
        client.collection("users").document(user_id).collection("reminders").document(reminder_id)
    )
    reminder_payload = {
        "status": "on",
        "label": "Smoke Test Reminder",
        "context": "Smoke Test Reminder",
        "nextOccurrenceUTC": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "schedule": {
            "repeat": "none",
            "timeLocal": "07:00",
            "dateLocal": datetime.now(timezone.utc).date().isoformat(),
            "days": [datetime.now(timezone.utc).date().isoformat()],
        },
        "targets": [
            {
                "deviceId": device_id,
                "mode": "reminder",
            }
        ],
        "deliveryChannel": ["plushie"],
    }
    reminder_ref.set(reminder_payload, merge=True)
    print(f"[smoke] inserted reminder at users/{user_id}/reminders/{reminder_id} for device {device_id}")

    try:
        monkeypatch.setattr(reminder_push_job, "get_ai_message", lambda **kwargs: "msg")
        plushie_calls = []
        monkeypatch.setattr(
            reminder_push_job,
            "_send_plushie_notification",
            lambda **kwargs: plushie_calls.append(kwargs) or True,
        )

        try:
            result = reminder_push_job.run_send_reminder_push_job(
                execute=True,
                now=datetime.now(timezone.utc),
                client=client,
            )
        except gcloud_exceptions.FailedPrecondition as exc:
            pytest.skip(f"Firestore index missing for query: {exc.message}")

        assert result["triggered"] == 1
        assert plushie_calls, "Expected plushie delivery for smoke reminder"
        assert plushie_calls[0]["reminder_id"] == reminder_id
        assert plushie_calls[0]["reminder_data"]["deliveryChannel"] == ["plushie"]
    finally:
        reminder_ref.delete()
        user_ref.delete()
