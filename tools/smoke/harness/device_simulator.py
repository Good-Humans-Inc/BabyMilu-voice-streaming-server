from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import paho.mqtt.client as mqtt
import websockets

from .models import DeviceCapture


class DeviceSimulator:
    def __init__(self, *, mqtt_host: str, ws_url: str, artifact_dir: Path) -> None:
        self.mqtt_host = mqtt_host
        self.ws_url = ws_url
        self.artifact_dir = artifact_dir

    async def capture_after_ws_start(
        self,
        *,
        device_id: str,
        timeout_seconds: int,
        outbound_messages: list[dict[str, Any]] | None = None,
    ) -> DeviceCapture:
        capture = DeviceCapture(device_id=device_id)
        mqtt_queue: Queue[dict[str, Any]] = Queue()
        mqtt_client = mqtt.Client(client_id=f"codex-smoke-{uuid.uuid4().hex[:8]}")

        def on_connect(client, userdata, flags, rc):
            client.subscribe(f"xiaozhi/{device_id}/down")

        def on_message(client, userdata, msg):
            mqtt_queue.put(
                {
                    "ts": time.time(),
                    "topic": msg.topic,
                    "payload": msg.payload.decode("utf-8", errors="replace"),
                }
            )

        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.connect(self.mqtt_host, 1883, 60)
        mqtt_client.loop_start()
        try:
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                try:
                    event = mqtt_queue.get(timeout=1)
                except Empty:
                    await asyncio.sleep(0)
                    continue
                capture.mqtt_events.append(event)
                if "ws_start" in event["payload"]:
                    break
            else:
                raise RuntimeError(f"Timed out waiting for ws_start for {device_id}")

            client_id = f"codex-smoke-device-{uuid.uuid4().hex[:8]}"
            async with websockets.connect(
                f"{self.ws_url}?device-id={device_id}&client-id={client_id}"
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "device_id": device_id,
                            "device_name": "Codex Shared Smoke Device",
                            "device_mac": device_id,
                            "token": "codex-smoke-token",
                            "features": {"mcp": True},
                        }
                    )
                )
                if outbound_messages:
                    for outbound in outbound_messages:
                        await websocket.send(json.dumps(outbound))
                started = time.time()
                while time.time() - started < timeout_seconds:
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(raw, bytes):
                        capture.audio_frames.append(raw)
                        continue
                    try:
                        message = json.loads(raw)
                    except Exception:
                        continue
                    if message.get("type") == "tts":
                        capture.tts_events.append(message)
                    elif message.get("type") == "llm":
                        capture.llm_events.append(message)
                    elif message.get("type") == "goodbye":
                        capture.goodbye_event = message
                        break
        finally:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

        wav_bytes = _maybe_decode_wav(capture.audio_frames)
        if wav_bytes:
            wav_path = self.artifact_dir / f"{device_id.replace(':', '-')}.wav"
            wav_path.write_bytes(wav_bytes)
            capture.wav_path = str(wav_path)

        return capture


def _maybe_decode_wav(audio_frames: list[bytes]) -> bytes | None:
    if not audio_frames:
        return None
    repo_root = Path(__file__).resolve().parents[3]
    server_root = repo_root / "main" / "xiaozhi-server"
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))
    try:
        from core.utils.util import opus_datas_to_wav_bytes
    except Exception:
        return None
    try:
        return opus_datas_to_wav_bytes(audio_frames)
    except Exception:
        return None
