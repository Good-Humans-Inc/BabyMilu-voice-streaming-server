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
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from services.journals import generator
from services.journals.lab import artifacts
from services.journals.lab.profile_firestore import load_lab_profile
from services.journals.lab.source_supabase import JournalLabSourceSupabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay journals over beta history without writing data")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--character-id", required=True)
    parser.add_argument("--alias", default="journal_lab_user")
    parser.add_argument("--user-name", default="the user")
    parser.add_argument("--character-name", default="Milu")
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument(
        "--profile-firestore-database",
        default="(default)",
        help="Firestore database for read-only profile lookup. Empty/(default) reads default Firestore.",
    )
    parser.add_argument("--artifact-root", default="journal_lab_artifacts")
    parser.add_argument("--allow-beta-read", action="store_true")
    parser.add_argument("--session-limit", type=int, default=20000)
    parser.add_argument("--turn-threshold", type=int, default=20)
    parser.add_argument("--min-user-turns", type=int, default=3)
    parser.add_argument("--context-max-days", type=int, default=7)
    parser.add_argument("--context-max-sessions", type=int, default=20)
    parser.add_argument("--context-max-user-turns", type=int, default=80)
    parser.add_argument("--context-max-total-turns", type=int, default=160)
    parser.add_argument("--context-max-chars", type=int, default=20000)
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
    parser.add_argument(
        "--disable-simulated-memory-events",
        action="store_true",
        help="Do not pass simulated journal_written events into later classification prompts.",
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
    profile = load_lab_profile(
        user_id=args.user_id,
        character_id=args.character_id,
        database_id=args.profile_firestore_database,
    )
    user_data = profile["userData"] if isinstance(profile.get("userData"), dict) else {}
    character_data = profile["characterData"] if isinstance(profile.get("characterData"), dict) else {}
    profile_name = _profile_name(character_data)
    user_name = _first_string(user_data, ("name", "displayName", "firstName")) or args.user_name
    character_name = profile_name or args.character_name
    timezone_name = _first_string(user_data, ("timezone", "timeZone", "timezoneId", "userTimezone")) or args.timezone

    state = ReplayState(
        user_id=args.user_id,
        character_id=args.character_id,
        user_data={**user_data, "name": user_name},
        character_data={
            **character_data,
            "id": args.character_id,
            "name": character_name,
            "profile": {**(character_data.get("profile") if isinstance(character_data.get("profile"), dict) else {}), "name": character_name},
        },
        timezone_name=timezone_name,
        turn_threshold=args.turn_threshold,
        min_user_turns=args.min_user_turns,
        context_max_days=args.context_max_days,
        context_max_sessions=args.context_max_sessions,
        context_max_user_turns=args.context_max_user_turns,
        context_max_total_turns=args.context_max_total_turns,
        context_max_chars=args.context_max_chars,
        max_generated_journals=args.max_generated_journals,
        simulated_memory_events_enabled=not args.disable_simulated_memory_events,
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
            "resolvedTimezone": timezone_name,
            "sessionLimit": args.session_limit,
            "turnThreshold": args.turn_threshold,
            "minUserTurns": args.min_user_turns,
            "contextMaxDays": args.context_max_days,
            "contextMaxSessions": args.context_max_sessions,
            "contextMaxUserTurns": args.context_max_user_turns,
            "contextMaxTotalTurns": args.context_max_total_turns,
            "contextMaxChars": args.context_max_chars,
            "includePendingMemory": args.include_pending_memory,
            "maxGeneratedJournals": args.max_generated_journals,
            "simulatedMemoryEventsEnabled": not args.disable_simulated_memory_events,
        },
        "profile": {
            "firestoreDatabase": profile["database"],
            "userExists": profile["userExists"],
            "characterExists": profile["characterExists"],
            "resolvedUserName": user_name,
            "resolvedCharacterName": character_name,
        },
        "counts": {
            "sessionsRead": len(sessions),
            "sessionsWithTurns": sum(1 for turns in turns_by_session.values() if turns),
            "decisions": dict(Counter(row["decision"] for row in decisions)),
            "reasons": dict(Counter(row["reason"] for row in decisions)),
            "journalsGenerated": len(state.journals),
            "simulatedMemoryEvents": len(state.memory_events),
            "threadReferences": sum(1 for journal in state.journals if journal.get("threadReference")),
            "dedupBlockedSessions": sum(1 for row in decisions if row.get("reason") == "dedup_blocked_by_prior_journal"),
        },
        "artifactDir": str(artifact_dir),
    }
    artifacts.write_json(artifact_dir / "replay-summary.json", summary)
    artifacts.write_json(artifact_dir / "replay-timeline.json", decisions)
    artifacts.write_json(artifact_dir / "memory-events.json", state.memory_events)
    artifacts.write_json(artifact_dir / "generated-journals.json", state.journals)
    artifacts.write_json(artifact_dir / "journal-briefs.json", artifacts.journal_briefs(state.journals))
    artifacts.write_json(artifact_dir / "writer-payloads.json", artifacts.writer_payloads(state.journals))
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
            "classificationShouldJournal",
            "journalValueType",
            "journalReasonType",
            "dedupClear",
            "classificationReason",
            "topicSummary",
            "queueDate",
            "journalEntryId",
            "journalType",
            "threadReference",
        ],
    )
    artifacts.write_generated_journals(artifact_dir / "generated-journals.md", state.journals)
    artifacts.write_journal_briefs(artifact_dir / "journal-briefs.md", state.journals)
    artifacts.write_writer_payloads(artifact_dir / "writer-payloads.md", state.journals)
    artifacts.write_conversation_timeline(artifact_dir / "conversation-timeline.md", decisions)

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
        user_data: dict[str, Any],
        character_data: dict[str, Any],
        timezone_name: str,
        turn_threshold: int,
        min_user_turns: int,
        context_max_days: int,
        context_max_sessions: int,
        context_max_user_turns: int,
        context_max_total_turns: int,
        context_max_chars: int,
        max_generated_journals: int,
        simulated_memory_events_enabled: bool,
    ) -> None:
        self.user_id = user_id
        self.character_id = character_id
        self.user_data = user_data
        self.character_data = character_data
        self.timezone_name = timezone_name
        self.turn_threshold = turn_threshold
        self.min_user_turns = min_user_turns
        self.context_max_days = context_max_days
        self.context_max_sessions = context_max_sessions
        self.context_max_user_turns = context_max_user_turns
        self.context_max_total_turns = context_max_total_turns
        self.context_max_chars = context_max_chars
        self.max_generated_journals = max_generated_journals
        self.simulated_memory_events_enabled = simulated_memory_events_enabled
        self.turns_since_last_journal = 0
        self.current_queue_date: Optional[str] = None
        self.queue: list[dict[str, Any]] = []
        self.journals: list[dict[str, Any]] = []
        self.memory_events: list[dict[str, Any]] = []
        self.eligible_context_sessions: list[dict[str, Any]] = []

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
        "classificationShouldJournal": "",
        "journalValueType": "",
        "journalReasonType": "",
        "dedupClear": "",
        "classificationReason": "",
        "topicSummary": "",
        "queueDate": "",
        "journalEntryId": "",
        "journalType": "",
        "threadReference": "",
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

    state.eligible_context_sessions.append(
        {
            "sessionId": session_id,
            "sessionStartTime": session.get("start_time") or session.get("created_at"),
            "sessionEndTime": session.get("end_time") or session.get("last_active_at") or session.get("created_at"),
            "memoryStatus": memory_status,
            "isTriggerSession": False,
            "userTurnCount": user_turn_count,
            "turns": turns,
        }
    )

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
            "journal_value_type": "strong",
            "journal_reason_type": "milestone",
            "topicSummary": ["first journal"],
            "reason": "First journal after threshold.",
        }
        _copy_classification_fields(decision, classification)
    else:
        journal_events = [event for event in reversed(state.memory_events) if event.get("eventType") == "journal_written"][:3]
        if not state.simulated_memory_events_enabled:
            journal_events = []
        classification = generator.classify_session(
            turns=turns,
            recent_memory_events=state.memory_events[-5:] if state.simulated_memory_events_enabled else [],
            journal_memory_events=journal_events,
            trigger_memory_events=[],
            session_start_time=str(session.get("start_time") or session.get("created_at") or ""),
        )
        _copy_classification_fields(decision, classification)
        if classification.get("dedup_clear") is False:
            decision["reason"] = "dedup_blocked_by_prior_journal"
            decision["decision"] = "skipped"
            return decision
        if not _classification_passes(classification):
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
        "_decision": decision,
    }
    if state.current_queue_date and local_date != state.current_queue_date:
        _generate_for_current_queue(state)
    state.current_queue_date = local_date
    state.queue.append(queued)
    for context_session in state.eligible_context_sessions:
        if context_session.get("sessionId") == session_id:
            context_session["isTriggerSession"] = True
    state.turns_since_last_journal = 0
    decision["turnsSinceLastJournalAfter"] = 0
    decision["decision"] = "queued"
    decision["reason"] = "queued_for_generation"
    decision["queueDate"] = local_date
    decision["journalType"] = journal_type
    return decision


