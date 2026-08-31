from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from ..context import ScenarioContext
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


class WifiProvisioningBm2ContractScenario(BaseScenario):
    name = "contract.wifi_provisioning_bm2"
    description = (
        "Exercise the BM2 app, authenticated backend, and firmware parser "
        "contracts without cloud writes or a new mobile build"
    )

    @staticmethod
    def _required_dir(value: str | None, label: str) -> Path:
        if not value:
            raise RuntimeError(f"{label} is required for this scenario")
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise RuntimeError(f"{label} is not a directory: {path}")
        return path

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        started = utc_now_iso()
        app_repo = self._required_dir(context.args.app_repo, "--app-repo")
        backend_dir = self._required_dir(
            context.args.backend_functions_dir,
            "--backend-functions-dir",
        )
        firmware_repo = self._required_dir(
            context.args.firmware_repo,
            "--firmware-repo",
        )
        node = shutil.which("node")
        if not node:
            raise RuntimeError("node is required for the app contract tests")

        commands = [
            (
                "app",
                app_repo,
                [
                    node,
                    "node_modules/tsx/dist/cli.mjs",
                    "--test",
                    "tests/wifi-provisioning.test.ts",
                    "tests/wifi-reconfiguration.test.ts",
                ],
            ),
            (
                "backend",
                backend_dir,
                [
                    context.args.backend_python,
                    "-m",
                    "pytest",
                    "-q",
                    "test_provisioning_result.py",
                ],
            ),
            (
                "firmware-parser",
                firmware_repo,
                ["./scripts/test_wifi_provisioning_protocol.sh"],
            ),
        ]

        details = {}
        success = True
        for label, cwd, command in commands:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            context.artifact_writer.write_bytes(
                f"{label}.log",
                output,
                context.artifact_dir,
            )
            details[label] = {
                "exitCode": process.returncode,
                "log": str(context.artifact_dir / f"{label}.log"),
            }
            success = success and process.returncode == 0

        firmware_bin = firmware_repo / "build" / "xiaozhi.bin"
        details["firmwareArtifact"] = {
            "path": str(firmware_bin),
            "exists": firmware_bin.is_file(),
            "bytes": firmware_bin.stat().st_size if firmware_bin.is_file() else 0,
        }
        success = success and firmware_bin.is_file() and firmware_bin.stat().st_size > 0

        return ScenarioResult(
            name=self.name,
            success=success,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=(
                "BM2 app/backend/firmware contract gate passed"
                if success
                else "BM2 app/backend/firmware contract gate failed"
            ),
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
