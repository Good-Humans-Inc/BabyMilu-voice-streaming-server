from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import requests

from .models import EnvironmentConfig


def trigger_scheduler(environment: EnvironmentConfig) -> dict:
    if environment.scheduler_trigger == "http":
        return _invoke_http(environment.scheduler_url)
    if environment.scheduler_trigger == "entrypoint":
        return _invoke_entrypoint(environment)
    if environment.scheduler_trigger == "docker-exec":
        return _invoke_docker_exec(environment)
    if environment.scheduler_trigger == "manual":
        return {"ok": True, "manual": True, "message": "Manual trigger selected; no scheduler invocation executed."}
    raise RuntimeError(f"Unsupported scheduler_trigger: {environment.scheduler_trigger}")


def _command_candidates(name: str) -> list[str]:
    candidates = []
    found = shutil.which(name)
    if found:
        candidates.append(found)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        cloud_sdk_bin = Path(local_app_data) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin"
        candidates.extend(
            [
                str(cloud_sdk_bin / f"{name}.cmd"),
                str(cloud_sdk_bin / f"{name}.CMD"),
                str(cloud_sdk_bin / name),
            ]
        )
    return list(dict.fromkeys(candidates))


def _is_usable_command(path: str) -> bool:
    try:
        return (
            subprocess.run(
                [path, "--version"],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            == 0
        )
    except Exception:
        return False


def _command(name: str) -> str:
    candidates = _command_candidates(name)
    for path in candidates:
        if Path(path).exists() and _is_usable_command(path):
            return path
    return candidates[0] if candidates else name


def _access_token() -> str:
    return subprocess.check_output(
        [_command("gcloud"), "auth", "print-access-token"],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _invoke_http(url: str) -> dict:
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json={},
        timeout=60,
    )
    response.raise_for_status()
    try:
        return response.json()
    except Exception:
        return {"text": response.text}


def _invoke_entrypoint(environment: EnvironmentConfig) -> dict:
    callable_ref = environment.scheduler_entrypoint
    if ":" not in callable_ref:
        raise RuntimeError(
            "scheduler_entrypoint must use module:function form, for example "
            "`services.alarms.cloud.functions:scan_due_scheduled_items`"
        )
    module_name, function_name = callable_ref.split(":", 1)
    compose_dir = Path(environment.compose_project_dir).resolve()
    server_root = compose_dir / "main" / "xiaozhi-server"
    code = textwrap.dedent(
        f"""
        import json
        import sys
        sys.path.insert(0, {str(server_root)!r})
        from {module_name} import {function_name}
        result = {function_name}(None)
        print(json.dumps(result, ensure_ascii=False, default=str))
        """
    ).strip()
    run_env = os.environ.copy()
    run_env.setdefault("GOOGLE_CLOUD_PROJECT", environment.project)
    run_env.setdefault("ALARM_WS_URL", environment.ws_url)
    run_env.setdefault("ALARM_MQTT_URL", f"mqtt://{environment.mqtt_host}:1883")
    run_env.setdefault("INCLUDE_REMINDERS_IN_UNIFIED_SCAN", "true")
    run_env.setdefault("REMINDER_EXECUTE", "true")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(server_root),
        env=run_env,
        text=True,
        capture_output=True,
        check=True,
    )
    stdout = completed.stdout.strip()
    return _json_from_stdout(stdout)


def _invoke_docker_exec(environment: EnvironmentConfig) -> dict:
    compose_dir = Path(environment.compose_project_dir).resolve()
    compose_cmd = ["docker", "compose"]
    if environment.compose_file:
        compose_cmd.extend(["-f", environment.compose_file])
    compose_cmd.extend(
        [
            "exec",
            "-T",
            environment.compose_service,
            "python",
            "-c",
            (
                "import json; "
                f"from {environment.scheduler_entrypoint.split(':', 1)[0]} import {environment.scheduler_entrypoint.split(':', 1)[1]}; "
                f"print(json.dumps({environment.scheduler_entrypoint.split(':', 1)[1]}(None), ensure_ascii=False, default=str))"
            ),
        ]
    )
    completed = subprocess.run(
        compose_cmd,
        cwd=str(compose_dir),
        text=True,
        capture_output=True,
        check=True,
    )
    stdout = completed.stdout.strip()
    return _json_from_stdout(stdout)


def _json_from_stdout(stdout: str) -> dict:
    if not stdout:
        return {"ok": True, "stdout": ""}
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"ok": False, "stdout": stdout}