def _generate_for_current_queue(state: ReplayState) -> None:
    if not state.current_queue_date or not state.queue or state.generation_capped:
        state.queue = []
        state.current_queue_date = None
        return

    journal_type = "first" if not state.journals else "regular"
    context = _generation_context_for_queue(state)
    prior_entries = _prior_entries_from_memory_events(state.memory_events) if state.simulated_memory_events_enabled else []
    try:
        generated = generator.generate_journal_text(
            journal_type=journal_type,
            character_data=state.character_data,
            user_data=state.user_data,
            system_memory_block="",
            sessions=context["sessions"],
            prior_journal_entries=prior_entries,
            thread_reference=_thread_reference_needed(state.memory_events),
            coverage_window=context["coverageWindow"],
            avoid_repeating=_avoid_repeating_from_journals(prior_entries),
            allow_time_specific_opening=context["singleSameDayMoment"],
        )
    except Exception as exc:
        for queued_session in state.queue:
            decision = queued_session.get("_decision") if isinstance(queued_session, dict) else None
            if isinstance(decision, dict):
                decision["decision"] = "generation_error"
                decision["reason"] = "generation_error"
                decision["classificationReason"] = str(exc)
        state.queue = []
        state.current_queue_date = None
        return
    entry_id = f"lab-{uuid.uuid4()}"
    source_session_ids = [str(item.get("sessionId")) for item in context["sessions"]]
    topic_summary = generated.get("topicSummary") or _topic_summary_from_queue(state.queue)
    journal = {
        "entryId": entry_id,
        "displayDate": state.current_queue_date,
        "journalType": journal_type,
        "text": generated["text"],
        "threadReference": bool(generated["thread_reference"]),
        "topicSummary": topic_summary,
        "coverageSummary": generated.get("coverageSummary") or [],
        "concreteAnchors": generated.get("concreteAnchors") or [],
        "emotionalThemes": generated.get("emotionalThemes") or [],
        "avoidRepeating": generated.get("avoidRepeating") or [],
        "coverageWindow": context["coverageWindow"],
        "journalShape": generated.get("journal_shape") or "",
        "mainEvent": generated.get("main_event") or "",
        "userExperience": generated.get("userExperience") or "",
        "characterObservation": generated.get("characterObservation") or "",
        "characterInnerResponse": generated.get("characterInnerResponse") or "",
        "userOwnedDetails": generated.get("userOwnedDetails") or [],
        "characterOwnedDetails": generated.get("characterOwnedDetails") or [],
        "forbiddenPovClaims": generated.get("forbiddenPovClaims") or [],
        "bannedPhrasesApplied": generated.get("bannedPhrasesApplied") or [],
        "journalBrief": generated.get("journalBrief") or {},
        "writerPrompt": generated.get("writerPrompt") or {},
        "repetitionProfile": generated.get("repetitionProfile") or {},
        "qualityCheck": generated.get("qualityCheck") or {},
        "retryAttempted": bool(generated.get("retryAttempted")),
        "threadReferenceReason": generated.get("thread_reference_reason") or "",
        "threadReferenceTargets": generated.get("thread_reference_targets") or [],
        "sourceSessionIds": source_session_ids,
    }
    state.journals.append(journal)
    for queued_session in state.queue:
        if isinstance(queued_session, dict):
            queued_session["journalEntryId"] = entry_id
            decision = queued_session.get("_decision")
            if isinstance(decision, dict):
                decision["journalEntryId"] = entry_id
                decision["journalType"] = journal_type
                decision["threadReference"] = bool(generated["thread_reference"])
                decision["topicSummary"] = "; ".join(topic_summary)
    ingested_at = datetime.now(timezone.utc).isoformat()
    state.memory_events.append(
        {
            "eventType": "journal_written",
            "modality": "plushie_conversation",
            "content": {
                "text": generated["text"],
                "journalEntryId": entry_id,
                "journalType": journal_type,
                "topicSummary": topic_summary,
                "coverageSummary": generated.get("coverageSummary") or [],
                "concreteAnchors": generated.get("concreteAnchors") or [],
                "emotionalThemes": generated.get("emotionalThemes") or [],
                "avoidRepeating": generated.get("avoidRepeating") or [],
                "journal_shape": generated.get("journal_shape") or "",
                "main_event": generated.get("main_event") or "",
                "userExperience": generated.get("userExperience") or "",
                "characterObservation": generated.get("characterObservation") or "",
                "characterInnerResponse": generated.get("characterInnerResponse") or "",
                "userOwnedDetails": generated.get("userOwnedDetails") or [],
                "characterOwnedDetails": generated.get("characterOwnedDetails") or [],
                "forbiddenPovClaims": generated.get("forbiddenPovClaims") or [],
                "bannedPhrasesApplied": generated.get("bannedPhrasesApplied") or [],
                "thread_reference": bool(generated["thread_reference"]),
                "thread_reference_reason": generated.get("thread_reference_reason") or "",
                "thread_reference_targets": generated.get("thread_reference_targets") or [],
            },
            "time": {
                "occurredAt": str(state.queue[-1].get("sessionEndTime") or ""),
                "ingestedAt": ingested_at,
            },
            "source": {
                "sessionId": source_session_ids[-1] if source_session_ids else None,
                "characterId": state.character_id,
            },
            "created_at": ingested_at,
        }
    )
    state.queue = []
    state.current_queue_date = None


