#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
MEMORY_WORKER_ROOT = WORKSPACE_ROOT / "memory-worker"
PYTHON = MEMORY_WORKER_ROOT / ".venv" / "bin" / "python"
SCRIPT = MEMORY_WORKER_ROOT / "scripts" / "run_next_starter_e2e.py"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the next_starter latency smoke through the existing memory-worker e2e harness."
    )
    parser.add_argument("--ws-url", default=os.environ.get("BABYMILU_LATENCY_WS_URL", ""))
    parser.add_argument("--device-id", default=os.environ.get("BABYMILU_LATENCY_DEVICE_ID", ""))
    parser.add_argument("--ssh-host", default=os.environ.get("BABYMILU_LATENCY_SSH_HOST", ""))
    args = parser.parse_args()

    if not PYTHON.exists():
        raise SystemExit(f"memory-worker python not found: {PYTHON}")
    if not SCRIPT.exists():
        raise SystemExit(f"latency harness script not found: {SCRIPT}")
    if not args.ws_url:
        raise SystemExit("missing --ws-url or BABYMILU_LATENCY_WS_URL")
    if not args.device_id:
        raise SystemExit("missing --device-id or BABYMILU_LATENCY_DEVICE_ID")
    if not args.ssh_host:
        raise SystemExit("missing --ssh-host or BABYMILU_LATENCY_SSH_HOST")

    cmd = [
        "/usr/bin/arch",
        "-arm64",
        str(PYTHON),
        str(SCRIPT),
        "--ws-url",
        args.ws_url,
        "--device-id",
        args.device_id,
        "--ssh-host",
        args.ssh_host,
    ]
    completed = subprocess.run(cmd)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
