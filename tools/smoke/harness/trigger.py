from __future__ import annotations

import json
import subprocess
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


def _access_token() -> str:
    return subprocess.check_output(
        ["gcloud", "auth", "print-access-token"],
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
    completed = subprocess.run(
        ["python3", "-c", code],
        cwd=str(server_root),
        text=True,
        capture_output=True,
        check=True,
    )
    stdout = completed.stdout.strip()
    return json.loads(stdout) if stdout else {"ok": True, "stdout": ""}


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
    return json.loads(stdout) if stdout else {"ok": True, "stdout": ""}