def _flush_all_queues(state: ReplayState) -> None:
    if state.queue:
        _generate_for_current_queue(state)


def _generation_context_for_queue(state: ReplayState) -> dict[str, Any]:
    queue_date = state.current_queue_date or _local_date(datetime.now(timezone.utc), state.timezone_name)
    start_at, end_at = _coverage_bounds_for_lab(state, queue_date)
    trigger_ids = {str(item.get("sessionId")) for item in state.queue}
    selected: list[dict[str, Any]] = []
    for session in state.eligible_context_sessions:
        started = _parse_dt(session.get("sessionStartTime"))
        is_trigger = str(session.get("sessionId")) in trigger_ids
        if started and not _within_bounds(started, start_at, end_at) and not is_trigger:
            continue
        selected.append({**session, "isTriggerSession": is_trigger or bool(session.get("isTriggerSession"))})
    for queued in state.queue:
        session_id = str(queued.get("sessionId"))
        if session_id and not any(item.get("sessionId") == session_id for item in selected):
            selected.append({key: value for key, value in queued.items() if key != "_decision"})
    selected = sorted(selected, key=lambda item: str(item.get("sessionStartTime") or ""))
    capped = _cap_lab_context_sessions(state, selected, trigger_ids)
    dates = {str(item.get("sessionStartTime") or "")[:10] for item in capped if item.get("sessionStartTime")}
    return {
        "sessions": capped,
        "coverageWindow": {
            "start": start_at.isoformat(),
            "end": end_at.isoformat(),
            "maxDays": state.context_max_days,
        },
        "singleSameDayMoment": len(capped) == 1 and len(dates) <= 1,
    }


