#!/usr/bin/env python3
import shlex
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <device_id>")
        return 1

    device_id = sys.argv[1]
    pattern = rf"{device_id} \| file path: tmp/asr_[^[:space:]]+\.wav"
    command = f"docker logs current-server-1 2>&1 | grep -E {shlex.quote(pattern)}"

    result = subprocess.run(command, shell=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
