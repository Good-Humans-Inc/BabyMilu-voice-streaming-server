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
                f"- Main event: {journal.get('mainEvent') or ''}",
                f"- Journal shape: {journal.get('journalShape') or ''}",
                f"- Banned phrases applied: {', '.join(journal.get('bannedPhrasesApplied') or [])}",
                f"- Retry attempted: {journal.get('retryAttempted')}",
                "",
                str(journal.get("text") or "").strip(),
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_conversation_timeline(path: Path, decisions: list[dict[str, Any]]) -> None:
    lines = ["# Conversation Timeline", ""]
    if not decisions:
        lines.append("No sessions were replayed.")
    for index, row in enumerate(decisions, start=1):
        label = "generated journal" if row.get("journalEntryId") else "no journal"
        lines.extend(
            [
                f"## {index}. {row.get('localDate') or 'unknown date'} - {label}",
                "",
                f"- Session ID: `{row.get('sessionId')}`",
                f"- Started at: {row.get('startedAt')}",
                f"- Memory status: {row.get('memoryStatus')}",
                f"- User turns: {row.get('userTurnCount')}",
                f"- Decision: {row.get('decision')}",
                f"- Reason: {row.get('reason')}",
                f"- Turns before/after: {row.get('turnsSinceLastJournalBefore')} -> {row.get('turnsSinceLastJournalAfter')}",
            ]
        )
        if row.get("classificationReason"):
            lines.append(f"- Classifier reason: {row.get('classificationReason')}")
        if row.get("dedupClear") not in {"", None}:
            lines.append(f"- Dedup clear: {row.get('dedupClear')}")
        if row.get("journalEntryId"):
            lines.extend(
                [
                    f"- Journal entry: `{row.get('journalEntryId')}`",
                    f"- Journal type: {row.get('journalType')}",
                    f"- Thread reference: {row.get('threadReference')}",
                ]
            )
        if row.get("topicSummary"):
            lines.append(f"- Topic summary: {row.get('topicSummary')}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