def _coverage_bounds_for_lab(state: ReplayState, queue_date: str) -> tuple[datetime, datetime]:
    tz = _zone(state.timezone_name)
    try:
        local_day = datetime.fromisoformat(queue_date).date()
    except ValueError:
        local_day = datetime.now(tz).date()
    end_at = datetime.combine(local_day, time(23, 59, 59), tzinfo=tz).astimezone(timezone.utc)
    start_at = datetime.combine(
        local_day - timedelta(days=max(state.context_max_days - 1, 0)),
        time.min,
        tzinfo=tz,
    ).astimezone(timezone.utc)
    prior_dates = [
        _parse_dt((event.get("time") or {}).get("ingestedAt") or event.get("created_at"))
        for event in state.memory_events
        if event.get("eventType") == "journal_written"
    ]
    prior_dates = [value for value in prior_dates if value]
    if prior_dates:
        latest = max(prior_dates)
        if latest > start_at:
            start_at = latest
    return start_at, end_at


def _within_bounds(value: datetime, start_at: datetime, end_at: datetime) -> bool:
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return start_at <= value.astimezone(timezone.utc) <= end_at


def _cap_lab_context_sessions(
    state: ReplayState,
    sessions: list[dict[str, Any]],
    trigger_ids: set[str],
) -> list[dict[str, Any]]:
    selected = list(sessions)
    while len(selected) > state.context_max_sessions:
        idx = _oldest_non_trigger_index(selected, trigger_ids)
        if idx is None:
            break
        selected.pop(idx)
    while True:
        user_turns, total_turns, chars = _context_size(selected)
        if (
            user_turns <= state.context_max_user_turns
            and total_turns <= state.context_max_total_turns
            and chars <= state.context_max_chars
        ):
            return selected
        idx = _oldest_non_trigger_index(selected, trigger_ids)
        if idx is None:
            return selected
        selected.pop(idx)


