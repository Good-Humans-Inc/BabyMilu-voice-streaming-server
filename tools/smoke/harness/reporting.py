from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()


class ArtifactWriter:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def begin_run(self, scenario_name: str, uid: str) -> Path:
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{stamp}-{_slug(uid)}-{_slug(scenario_name)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, name: str, payload: Any, base_dir: Path | None = None) -> Path:
        target = (base_dir or self.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        return target

    def write_bytes(self, name: str, payload: bytes, base_dir: Path | None = None) -> Path:
        target = (base_dir or self.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target
