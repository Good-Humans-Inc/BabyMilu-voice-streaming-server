from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ..context import ScenarioContext
from ..device_simulator import DeviceSimulator
from ..firestore_api import FirestoreDataAdapter
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario
from ..trigger import trigger_scheduler


async def _wait_for(predicate, timeout_seconds: int, interval_seconds: float = 1.0):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    while datetime.now(timezone.utc) < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval_seconds)
    return None


class ScheduledReminderScenario(BaseScenario):
    name = "scheduled.reminder"
    description = "Smoke test reminder creation, scheduler trigger, app marker, and optional plushie stream"

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        args = context.args
        adapter = FirestoreDataAdapter(context.firestore)
        user = adapter.get_user(args.uid)
        if not user:
            raise RuntimeError(f"User {args.uid} does not exist")

        timezone_name = user.get("timezone") or context.environment.default_timezone
        label = args.label or f"smoke reminder {datetime.now().strftime('%H%M%S')}"
        due_utc = datetime.now(timezone.utc) - timedelta(seconds=max(args.lead_seconds, 1))
        created = adapter.create_reminder(
            uid=args.uid,
            device_id=args.device_id,
            label=label,
            due_utc=due_utc,
            repeat=args.repeat,
            user_timezone=timezone_name,
            channel=args.channel,
        )

        capture_task = None
        if args.channel in {"plushie", "both"}:
            if not args.device_id:
                raise RuntimeError("--device-id is required for plushie reminder scenarios")
            simulator = DeviceSimulator(
                mqtt_host=context.environment.mqtt_host,
                ws_url=context.environment.ws_url,
                artifact_dir=context.artifact_dir,
            )
            capture_task = asyncio.create_task(
                simulator.capture_after_ws_start(
                    device_id=args.device_id,
                    timeout_seconds=args.timeout_seconds,
                )
            )

        scheduler_response = None
        try:
            if args.trigger_mode == "invoke-function":
                scheduler_response = trigger_scheduler(context.environment)

            final_doc = await _wait_for(
                lambda: _reminder_ready(adapter, created.path, args.channel, args.repeat),
                timeout_seconds=args.timeout_seconds,
            )
            if not final_doc:
                raise RuntimeError("Reminder did not reach the expected final state in time")

            capture = await capture_task if capture_task else None
            details = {
                "user": {
                    "uid": args.uid,
                    "timezone": timezone_name,
                },
                "created": created.payload,
                "path": created.path,
                "schedulerResponse": scheduler_response,
                "finalDoc": final_doc,
                "deviceCapture": _capture_dict(capture),
            }
            context.artifact_writer.write_json("scenario-details.json", details, context.artifact_dir)
            summary = f"Reminder {created.doc_id} reached final state for channel={args.channel} repeat={args.repeat}"
            return ScenarioResult(
                name=self.name,
                success=True,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=summary,
                artifact_dir=str(context.artifact_dir),
                details=details,
            )
        finally:
            if not args.keep_docs:
                adapter.delete_path(created.path)
                if args.device_id:
                    adapter.delete_path(f"sessionContexts/{args.device_id.lower()}")


class ScheduledAlarmScenario(BaseScenario):
    name = "scheduled.alarm"
    description = "Smoke test alarm wake-up, websocket capture, and recurring advancement"

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        args = context.args
        if not args.device_id:
            raise RuntimeError("--device-id is required for alarm scenarios")
        adapter = FirestoreDataAdapter(context.firestore)
        user = adapter.get_user(args.uid)
        if not user:
            raise RuntimeError(f"User {args.uid} does not exist")

        timezone_name = user.get("timezone") or context.environment.default_timezone
        label = args.label or f"smoke alarm {datetime.now().strftime('%H%M%S')}"
        due_utc = datetime.now(timezone.utc) - timedelta(seconds=max(args.lead_seconds, 1))
        created = adapter.create_alarm(
            uid=args.uid,
            device_id=args.device_id,
            label=label,
            due_utc=due_utc,
            repeat=args.repeat,
            user_timezone=timezone_name,
        )

        simulator = DeviceSimulator(
            mqtt_host=context.environment.mqtt_host,
            ws_url=context.environment.ws_url,
            artifact_dir=context.artifact_dir,
        )
        capture_task = asyncio.create_task(
            simulator.capture_after_ws_start(
                device_id=args.device_id,
                timeout_seconds=args.timeout_seconds,
            )
        )

        scheduler_response = None
        try:
            if args.trigger_mode == "invoke-function":
                scheduler_response = trigger_scheduler(context.environment)

            final_doc = await _wait_for(
                lambda: _alarm_ready(adapter, created.path, args.repeat),
                timeout_seconds=args.timeout_seconds,
            )
            if not final_doc:
                raise RuntimeError("Alarm did not reach the expected final state in time")

            capture = await capture_task
            details = {
                "user": {
                    "uid": args.uid,
                    "timezone": timezone_name,
                },
                "created": created.payload,
                "path": created.path,
                "schedulerResponse": scheduler_response,
                "finalDoc": final_doc,
                "deviceCapture": _capture_dict(capture),
            }
            context.artifact_writer.write_json("scenario-details.json", details, context.artifact_dir)
            summary = f"Alarm {created.doc_id} reached final state repeat={args.repeat}"
            return ScenarioResult(
                name=self.name,
                success=True,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=summary,
                artifact_dir=str(context.artifact_dir),
                details=details,
            )
        finally:
            if not args.keep_docs:
                adapter.delete_path(created.path)
                adapter.delete_path(f"sessionContexts/{args.device_id.lower()}")


def _reminder_ready(
    adapter: FirestoreDataAdapter,
    path: str,
    channel: str,
    repeat: str,
) -> dict | None:
    doc = adapter.get_document(path)
    if not doc:
        return None
    last_delivered = doc.get("lastDelivered", {})
    app_ready = channel not in {"app", "both"} or bool((last_delivered.get("app") or {}).get("at"))
    plushie_ready = channel not in {"plushie", "both"} or bool((last_delivered.get("plushie") or {}).get("at"))
    if repeat == "none":
        final_state = doc.get("status") == "off"
    else:
        final_state = doc.get("status") == "on" and doc.get("nextOccurrenceUTC") != doc.get("lastDelivered", {}).get("occurrenceUTC")
    return doc if app_ready and plushie_ready and final_state else None


def _alarm_ready(adapter: FirestoreDataAdapter, path: str, repeat: str) -> dict | None:
    doc = adapter.get_document(path)
    if not doc:
        return None
    if not doc.get("lastProcessedUTC"):
        return None
    if repeat == "none":
        return doc if doc.get("status") == "off" else None
    return doc if doc.get("nextOccurrenceUTC") and doc.get("status") == "on" else None


def _capture_dict(capture) -> dict | None:
    if capture is None:
        return None
    return {
        "deviceId": capture.device_id,
        "mqttEvents": capture.mqtt_events,
        "ttsEvents": capture.tts_events,
        "llmEvents": capture.llm_events,
        "audioFrameCount": len(capture.audio_frames),
        "wavPath": capture.wav_path,
        "goodbyeEvent": capture.goodbye_event,
    }