def _oldest_non_trigger_index(sessions: list[dict[str, Any]], trigger_ids: set[str]) -> Optional[int]:
    for idx, session in enumerate(sessions):
        if str(session.get("sessionId")) not in trigger_ids:
            return idx
    return None


def _context_size(sessions: list[dict[str, Any]]) -> tuple[int, int, int]:
    user_turns = 0
    total_turns = 0
    chars = 0
    for session in sessions:
        turns = session.get("turns") if isinstance(session.get("turns"), list) else []
        total_turns += len(turns)
        for turn in turns:
            if str(turn.get("speaker") or "").lower() == "user":
                user_turns += 1
            chars += len(str(turn.get("text") or turn.get("content") or turn.get("transcript") or ""))
    return user_turns, total_turns, chars


def _prior_entries_from_memory_events(memory_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    journal_events = [event for event in reversed(memory_events) if event.get("eventType") == "journal_written"]
    for event in journal_events:
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        time_data = event.get("time") if isinstance(event.get("time"), dict) else {}
        created_at = event.get("created_at") or time_data.get("ingestedAt") or time_data.get("occurredAt")
        entry_id = content.get("journalEntryId") or event.get("id") or event.get("event_id")
        entries.append(
            {
                "_id": str(entry_id or ""),
                "entryId": str(entry_id or ""),
                "text": content.get("text") or "",
                "created_at": created_at,
                "displayDate": str(time_data.get("occurredAt") or "")[:10],
                "journalType": content.get("journalType") or "",
                "coverageSummary": content.get("coverageSummary") or [],
                "concreteAnchors": content.get("concreteAnchors") or [],
                "emotionalThemes": content.get("emotionalThemes") or [],
                "avoidRepeating": content.get("avoidRepeating") or [],
                "journalShape": content.get("journal_shape") or content.get("journalShape") or "",
                "mainEvent": content.get("main_event") or content.get("mainEvent") or "",
                "threadReferenceReason": content.get("thread_reference_reason") or "",
            }
        )
    return entries


def _avoid_repeating_from_journals(journals: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for journal in journals:
        raw = journal.get("avoidRepeating") or journal.get("avoid_repeating")
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    return values


def _classification_passes(classification: dict[str, Any]) -> bool:
    if classification.get("dedup_clear") is False:
        return False
    value = str(classification.get("journal_value_type") or "").strip().lower()
    if value and value not in {"strong", "medium"}:
        return False
    return bool(classification.get("should_journal"))


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
    return value.astimezone(_zone(timezone_name)).date().isoformat()


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("UTC")


def _topic_summary_from_queue(queue: list[dict[str, Any]]) -> list[str]:
    topics: list[str] = []
    for session in queue:
        classification = session.get("classification") if isinstance(session.get("classification"), dict) else {}
        for item in classification.get("topicSummary") or []:
            text = str(item).strip()
            if text and text not in topics:
                topics.append(text)
    return topics


def _copy_classification_fields(decision: dict[str, Any], classification: dict[str, Any]) -> None:
    decision["classificationShouldJournal"] = classification.get("should_journal")
    decision["journalValueType"] = classification.get("journal_value_type")
    decision["journalReasonType"] = classification.get("journal_reason_type")
    decision["dedupClear"] = classification.get("dedup_clear")
    decision["classificationReason"] = str(classification.get("reason") or "")
    topics = classification.get("topicSummary") or classification.get("topic_summary") or []
    if isinstance(topics, list):
        decision["topicSummary"] = "; ".join(str(item) for item in topics)
    else:
        decision["topicSummary"] = str(topics)


def _profile_name(character_data: dict[str, Any]) -> Optional[str]:
    profile = character_data.get("profile") if isinstance(character_data.get("profile"), dict) else {}
    return _first_string(profile, ("name", "displayName")) or _first_string(character_data, ("name", "displayName"))


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _thread_reference_needed(memory_events: list[dict[str, Any]]) -> bool:
    journal_events = [event for event in reversed(memory_events) if event.get("eventType") == "journal_written"][:3]
    if len(journal_events) < 3:
        return False
    return all((event.get("content") or {}).get("thread_reference") is False for event in journal_events)


if __name__ == "__main__":
    raise SystemExit(main())
