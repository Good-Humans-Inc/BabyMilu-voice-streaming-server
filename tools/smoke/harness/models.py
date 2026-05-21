from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EnvironmentConfig:
    name: str
    environment_type: str
    data_mode: str
    project: str
    scheduler_url: str
    mqtt_host: str
    ws_url: str
    artifact_root: str
    default_timezone: str = "America/Los_Angeles"
    scheduler_trigger: str = "http"
    scheduler_entrypoint: str = ""
    compose_project_dir: str = ""
    compose_file: str = ""
    compose_service: str = "server"
    compose_workdir: str = "/opt/xiaozhi-esp32-server"
    server_log_command_template: str = ""
    notes: str = ""


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    level: str = "error"
    detail: str = ""


@dataclass
class ScenarioResult:
    name: str
    success: bool
    started_at: str
    finished_at: str
    summary: str
    artifact_dir: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "summary": self.summary,
            "artifactDir": self.artifact_dir,
            "details": self.details,
        }


@dataclass
class DeviceCapture:
    device_id: str
    mqtt_events: list[dict[str, Any]] = field(default_factory=list)
    tts_events: list[dict[str, Any]] = field(default_factory=list)
    llm_events: list[dict[str, Any]] = field(default_factory=list)
    audio_frames: list[bytes] = field(default_factory=list)
    goodbye_event: dict[str, Any] | None = None
    wav_path: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_path(value: str | Path) -> Path:
    if isinstance(value, Path):
        return value
    return Path(value)
