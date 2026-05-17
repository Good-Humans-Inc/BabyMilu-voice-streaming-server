from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable


def make_artifact_dir(root: str, alias: str) -> Path:
    safe_alias = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in alias).strip("-")
    path = Path(root) / (safe_alias or "journal_lab_user")
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_generated_journals(path: Path, journals: list[dict[str, Any]]) -> None:
    lines = ["# Generated Journals", ""]
    if not journals:
        lines.append("No journals were generated.")
    for index, journal in enumerate(journals, start=1):
        lines.extend(
            [
                f"## {index}. {journal.get('displayDate')} - {journal.get('journalType')}",
                "",
                f"- Entry ID: `{journal.get('entryId')}`",
                f"- Source sessions: {', '.join(journal.get('sourceSessionIds') or [])}",
                f"- Topic summary: {', '.join(journal.get('topicSummary') or [])}",
                "",
                str(journal.get("text") or "").strip(),
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
