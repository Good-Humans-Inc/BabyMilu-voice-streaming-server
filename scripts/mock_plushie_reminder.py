#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import websockets
from google.cloud import firestore


PROJECT = "composed-augury-469200-g6"
DEFAULT_UID = "+11551551551"
DEFAULT_DEVICE = "90:e5:b1:d6:f8:58"
DEFAULT_BROKER_HOST = "34.30.176.148"
DEFAULT_BROKER_PORT = 1883
DEFAULT_WS_URL = "ws://34.30.176.148:8000/xiaozhi/v1/"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    import sys

    xiaozhi_server_root = _repo_root() / "main" / "xiaozhi-server"
    if str(xiaozhi_server_root) not in sys.path:
        sys.path.insert(0, str(xiaozhi_server_root))


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _save_wav(path: Path, wav_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes)


def _save_text(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _play_audio(path: Path) -> None:
    player = None
    for candidate in ("afplay", "ffplay"):
        if shutil.which(candidate):
            player = candidate
            break
    if player is None:
        return
    if player == "afplay":
        subprocess.Popen([player, str(path)])
    else:
        subprocess.Popen([player, "-nodisp", "-autoexit", str(path)])


def _build_recurring_payload(*, label: str, due_utc: datetime, user_timezone: str, device_id: str) -> dict:
    local_due = due_utc.astimezone(ZoneInfo(user_timezone))
    now = datetime.now(timezone.utc)
    now_iso = _utc_iso(now)
    due_iso = _utc_iso(due_utc)
    return {
        "label": label,
        "status": "on",
        "deliveryChannel": ["app", "plushie"],
        "schedule": {
            "timeLocal": local_due.strftime("%H:%M"),
            "repeat": "daily",
        },
        "nextOccurrenceUTC": due_iso,
        "nextTriggerUTC": due_iso,
        "targets": [{"deviceId": device_id, "mode": "reminder"}],
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "memory_status": "pending",
        "memory_updated_at": now_iso,
    }


def _load_user(client: firestore.Client, uid: str) -> dict:
    data = client.collection("users").document(uid).get().to_dict() or {}
    if not data:
        raise RuntimeError(f"User {uid} not found")
    return data


async def _collect_session(
    *,
    ws_url: str,
    device_id: str,
    mqtt_queue: Queue,
    result: dict,
) -> None:
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            evt = mqtt_queue.get(timeout=1)
        except Empty:
            continue
        result["mqtt"].append(evt)
        if "ws_start" in evt["payload"]:
            break
    else:
        raise RuntimeError("Timed out waiting for ws_start over MQTT")

    client_id = f"codex-mock-{uuid.uuid4().hex[:8]}"
    async with websockets.connect(f"{ws_url}?device-id={device_id}&client-id={client_id}") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "device_id": device_id,
                    "device_name": "Codex Mock Device",
                    "device_mac": device_id,
                    "token": "your-token1",
                    "features": {"mcp": True},
                }
            )
        )
        started = time.time()
        while time.time() - started < 35:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break

            if isinstance(raw, bytes):
                result["audio_frames"].append(raw)
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                continue

            msg_type = msg.get("type")
            if msg_type == "tts":
                result["tts"].append(msg)
            elif msg_type == "llm":
                result["llm"].append(msg)
            elif msg_type == "goodbye":
                result["goodbye"] = msg
                break


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock a plushie reminder session against staging.")
    parser.add_argument("--uid", default=DEFAULT_UID)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE)
    parser.add_argument(
        "--label",
        default="follow up on that scheduler test later",
        help="Reminder label to simulate as if created from app.",
    )
    parser.add_argument("--output-dir", default=str(_repo_root() / "artifacts" / "mock-plushie"))
    parser.add_argument("--play-audio", action="store_true")
    parser.add_argument("--broker-host", default=DEFAULT_BROKER_HOST)
    parser.add_argument("--broker-port", type=int, default=DEFAULT_BROKER_PORT)
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL)
    parser.add_argument("--lead-seconds", type=int, default=75)
    args = parser.parse_args()

    _ensure_import_path()
    from core.utils.util import opus_datas_to_wav_bytes
    client = firestore.Client(project=PROJECT)
    user = _load_user(client, args.uid)
    user_timezone = user.get("timezone") or "UTC"

    user_ref = client.collection("users").document(args.uid)
    reminder_ref = user_ref.collection("reminders").document()
    session_ref = client.collection("sessionContexts").document(args.device_id)

    try:
        session_ref.delete()
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    due_utc = (now + timedelta(seconds=args.lead_seconds)).replace(second=0, microsecond=0)
    payload = _build_recurring_payload(
        label=args.label,
        due_utc=due_utc,
        user_timezone=user_timezone,
        device_id=args.device_id,
    )

    mqtt_queue: Queue = Queue()
    mqtt_client = mqtt.Client(client_id=f"codex-mqtt-{uuid.uuid4().hex[:8]}")

    def on_connect(c, userdata, flags, rc):
        c.subscribe(f"xiaozhi/{args.device_id}/down")

    def on_message(c, userdata, msg):
        mqtt_queue.put(
            {
                "ts": time.time(),
                "topic": msg.topic,
                "payload": msg.payload.decode("utf-8", errors="replace"),
            }
        )

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect(args.broker_host, args.broker_port, 60)
    mqtt_client.loop_start()

    reminder_ref.set(payload)

    result = {
        "uid": args.uid,
        "deviceId": args.device_id,
        "reminderId": reminder_ref.id,
        "dueUTC": _utc_iso(due_utc),
        "label": args.label,
        "mqtt": [],
        "tts": [],
        "llm": [],
        "audio_frames": [],
        "session": None,
        "final": None,
    }

    async def rerun_shared():
        await _collect_session(
            ws_url=args.ws_url,
            device_id=args.device_id,
            mqtt_queue=mqtt_queue,
            result=result,
        )

    try:
        asyncio.run(rerun_shared())
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    for _ in range(30):
        snap = reminder_ref.get()
        data = snap.to_dict() or {}
        if data.get("lastDelivered"):
            result["final"] = data
            break
        time.sleep(2)

    session_snap = session_ref.get()
    if session_snap.exists:
        result["session"] = session_snap.to_dict()

    result["audio_frames_count"] = len(result["audio_frames"])

    output_dir = Path(args.output_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{args.uid.replace('+','plus')}-{timestamp}"

    wav_path = None
    if result["audio_frames"]:
        wav_bytes = opus_datas_to_wav_bytes(result["audio_frames"], sample_rate=16000)
        wav_path = output_dir / f"{stem}.wav"
        _save_wav(wav_path, wav_bytes)
        if args.play_audio:
            _play_audio(wav_path)

    text_path = output_dir / f"{stem}.json"
    serializable = dict(result)
    serializable.pop("audio_frames", None)
    serializable["wavPath"] = str(wav_path) if wav_path else None
    _save_text(text_path, serializable)

    try:
        reminder_ref.delete()
    except Exception:
        pass
    try:
        session_ref.delete()
    except Exception:
        pass

    print(json.dumps(serializable, indent=2, ensure_ascii=False, default=str))
    if wav_path:
        print(f"WAV saved to: {wav_path}")
    print(f"Metadata saved to: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
