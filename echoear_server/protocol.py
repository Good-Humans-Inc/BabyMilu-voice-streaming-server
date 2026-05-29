from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


def dumps(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def new_session_id() -> str:
    return uuid.uuid4().hex


def hello(session_id: str, version: int = 3) -> dict:
    return {
        "type": "hello",
        "transport": "websocket",
        "version": version,
        "session_id": session_id,
        "audio_params": {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        },
    }


def stt(session_id: str, text: str) -> dict:
    return {"type": "stt", "session_id": session_id, "text": text}


def llm(session_id: str, text: str) -> dict:
    return {"type": "llm", "session_id": session_id, "text": text}


def tts(session_id: str, state: str, text: str | None = None) -> dict:
    message = {"type": "tts", "session_id": session_id, "state": state}
    if text is not None:
        message["text"] = text
    return message


def error(session_id: str, stage: str, message: str) -> dict:
    return {"type": "error", "session_id": session_id, "stage": stage, "message": message}


@dataclass
class SessionState:
    session_id: str = field(default_factory=new_session_id)
    audio_format: str = "opus"
    listening: bool = False
    audio_frames: list[bytes] = field(default_factory=list)
    processing: bool = False
    turn_task: Any | None = None

    def begin_listen(self, audio_format: str | None = None) -> None:
        if audio_format:
            self.audio_format = audio_format
        self.audio_frames.clear()
        self.listening = True

    def append_audio(self, data: bytes) -> None:
        if self.listening and data:
            self.audio_frames.append(data)

    def stop_listen(self) -> list[bytes]:
        self.listening = False
        frames = self.audio_frames
        self.audio_frames = []
        return frames
