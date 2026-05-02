from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ..context import ScenarioContext
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


class NextStarterScenario(BaseScenario):
    name = "interaction.next_starter"
    description = "Run the validated next_starter latency-hack end-to-end flow through the shared smoke entrypoint."

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        args = context.args

        if not args.device_id:
            raise RuntimeError("--device-id is required for interaction.next_starter")
        if not args.ssh_host:
            raise RuntimeError("--ssh-host is required for interaction.next_starter")

        repo_root = Path(__file__).resolve().parents[4]
        wrapper = repo_root / "tools" / "smoke" / "run_next_starter_latency.py"
        if not wrapper.exists():
            raise RuntimeError(f"Latency smoke wrapper missing: {wrapper}")

        cmd = [
            "python3",
            str(wrapper),
            "--ws-url",
            context.environment.ws_url,
            "--device-id",
            args.device_id,
            "--ssh-host",
            args.ssh_host,
        ]

        completed = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(repo_root),
            text=True,
            capture_output=True,
        )

        details = {
            "command": cmd,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ws_url": context.environment.ws_url,
            "device_id": args.device_id,
            "ssh_host": args.ssh_host,
        }
        context.artifact_writer.write_json("scenario-details.json", details, context.artifact_dir)
        context.artifact_writer.write_json(
            "scenario-stdout.json",
            {"stdout": completed.stdout, "stderr": completed.stderr},
            context.artifact_dir,
        )

        if completed.returncode != 0:
            summary = "next_starter latency smoke failed"
            return ScenarioResult(
                name=self.name,
                success=False,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=summary,
                artifact_dir=str(context.artifact_dir),
                details=details,
            )

        summary = "next_starter latency smoke passed"
        return ScenarioResult(
            name=self.name,
            success=True,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=summary,
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
