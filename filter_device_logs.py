#!/usr/bin/env python3
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <device_id> [n_recent_lines]")
        return 1

    device_id = sys.argv[1]
    n_recent_lines = None
    if len(sys.argv) >= 3:
        try:
            n_recent_lines = int(sys.argv[2])
            if n_recent_lines <= 0:
                raise ValueError
        except ValueError:
            print("n_recent_lines must be a positive integer")
            return 1

    pattern = rf"{device_id} \| file path: tmp/asr_[^[:space:]]+\.wav"
    script_dir = Path(__file__).resolve().parent
    # Keep '*' outside shell quoting so glob expansion can match rotated logs.
    log_glob = f"{shlex.quote(str(script_dir / 'container.log'))}*"
    command = (
        "grep -hE "
        f"{shlex.quote(pattern)} "
        f"{log_glob}"
    )
    if n_recent_lines is not None:
        command += f" | tail -n {n_recent_lines}"

    result = subprocess.run(command, shell=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
