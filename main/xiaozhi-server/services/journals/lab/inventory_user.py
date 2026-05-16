from __future__ import annotations

"""Read-only inventory for journal replay lab.

Run from the server container after configuring lab source env vars on the VM:

    docker compose run --rm --no-deps server python -m services.journals.lab.inventory_user \
      --user-id +13854915166 \
      --character-id CH-59cf426a087b4ccc \
      --alias tester_liora \
      --allow-beta-read

This command is the first step before simulating journals over a beta user's
history. It reports counts, date ranges, schema/table names, and readiness
signals. It does not print transcript text and does not write any data.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from google.cloud import firestore

from core.utils.firestore_factory import build_firestore_client
from services.journals.lab.source_supabase import JournalLabSourceSupabase


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only beta-history inventory for journal replay lab")
    parser.add_argument("--user-id", required=True, help="Source Supabase sessions.user_id value")
    parser.add_argument("--character-id", help="Character id to inspect in configured Firestore")
    parser.add_argument("--alias", default="journal_lab_user", help="Pseudonymous label for artifacts and reports")
    parser.add_argument("--allow-beta-read", action="store_true", help="Required explicit confirmation for beta reads")
    parser.add_argument("--session-limit", type=int, default=20000)
    parser.add_argument("--memory-event-limit", type=int, default=20000)
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_beta_read:
        raise SystemExit("Refusing to read beta history without --allow-beta-read")

    client = JournalLabSourceSupabase()
    sessions = client.get_sessions_for_user(args.user_id, limit=args.session_limit)
    session_ids = [str(row.get("session_id")) for row in sessions if row.get("session_id")]
    turn_inventory = client.get_turn_inventory_for_sessions(session_ids)
    memory_events = client.get_memory_events_for_user(args.user_id, limit=args.memory_event_limit)
    read_model = client.get_read_model_for_user(args.user_id)
    firestore_info = _firestore_inventory(args.user_id, args.character_id)

    result = {
        "ok": True,
        "mode": "read_only_inventory_no_transcripts",
        "alias": args.alias,
        "request": {
            "userId": args.user_id,
            "characterId": args.character_id,
            "timeRange": "all_history",
        },
        "sourceSupabase": {
            "host": client.host,
            "tables": client.table_names(),
            "sessions": _session_inventory(sessions),
            "turns": _turn_inventory(session_ids, turn_inventory),
            "memoryEvents": _memory_event_inventory(memory_events),
            "memoryReadModel": _read_model_inventory(read_model),
        },
        "configuredFirestore": firestore_info,
        "replayReadiness": _replay_readiness(
            sessions=sessions,
            session_ids=session_ids,
            turn_inventory=turn_inventory,
            firestore_info=firestore_info,
            character_id=args.character_id,
        ),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human_summary(result)
        print("\nJSON:")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _session_inventory(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    date_values = []
    for row in sessions:
        date_values.extend([row.get("start_time"), row.get("created_at"), row.get("end_time"), row.get("last_active_at")])
    start, end = _iso_min_max(date_values)
    memory_statuses = Counter(str(row.get("memory_status") or "missing") for row in sessions)
    device_ids = sorted({str(row.get("device_id")) for row in sessions if row.get("device_id")})
    return {
        "count": len(sessions),
        "dateRange": {"start": start, "end": end},
        "memoryStatusBreakdown": dict(sorted(memory_statuses.items())),
        "deviceCount": len(device_ids),
        "deviceIdsSample": device_ids[:10],
        "hasMemoryStatusColumn": any("memory_status" in row for row in sessions),
    }


def _turn_inventory(session_ids: list[str], turns: list[dict[str, Any]]) -> dict[str, Any]:
    by_session: Counter[str] = Counter()
    by_speaker: Counter[str] = Counter()
    for row in turns:
        by_session[str(row.get("session_id") or "")] += 1
        by_speaker[str(row.get("speaker") or "unknown")] += 1
    return {
        "totalTurns": len(turns),
        "speakerBreakdown": dict(sorted(by_speaker.items())),
        "sessionsWithTurns": sum(1 for sid in session_ids if by_session.get(sid, 0) > 0),
        "sessionsWithoutTurns": sum(1 for sid in session_ids if by_session.get(sid, 0) == 0),
        "minTurnsPerSession": min((by_session.get(sid, 0) for sid in session_ids), default=0),
        "maxTurnsPerSession": max((by_session.get(sid, 0) for sid in session_ids), default=0),
    }


def _memory_event_inventory(memory_events: list[dict[str, Any]]) -> dict[str, Any]:
    event_types = Counter(str(row.get("event_type") or row.get("eventType") or "missing") for row in memory_events)
    start, end = _iso_min_max([row.get("created_at") for row in memory_events])
    return {
        "count": len(memory_events),
        "dateRange": {"start": start, "end": end},
        "eventTypeBreakdown": dict(sorted(event_types.items())),
    }


def _read_model_inventory(read_model: Optional[dict[str, Any]]) -> dict[str, Any]:
    prompt_pack = read_model.get("prompt_pack") if isinstance(read_model, dict) else None
    if not isinstance(prompt_pack, dict):
        prompt_pack = {}
    return {
        "exists": bool(read_model),
        "hasPromptPack": bool(prompt_pack),
        "hasSystemMemoryBlock": bool(prompt_pack.get("systemMemoryBlock")),
    }


def _firestore_inventory(user_id: str, character_id: Optional[str]) -> dict[str, Any]:
    try:
        db = build_firestore_client()
        user_ref = db.collection("users").document(user_id)
        user_snap = user_ref.get()
        user_data = user_snap.to_dict() or {} if user_snap.exists else {}
        character_ids = [snap.id for snap in user_ref.collection("characters").limit(20).stream()] if user_snap.exists else []
        char_data = {}
        char_exists = False
        if character_id:
            char_snap = user_ref.collection("characters").document(character_id).get()
            char_exists = bool(char_snap.exists)
            char_data = char_snap.to_dict() or {} if char_snap.exists else {}
        profile = char_data.get("profile") if isinstance(char_data.get("profile"), dict) else {}
        return {
            "ok": True,
            "database": getattr(db, "_database_string", None) or "(unknown)",
            "userExists": bool(user_snap.exists),
            "timezone": _first_string(user_data, ("timezone", "timeZone", "timezoneId", "userTimezone")),
            "requestedCharacterExists": char_exists,
            "characterIdsSample": character_ids,
            "requestedCharacterName": char_data.get("name") or profile.get("name"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _replay_readiness(
    *,
    sessions: list[dict[str, Any]],
    session_ids: list[str],
    turn_inventory: list[dict[str, Any]],
    firestore_info: dict[str, Any],
    character_id: Optional[str],
) -> dict[str, Any]:
    notes = []
    if not sessions:
        notes.append("No sessions found for this user_id in source Supabase.")
    if sessions and not turn_inventory:
        notes.append("Sessions found, but no turn rows found for their session_ids.")
    if character_id and firestore_info.get("ok") and not firestore_info.get("requestedCharacterExists"):
        notes.append("Requested character was not found in the configured Firestore database.")
    return {
        "hasSessions": bool(sessions),
        "hasSessionIds": bool(session_ids),
        "hasTurns": bool(turn_inventory),
        "characterIdProvided": bool(character_id),
        "canReplay": bool(sessions and session_ids and turn_inventory),
        "notes": notes,
    }


def _print_human_summary(result: dict[str, Any]) -> None:
    source = result["sourceSupabase"]
    readiness = result["replayReadiness"]
    print(f"Journal lab inventory for {result['alias']}")
    print(f"Source Supabase: {source['host']}")
    print(f"Sessions: {source['sessions']['count']}")
    print(f"Turns: {source['turns']['totalTurns']}")
    print(f"Memory events: {source['memoryEvents']['count']}")
    print(f"Can replay: {readiness['canReplay']}")
    for note in readiness["notes"]:
        print(f"- {note}")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso_min_max(values: Iterable[Any]) -> tuple[Optional[str], Optional[str]]:
    parsed = [dt for dt in (_parse_dt(value) for value in values) if dt]
    if not parsed:
        return None, None
    return min(parsed).isoformat(), max(parsed).isoformat()


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
