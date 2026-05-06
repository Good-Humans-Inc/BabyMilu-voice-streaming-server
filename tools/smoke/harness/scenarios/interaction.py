from __future__ import annotations

import subprocess
from typing import Any

from ..context import ScenarioContext
from ..device_simulator import DeviceSimulator
from ..firestore_api import FirestoreDataAdapter
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


DEFAULT_MAGIC_CAMERA_PROMPT = (
    "Please use your inspect_recent_magic_camera_photo tool to check the recent "
    "Magic Camera photo I just took and tell me what you see."
)

NEGATIVE_RESPONSE_MARKERS = (
    "i can't see",
    "i cannot see",
    "wish i could see",
    "can't directly view",
    "cannot directly view",
    "rely on your descriptions",
    "describe it to me",
    "describe it for me",
    "imagine it",
)


def _conversation_messages(prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "roots": {"listChanged": True},
                        "sampling": {},
                    },
                    "clientInfo": {
                        "name": "XiaozhiClient",
                        "version": "1.0.0",
                    },
                },
            },
        },
        {
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        },
        {
            "type": "listen",
            "state": "detect",
            "text": prompt,
        },
    ]


def _assistant_text(capture) -> str:
    parts: list[str] = []
    for event in capture.llm_events:
        text = str(event.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _contains_negative_marker(text: str) -> str | None:
    lowered = text.lower()
    for marker in NEGATIVE_RESPONSE_MARKERS:
        if marker in lowered:
            return marker
    return None


def _assistant_turn_inserted(log_excerpt: str) -> bool:
    return "speaker=assistant" in log_excerpt and "insert_turn" in log_excerpt


def _fetch_server_log_excerpt(context: ScenarioContext, *, device_id: str) -> str:
    template = (context.environment.server_log_command_template or "").strip()
    if not template:
        return ""
    command = template.format(device_id=device_id)
    completed = subprocess.run(
        command,
        shell=True,
        check=False,
        text=True,
        capture_output=True,
    )
    output = completed.stdout.strip()
    if completed.stderr.strip():
        output = f"{output}\n{completed.stderr.strip()}".strip()
    return output


def _capture_dict(capture) -> dict[str, Any]:
    return {
        "deviceId": capture.device_id,
        "mqttEvents": capture.mqtt_events,
        "ttsEvents": capture.tts_events,
        "llmEvents": capture.llm_events,
        "audioFrameCount": len(capture.audio_frames),
        "wavPath": capture.wav_path,
        "goodbyeEvent": capture.goodbye_event,
    }


class MagicCameraPhotoScenario(BaseScenario):
    name = "interaction.magic_camera_photo"
    description = (
        "Run a websocket conversation that asks for a Magic Camera photo check, "
        "verify a recent photo exists, and assert the assistant does not fall back "
        "to the 'I can't see it' response."
    )

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        args = context.args
        if not args.device_id:
            raise RuntimeError("--device-id is required for interaction.magic_camera_photo")

        adapter = FirestoreDataAdapter(context.firestore)
        recent_photo = adapter.get_recent_magic_photo(uid=args.uid)
        if not recent_photo:
            raise RuntimeError(
                f"No recent Magic Camera photo found for {args.uid} in the past 24 hours"
            )
        photo_path, photo_doc = recent_photo

        simulator = DeviceSimulator(
            mqtt_host=context.environment.mqtt_host,
            ws_url=context.environment.ws_url,
            artifact_dir=context.artifact_dir,
        )
        prompt = args.prompt or DEFAULT_MAGIC_CAMERA_PROMPT
        capture = await simulator.capture_websocket_session(
            device_id=args.device_id,
            timeout_seconds=args.timeout_seconds,
            outbound_messages=_conversation_messages(prompt),
        )

        assistant_text = _assistant_text(capture)
        negative_marker = _contains_negative_marker(assistant_text)
        log_excerpt = _fetch_server_log_excerpt(context, device_id=args.device_id)
        tool_triggered = "inspect_recent_magic_camera_photo" in log_excerpt
        assistant_turn_inserted = _assistant_turn_inserted(log_excerpt)

        details = {
            "user": {"uid": args.uid, "deviceId": args.device_id},
            "prompt": prompt,
            "recentPhotoPath": photo_path,
            "recentPhotoCreatedAt": str(photo_doc.get("createdAt") or ""),
            "assistantText": assistant_text,
            "negativeMarker": negative_marker,
            "toolTriggeredInLogs": tool_triggered,
            "assistantTurnInsertedInLogs": assistant_turn_inserted,
            "serverLogExcerpt": log_excerpt,
            "deviceCapture": _capture_dict(capture),
        }
        context.artifact_writer.write_json("scenario-details.json", details, context.artifact_dir)

        if negative_marker:
            raise RuntimeError(
                f"Assistant fell back to a non-visual response marker: {negative_marker}"
            )
        if not assistant_text and not assistant_turn_inserted:
            raise RuntimeError("No assistant text was captured from the websocket session")
        if context.environment.server_log_command_template and not tool_triggered:
            raise RuntimeError("Server logs did not show inspect_recent_magic_camera_photo")

        summary = "Magic Camera inspection path avoided the fallback non-visual response"
        if tool_triggered:
            summary += " and server logs confirmed the tool call"
        if not assistant_text and assistant_turn_inserted:
            summary += "; assistant turn was confirmed in server logs even though websocket text was not captured"

        return ScenarioResult(
            name=self.name,
            success=True,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=summary,
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
