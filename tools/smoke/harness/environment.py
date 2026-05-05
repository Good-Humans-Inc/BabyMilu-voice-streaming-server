from __future__ import annotations

import json
import os
from pathlib import Path

from .models import EnvironmentConfig


ROOT = Path(__file__).resolve().parents[1]
ENV_DIR = ROOT / "environments"


def _candidate_paths(name: str, explicit: str | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(ENV_DIR / f"{name}.local.json")
    paths.append(ENV_DIR / f"{name}.json")
    return paths


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_environment(name: str, explicit: str | None = None) -> EnvironmentConfig:
    payload: dict | None = None
    source_path: Path | None = None
    for path in _candidate_paths(name, explicit):
        if path.exists():
            payload = _read_json(path)
            source_path = path
            break

    if payload is None:
        payload = {
            "environment_type": os.environ.get("BABYMILU_SMOKE_ENVIRONMENT_TYPE", "cloud"),
            "data_mode": os.environ.get("BABYMILU_SMOKE_DATA_MODE", "live-shape"),
            "project": os.environ.get("BABYMILU_SMOKE_PROJECT", ""),
            "scheduler_url": os.environ.get("BABYMILU_SMOKE_SCHEDULER_URL", ""),
            "mqtt_host": os.environ.get("BABYMILU_SMOKE_MQTT_HOST", ""),
            "ws_url": os.environ.get("BABYMILU_SMOKE_WS_URL", ""),
            "artifact_root": os.environ.get(
                "BABYMILU_SMOKE_ARTIFACT_ROOT",
                str(ROOT / "artifacts"),
            ),
            "default_timezone": os.environ.get(
                "BABYMILU_SMOKE_DEFAULT_TIMEZONE",
                "America/Los_Angeles",
            ),
            "scheduler_trigger": os.environ.get("BABYMILU_SMOKE_SCHEDULER_TRIGGER", "http"),
            "scheduler_entrypoint": os.environ.get("BABYMILU_SMOKE_SCHEDULER_ENTRYPOINT", ""),
            "compose_project_dir": os.environ.get("BABYMILU_SMOKE_COMPOSE_PROJECT_DIR", ""),
            "compose_file": os.environ.get("BABYMILU_SMOKE_COMPOSE_FILE", ""),
            "compose_service": os.environ.get("BABYMILU_SMOKE_COMPOSE_SERVICE", "server"),
            "compose_workdir": os.environ.get("BABYMILU_SMOKE_COMPOSE_WORKDIR", "/opt/xiaozhi-esp32-server"),
            "notes": os.environ.get("BABYMILU_SMOKE_NOTES", ""),
        }
    else:
        payload.setdefault("environment_type", "cloud")
        payload.setdefault("data_mode", "live-shape")
        payload.setdefault("artifact_root", str(ROOT / "artifacts"))
        payload.setdefault("default_timezone", "America/Los_Angeles")
        payload.setdefault("scheduler_trigger", "http")
        payload.setdefault("scheduler_entrypoint", "")
        payload.setdefault("compose_project_dir", "")
        payload.setdefault("compose_file", "")
        payload.setdefault("compose_service", "server")
        payload.setdefault("compose_workdir", "/opt/xiaozhi-esp32-server")
        payload.setdefault("notes", "")

    artifact_root = payload["artifact_root"]
    if not Path(artifact_root).is_absolute():
        artifact_root = str((ROOT.parent.parent / artifact_root).resolve())

    env = EnvironmentConfig(
        name=name,
        environment_type=payload["environment_type"],
        data_mode=payload["data_mode"],
        project=payload["project"],
        scheduler_url=payload["scheduler_url"],
        mqtt_host=payload["mqtt_host"],
        ws_url=payload["ws_url"],
        artifact_root=artifact_root,
        default_timezone=payload["default_timezone"],
        scheduler_trigger=payload["scheduler_trigger"],
        scheduler_entrypoint=payload["scheduler_entrypoint"],
        compose_project_dir=payload["compose_project_dir"],
        compose_file=payload["compose_file"],
        compose_service=payload["compose_service"],
        compose_workdir=payload["compose_workdir"],
        notes=payload["notes"],
    )
    setattr(env, "source_path", str(source_path) if source_path else "<env-vars>")
    return env
