from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

from .models import EnvironmentConfig, PreflightCheck


def _check_command(name: str) -> PreflightCheck:
    path = shutil.which(name)
    return PreflightCheck(
        name=f"command:{name}",
        ok=bool(path),
        detail=path or "not found on PATH",
    )


def _check_optional_command(name: str) -> PreflightCheck:
    path = shutil.which(name)
    return PreflightCheck(
        name=f"command:{name}",
        ok=bool(path),
        level="warn",
        detail=path or "not found on PATH",
    )


def _check_import(name: str) -> PreflightCheck:
    try:
        importlib.import_module(name)
        return PreflightCheck(name=f"python:{name}", ok=True, detail="import ok")
    except Exception as exc:
        return PreflightCheck(name=f"python:{name}", ok=False, detail=str(exc))


def _check_gcloud_access_token() -> PreflightCheck:
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        return PreflightCheck(
            name="gcloud-auth",
            ok=bool(token),
            detail="access token available" if token else "empty token",
        )
    except Exception as exc:
        detail = getattr(exc, "output", "") or str(exc)
        if (
            "PermissionError" in detail
            or "Operation not permitted" in detail
            or "SandboxDenied" in detail
        ):
            return PreflightCheck(
                name="gcloud-auth",
                ok=False,
                level="warn",
                detail="Codex sandbox blocked gcloud credential file access; verify `gcloud auth print-access-token` in a normal terminal",
            )
        return PreflightCheck(name="gcloud-auth", ok=False, detail=detail)


def _check_firestore(project: str) -> PreflightCheck:
    try:
        firestore = importlib.import_module("google.cloud.firestore")
        client = firestore.Client(project=project)
        _ = client.project
        return PreflightCheck(
            name="firestore-client",
            ok=True,
            detail=f"client ready for project {project}",
        )
    except Exception as exc:
        return PreflightCheck(name="firestore-client", ok=False, detail=str(exc))


def _check_adc_token_detail() -> tuple[bool, str, str]:
    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "application-default", "print-access-token"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        return bool(token), "error", "application-default token available" if token else "empty ADC token"
    except Exception as exc:
        detail = getattr(exc, "output", "") or str(exc)
        if (
            "PermissionError" in detail
            or "Operation not permitted" in detail
            or "SandboxDenied" in detail
        ):
            return False, "warn", "Codex sandbox blocked gcloud ADC file access; verify `gcloud auth application-default print-access-token` in a normal terminal"
        if "NameResolutionError" in detail or "Failed to resolve" in detail:
            return False, "warn", "Codex sandbox could not refresh ADC over the network; verify in a normal terminal"
        return False, "error", "run gcloud auth application-default login"


def _check_env_field(name: str, value: str) -> PreflightCheck:
    return PreflightCheck(
        name=f"config:{name}",
        ok=bool(value),
        detail=value or "missing",
    )


def _check_choice(name: str, value: str, allowed: set[str]) -> PreflightCheck:
    ok = value in allowed
    return PreflightCheck(
        name=f"config:{name}",
        ok=ok,
        detail=value if ok else f"{value or 'missing'} not in {sorted(allowed)}",
    )


def _check_path_exists(name: str, value: str, *, optional: bool = False) -> PreflightCheck:
    if optional and not value:
        return PreflightCheck(name=f"config:{name}", ok=True, level="warn", detail="not set")
    ok = bool(value) and Path(value).exists()
    return PreflightCheck(
        name=f"config:{name}",
        ok=ok,
        detail=value if ok else f"{value or 'missing'} does not exist",
        level="warn" if optional else "error",
    )


def _check_callable_ref(name: str, value: str) -> PreflightCheck:
    ok = bool(value) and ":" in value and all(part.strip() for part in value.split(":", 1))
    return PreflightCheck(
        name=f"config:{name}",
        ok=ok,
        detail=value if ok else f"{value or 'missing'} is not in module:function form",
    )


def run_preflight(environment: EnvironmentConfig, strict: bool = False) -> list[PreflightCheck]:
    checks = [
        _check_import("requests"),
        _check_import("paho.mqtt.client"),
        _check_import("websockets"),
        _check_choice("environment_type", environment.environment_type, {"cloud", "local-compose", "external-dev"}),
        _check_choice("data_mode", environment.data_mode, {"live-shape", "isolated"}),
        _check_env_field("mqtt_host", environment.mqtt_host),
        _check_env_field("ws_url", environment.ws_url),
    ]

    if environment.environment_type == "cloud":
        checks.extend(
            [
                _check_command("gcloud"),
                _check_env_field("project", environment.project),
                _check_env_field("scheduler_url", environment.scheduler_url),
            ]
        )
    elif environment.environment_type == "local-compose":
        checks.extend(
            [
                _check_optional_command("docker"),
                _check_optional_command("docker-compose"),
                _check_path_exists("compose_project_dir", environment.compose_project_dir),
                _check_path_exists("compose_file", environment.compose_file, optional=True),
                _check_choice("scheduler_trigger", environment.scheduler_trigger, {"http", "entrypoint", "docker-exec"}),
            ]
        )
        if environment.scheduler_trigger == "entrypoint":
            checks.append(_check_callable_ref("scheduler_entrypoint", environment.scheduler_entrypoint))
        if environment.scheduler_trigger == "docker-exec":
            checks.append(_check_env_field("compose_service", environment.compose_service))
            checks.append(_check_callable_ref("scheduler_entrypoint", environment.scheduler_entrypoint))
    elif environment.environment_type == "external-dev":
        checks.extend(
            [
                _check_choice("scheduler_trigger", environment.scheduler_trigger, {"http", "manual"}),
                _check_env_field("scheduler_url", environment.scheduler_url) if environment.scheduler_trigger == "http" else PreflightCheck(name="config:scheduler_url", ok=True, level="warn", detail="manual trigger"),
            ]
        )

    if environment.data_mode == "live-shape":
        checks.extend(
            [
                _check_import("google.cloud.firestore"),
                _check_env_field("project", environment.project),
            ]
        )

    if all(check.ok or check.level == "warn" for check in checks):
        if environment.environment_type == "cloud" or environment.data_mode == "live-shape":
            checks.append(_check_gcloud_access_token())
        if environment.data_mode == "live-shape":
            adc_ok, adc_level, adc_detail = _check_adc_token_detail()
            checks.append(
                PreflightCheck(
                    name="gcloud-adc",
                    ok=adc_ok,
                    level=adc_level,
                    detail=adc_detail,
                )
            )
            checks.append(_check_firestore(environment.project))
    return checks


def render_preflight(checks: list[PreflightCheck]) -> str:
    lines = ["Smoke preflight:"]
    for check in checks:
        marker = "OK" if check.ok else ("WARN" if check.level == "warn" else "FAIL")
        lines.append(f"- [{marker}] {check.name}: {check.detail}")
    return "\n".join(lines)


def has_failures(checks: list[PreflightCheck]) -> bool:
    return any(not check.ok and check.level != "warn" for check in checks)
