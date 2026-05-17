from __future__ import annotations

"""Counterfactual journal replay over a beta user's history.

Run from the server container after configuring lab source env vars on the VM:

    docker compose run --rm --no-deps server python -m services.journals.lab.replay_user \
      --user-id +13854915166 \
      --character-id CH-59cf426a087b4ccc \
      --alias tester_liora \
      --character-name Miffy \
      --timezone America/Los_Angeles \
      --allow-beta-read

This simulates what the journal feature would have done if it had existed from
the user's first BabyMilu session. It reads source sessions/turns, writes local
artifacts under journal_lab_artifacts/, and does not write Supabase, Firestore,
or production data.
"""

import argparse
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from services.journals import generator
from services.journals.lab import artifacts
from services.journals.lab.source_supabase import JournalLabSourceSupabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay journals over beta history without writing data")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--character-id", required=True)
    parser.add_argument("--alias", default="journal_lab_user")
    parser.add_argument("--user-name", default="the user")
    parser.add_argument("--character-name", default="Milu")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--artifact-root", default="journal_lab_artifacts")
    parser.add_argument("--allow-beta-read", action="store_true")
    parser.add_argument("--session-limit", type=int, default=20000)
    parser.add_argument("--turn-threshold", type=int, default=20)
    parser.add_argument("--min-user-turns", type=int, default=3)
    parser.add_argument(
        "--include-pending-memory",
        action="store_true",
        help="Replay pending-memory sessions too. Skipped sessions still remain skipped.",
    )
    parser.add_argument(
        "--max-generated-journals",
        type=int,
        default=0,
        help="Safety cap for generated journals. 0 means no cap.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_beta_read:
        raise SystemExit("Refusing to read beta history without --allow-beta-read")

    source = JournalLabSourceSupabase()
    sessions = source.get_sessions_for_user(args.user_id, limit=args.session_limit)
    turns_by_session = _load_turns_by_session(source, sessions)
    artifact_dir = artifacts.make_artifact_dir(args.artifact_root, args.alias)

    state = ReplayState(
        user_id=args.user_id,
        character_id=args.character_id,
        user_name=args.user_name,
        character_name=args.character_name,
        timezone_name=args.timezone,
        turn_threshold=args.turn_threshold,
        min_user_turns=args.min_user_turns,
        max_generated_journals=args.max_generated_journals,
    )

    decisions: list[dict[str, Any]] = []
    for session in sorted(sessions, key=_session_sort_key):
        decision = _process_session(
            state=state,
            session=session,
            turns=turns_by_session.get(str(session.get("session_id")), []),
            include_pending_memory=args.include_pending_memory,
        )
        decisions.append(decision)

    _flush_all_queues(state)

    summary = {
        "ok": True,
        "mode": "counterfactual_replay_local_artifacts_only",
        "alias": args.alias,
        "sourceSupabaseHost": source.host,
        "request": {
            "userId": args.user_id,
            "characterId": args.character_id,
            "timezone": args.timezone,
            "sessionLimit": args.session_limit,
            "turnThreshold": args.turn_threshold,
            "minUserTurns": args.min_user_turns,
            "includePendingMemory": args.include_pending_memory,
            "maxGeneratedJournals": args.max_generated_journals,
        },
        "counts": {
            "sessionsRead": len(sessions),
            "sessionsWithTurns": sum(1 for turns in turns_by_session.values() if turns),
            "decisions": dict(Counter(row["decision"] for row in decisions)),
            "journalsGenerated": len(state.journals),
            "simulatedMemoryEvents": len(state.memory_events),
        },
        "artifactDir": str(artifact_dir),
    }
    artifacts.write_json(artifact_dir / "replay-summary.json", summary)
    artifacts.write_json(artifact_dir / "replay-timeline.json", decisions)
    artifacts.write_json(artifact_dir / "memory-events.json", state.memory_events)
    artifacts.write_json(artifact_dir / "generated-journals.json", state.journals)
    artifacts.write_csv(
        artifact_dir / "session-decisions.csv",
        decisions,
        [
            "sessionId",
            "localDate",
            "startedAt",
            "memoryStatus",
            "userTurnCount",
            "turnsSinceLastJournalBefore",
            "turnsSinceLastJournalAfter",
            "decision",
            "reason",
            "queueDate",
            "journalEntryId",
        ],
    )
    artifacts.write_generated_journals(artifact_dir / "generated-journals.md", state.journals)

    print(f"Replay complete for {args.alias}")
    print(f"Sessions read: {len(sessions)}")
    print(f"Journals generated: {len(state.journals)}")
    print(f"Artifacts: {artifact_dir}")
    return 0


class ReplayState:
    def __init__(
        self,
        *,
        user_id: str,
        character_id: str,
        user_name: str,
        character_name: str,
        timezone_name: str,
        turn_threshold: int,
        min_user_turns: int,
        max_generated_journals: int,
    ) -> None:
        self.user_id = user_id
        self.character_id = character_id
        self.user_name = user_name
        self.character_name = character_name
        self.timezone_name = timezone_name
        self.turn_threshold = turn_threshold
        self.min_user_turns = min_user_turns
        self.max_generated_journals = max_generated_journals
        self.turns_since_last_journal = 0
        self.current_queue_date: Optional[str] = None
        self.queue: list[dict[str, Any]] = []
        self.journals: list[dict[str, Any]] = []
        self.memory_events: list[dict[str, Any]] = []

    @property
    def generation_capped(self) -> bool:
        return bool(self.max_generated_journals and len(self.journals) >= self.max_generated_journals)


def _process_session(
    *,
    state: ReplayState,
    session: dict[str, Any],
    turns: list[dict[str, Any]],
    include_pending_memory: bool,
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "")
    started_at = _session_time(session)
    local_date = _local_date(started_at, state.timezone_name)
    if state.current_queue_date and local_date != state.current_queue_date:
        _generate_for_current_queue(state)

    before = state.turns_since_last_journal
    memory_status = str(session.get("memory_status") or "missing").lower()
    user_turn_count = _user_turn_count(turns)
    decision = {
        "sessionId": session_id,
        "localDate": local_date,
        "startedAt": started_at.isoformat() if started_at else None,
        "memoryStatus": memory_status,
        "userTurnCount": user_turn_count,
        "turnsSinceLastJournalBefore": before,
        "turnsSinceLastJournalAfter": before,
        "decision": "skipped",
        "reason": "",
        "queueDate": "",
        "journalEntryId": "",
    }

    if memory_status == "skipped":
        decision["reason"] = "memory_skipped"
        return decision
    if memory_status == "pending" and not include_pending_memory:
        decision["reason"] = "memory_pending"
        return decision
    if user_turn_count < state.min_user_turns:
        decision["reason"] = "too_few_turns"
        return decision

    state.turns_since_last_journal += user_turn_count
    decision["turnsSinceLastJournalAfter"] = state.turns_since_last_journal
    if state.turns_since_last_journal < state.turn_threshold:
        decision["reason"] = "below_turn_threshold"
        return decision

    journal_type = "first" if not state.journals and not state.queue else "regular"
    classification: dict[str, Any]
    if journal_type == "first":
        classification = {
            "should_journal": True,
            "dedup_clear": True,
            "topicSummary": ["first journal"],
            "reason": "First journal after threshold.",
        }
    else:
        classification = generator.classify_session(
            turns=turns,
            recent_memory_events=state.memory_events[-5:],
            journal_memory_events=[event for event in reversed(state.memory_events) if event.get("eventType") == "journal_written"][:3],
            trigger_memory_events=[],
            session_start_time=str(session.get("start_time") or session.get("created_at") or ""),
        )
        if not classification.get("should_journal") or classification.get("dedup_clear") is False:
            decision["reason"] = "classification_false"
            decision["decision"] = "skipped"
            return decision

    queued = {
        "sessionId": session_id,
        "classification": classification,
        "sessionStartTime": session.get("start_time") or session.get("created_at"),
        "sessionEndTime": session.get("end_time") or session.get("last_active_at") or session.get("created_at"),
        "turns": turns,
        "sourceMemoryEventIds": [],
    }
    if state.current_queue_date and local_date != state.current_queue_date:
        _generate_for_current_queue(state)
    state.current_queue_date = local_date
    state.queue.append(queued)
    state.turns_since_last_journal = 0
    decision["turnsSinceLastJournalAfter"] = 0
    decision["decision"] = "queued"
    decision["reason"] = "queued_for_generation"
    decision["queueDate"] = local_date
    return decision


def _generate_for_current_queue(state: ReplayState) -> None:
    if not state.current_queue_date or not state.queue or state.generation_capped:
        state.queue = []
        state.current_queue_date = None
        return

    journal_type = "first" if not state.journals else "regular"
    generated = generator.generate_journal_text(
        journal_type=journal_type,
        character_data={"id": state.character_id, "name": state.character_name, "profile": {"name": state.character_name}},
        user_data={"id": state.user_id, "name": state.user_name},
        system_memory_block="",
        sessions=state.queue,
        prior_journal_entries=state.journals[-10:],
        thread_reference=_thread_reference_needed(state.memory_events),
    )
    entry_id = f"lab-{uuid.uuid4()}"
    source_session_ids = [str(item.get("sessionId")) for item in state.queue]
    topic_summary = generated.get("topicSummary") or _topic_summary_from_queue(state.queue)
    journal = {
        "entryId": entry_id,
        "displayDate": state.current_queue_date,
        "journalType": journal_type,
        "text": generated["text"],
        "threadReference": bool(generated["thread_reference"]),
        "topicSummary": topic_summary,
        "sourceSessionIds": source_session_ids,
    }
    state.journals.append(journal)
    state.memory_events.append(
        {
            "eventType": "journal_written",
            "modality": "plushie_conversation",
            "content": {
                "text": generated["text"],
                "journalEntryId": entry_id,
                "journalType": journal_type,
                "topicSummary": topic_summary,
                "thread_reference": bool(generated["thread_reference"]),
            },
            "time": {
                "occurredAt": str(state.queue[-1].get("sessionEndTime") or ""),
                "ingestedAt": datetime.now(timezone.utc).isoformat(),
            },
            "source": {
                "sessionId": source_session_ids[-1] if source_session_ids else None,
                "characterId": state.character_id,
            },
        }
    )
    state.queue = []
    state.current_queue_date = None


def _flush_all_queues(state: ReplayState) -> None:
    if state.queue:
        _generate_for_current_queue(state)


def _load_turns_by_session(
    source: JournalLabSourceSupabase,
    sessions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if session_id:
            result[session_id] = source.get_turns_for_session(session_id)
    return result


def _user_turn_count(turns: list[dict[str, Any]]) -> int:
    return sum(1 for turn in turns if str(turn.get("speaker") or "").lower() == "user")


def _session_sort_key(session: dict[str, Any]) -> str:
    return str(session.get("start_time") or session.get("created_at") or session.get("last_active_at") or "")


def _session_time(session: dict[str, Any]) -> Optional[datetime]:
    for key in ("start_time", "created_at", "last_active_at", "end_time"):
        parsed = _parse_dt(session.get(key))
        if parsed:
            return parsed
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _local_date(value: Optional[datetime], timezone_name: str) -> str:
    value = value or datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return value.astimezone(tz).date().isoformat()


def _topic_summary_from_queue(queue: list[dict[str, Any]]) -> list[str]:
    topics: list[str] = []
    for session in queue:
        classification = session.get("classification") if isinstance(session.get("classification"), dict) else {}
        for item in classification.get("topicSummary") or []:
            text = str(item).strip()
            if text and text not in topics:
                topics.append(text)
    return topics


def _thread_reference_needed(memory_events: list[dict[str, Any]]) -> bool:
    journal_events = [event for event in reversed(memory_events) if event.get("eventType") == "journal_written"][:3]
    if len(journal_events) < 3:
        return False
    return all((event.get("content") or {}).get("thread_reference") is False for event in journal_events)


if __name__ == "__main__":
    raise SystemExit(main())
