#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'psycopg'. Install with: pip install 'psycopg[binary]'"
    ) from exc

try:
    from google.cloud import firestore
except ImportError:  # pragma: no cover
    firestore = None


DEFAULT_API_BASE_URL = "http://34.44.233.1:8001"
SCHEMA_PATH = Path(__file__).with_name("bootstrap_memory_schema.sql")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Supabase memory tables from the historical conversation API."
    )
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("HISTORY_API_BASE_URL", DEFAULT_API_BASE_URL),
        help="Base URL for the historical conversation API.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Target Postgres/Supabase DATABASE_URL.",
    )
    parser.add_argument(
        "--firestore-project-id",
        default=os.environ.get("FIRESTORE_PROJECT_ID", ""),
        help="Optional Firestore project id for resolving phone-number user ids from device docs.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=200,
        help="Page size for paginated API reads.",
    )
    parser.add_argument(
        "--limit-sessions",
        type=int,
        default=0,
        help="Optional max number of sessions to import after sorting oldest first.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and summarize source data only; do not write to Postgres.",
    )
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="Do not run the schema bootstrap SQL before backfilling.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-session progress.",
    )
    return parser.parse_args()


def load_schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def ensure_schema(pg_conn: psycopg.Connection) -> None:
    pg_conn.execute(load_schema_sql())
    pg_conn.commit()


def fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url) as response:
        return json.load(response)


def build_url(base_url: str, path: str, **params: Any) -> str:
    query = {key: value for key, value in params.items() if value is not None}
    encoded = urllib.parse.urlencode(query)
    return f"{base_url.rstrip('/')}{path}{'?' + encoded if encoded else ''}"


def paginate_items(base_url: str, path: str, page_size: int) -> Iterable[dict[str, Any]]:
    offset = 0
    while True:
        payload = fetch_json(build_url(base_url, path, limit=page_size, offset=offset))
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            break
        for item in items:
            yield item
        offset += len(items)
        total = payload.get("total")
        if total is not None and offset >= total:
            break


def session_exists(pg_conn: psycopg.Connection, session_id: str) -> bool:
    row = pg_conn.execute(
        "select 1 from public.sessions where session_id = %s",
        (session_id,),
    ).fetchone()
    return row is not None


def get_existing_device_ids(pg_conn: psycopg.Connection, user_id: str) -> str:
    row = pg_conn.execute(
        "select device_ids from public.users where user_id = %s",
        (user_id,),
    ).fetchone()
    return (row["device_ids"] or "") if row else ""


def merge_device_ids(existing: str, new_device_ids: list[str]) -> str:
    items = [item.strip() for item in (existing or "").split(",") if item.strip()]
    for device_id in new_device_ids:
        device_id = (device_id or "").strip()
        if device_id and device_id not in items:
            items.append(device_id)
    return ",".join(items)


def upsert_user(
    pg_conn: psycopg.Connection, user_id: str, name: str, device_ids: list[str]
) -> None:
    merged_device_ids = merge_device_ids(
        get_existing_device_ids(pg_conn, user_id),
        device_ids,
    )
    pg_conn.execute(
        """
        insert into public.users (user_id, name, device_ids)
        values (%s, %s, %s)
        on conflict (user_id) do update
        set name = excluded.name,
            device_ids = excluded.device_ids
        """,
        (user_id, name, merged_device_ids),
    )


def insert_session(pg_conn: psycopg.Connection, payload: dict[str, Any]) -> None:
    pg_conn.execute(
        """
        insert into public.sessions (
          session_id,
          user_name,
          user_id,
          device_id,
          created_at,
          start_time,
          end_time,
          analysis_status,
          memory_status,
          turns,
          conversation_id,
          analysis_json,
          token_usage,
          last_active_at
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, '[]'::jsonb, %s, %s, %s, %s)
        """,
        (
            payload["session_id"],
            payload["user_name"],
            payload["user_id"],
            payload["device_id"],
            payload["created_at"],
            payload["start_time"],
            payload["end_time"],
            payload["analysis_status"],
            payload["memory_status"],
            payload["conversation_id"],
            json.dumps(payload["analysis_json"]) if payload["analysis_json"] is not None else None,
            payload["token_usage"],
            payload["last_active_at"],
        ),
    )


def insert_turns(
    pg_conn: psycopg.Connection, session_id: str, turns: list[dict[str, Any]]
) -> list[int]:
    inserted_ids: list[int] = []
    for turn in turns:
        row = pg_conn.execute(
            """
            insert into public.turns (
              session_id,
              turn_index,
              speaker,
              text,
              created_at,
              "timestamp"
            )
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                session_id,
                turn["turn_index"],
                turn["speaker"],
                turn["text"],
                turn["timestamp"],
                turn["timestamp"],
            ),
        ).fetchone()
        inserted_ids.append(int(row["id"]))
    return inserted_ids


def update_session_turn_refs(
    pg_conn: psycopg.Connection, session_id: str, turn_ids: list[int]
) -> None:
    pg_conn.execute(
        """
        update public.sessions
        set turns = %s::jsonb
        where session_id = %s
        """,
        (json.dumps([str(turn_id) for turn_id in turn_ids]), session_id),
    )


def make_firestore_client(project_id: str):
    if firestore is None:
        return None
    return firestore.Client(project=project_id or None)


def normalize_phone(value: str) -> str:
    return value.strip()


def resolve_owner_phone(db: Any, device_id: str) -> str:
    if not db or not device_id:
        return ""
    try:
        direct = db.collection("devices").document(device_id).get()
        if direct.exists:
            data = direct.to_dict() or {}
            owner_phone = data.get("ownerPhone")
            if isinstance(owner_phone, str) and owner_phone.strip():
                return normalize_phone(owner_phone)
    except Exception:
        return ""
    return ""


def coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def resolve_user_records(
    base_url: str, page_size: int, firestore_project_id: str
) -> tuple[dict[str, dict[str, Any]], int]:
    db = make_firestore_client(firestore_project_id)
    aliases: dict[str, dict[str, Any]] = {}
    fallback_count = 0

    for item in paginate_items(base_url, "/api/intel/users", page_size):
        user_key = str(item.get("user_key") or "").strip()
        user_name = str(item.get("user_name") or user_key or "").strip()
        device_ids = [str(device_id).strip() for device_id in (item.get("device_ids") or []) if str(device_id).strip()]

        owner_phone = ""
        for device_id in device_ids:
            owner_phone = resolve_owner_phone(db, device_id)
            if owner_phone:
                break

        resolved_user_id = owner_phone or user_key or user_name
        if not owner_phone:
            fallback_count += 1

        record = {
            "user_id": resolved_user_id,
            "user_name": user_name or resolved_user_id,
            "device_ids": device_ids,
        }

        for alias in {user_key, user_name, resolved_user_id}:
            if alias:
                aliases[alias] = record

    return aliases, fallback_count


def summarize_analysis(detail: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "session_tag",
        "outcome",
        "quality_score",
        "user_sentiment",
        "ending_type_simple",
        "session_depth",
        "primary_language",
        "mode",
        "gpt_intent",
        "gpt_outcome_resolved",
        "gpt_outcome_summary",
        "emotion_start",
        "emotion_end",
        "has_log_data",
    ]
    return {"source": "history_api", **{key: detail.get(key) for key in keys if key in detail}}


def flatten_turns(detail: dict[str, Any]) -> list[dict[str, Any]]:
    raw_turns = detail.get("turns") or []
    flattened: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_turns):
        if isinstance(raw, dict) and {"speaker", "text", "timestamp"} <= set(raw.keys()):
            flattened.append(
                {
                    "speaker": raw.get("speaker") or "unknown",
                    "text": raw.get("text") or "",
                    "timestamp": raw.get("timestamp"),
                    "_source_index": idx,
                }
            )
            continue

        user_text = raw.get("user_text") if isinstance(raw, dict) else None
        bot_text = raw.get("bot_text") if isinstance(raw, dict) else None
        user_timestamp = raw.get("user_timestamp") if isinstance(raw, dict) else None
        bot_timestamp = raw.get("bot_timestamp") if isinstance(raw, dict) else None

        if user_text:
            flattened.append(
                {
                    "speaker": "user",
                    "text": user_text,
                    "timestamp": user_timestamp,
                    "_source_index": idx * 2,
                }
            )
        if bot_text:
            flattened.append(
                {
                    "speaker": "assistant",
                    "text": bot_text,
                    "timestamp": bot_timestamp,
                    "_source_index": idx * 2 + 1,
                }
            )

    flattened.sort(
        key=lambda item: (
            item.get("timestamp") or "9999-12-31T23:59:59Z",
            item["_source_index"],
        )
    )

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(flattened, start=1):
        normalized.append(
            {
                "turn_index": index,
                "speaker": item["speaker"],
                "text": item["text"],
                "timestamp": item.get("timestamp"),
            }
        )
    return normalized


def build_session_payload(
    summary: dict[str, Any], detail: dict[str, Any], user_aliases: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    user_alias = str(coalesce(detail.get("user_name"), summary.get("user_name"), detail.get("user"), summary.get("user")) or "").strip()
    user_record = user_aliases.get(user_alias, {})
    device_id = str(coalesce(detail.get("device_id"), summary.get("device_id"), (user_record.get("device_ids") or [None])[0]) or "").strip()
    turns = flatten_turns(detail)

    created_at = coalesce(detail.get("start_time"), summary.get("start_time"), turns[0]["timestamp"] if turns else None, detail.get("end_time"))
    last_active_at = coalesce(detail.get("last_active_at"), detail.get("end_time"), turns[-1]["timestamp"] if turns else None, created_at)
    user_id = str(coalesce(user_record.get("user_id"), user_alias, detail.get("user"), summary.get("user")) or "").strip()
    user_name = str(coalesce(detail.get("user_name"), summary.get("user_name"), user_record.get("user_name"), user_id) or "").strip()

    payload = {
        "session_id": str(coalesce(detail.get("session_id"), summary.get("session_id"))),
        "user_name": user_name,
        "user_id": user_id,
        "device_id": device_id or None,
        "created_at": created_at,
        "start_time": coalesce(detail.get("start_time"), summary.get("start_time"), created_at),
        "end_time": coalesce(detail.get("end_time"), summary.get("end_time"), last_active_at),
        "analysis_status": "done" if detail.get("has_log_data") else None,
        "memory_status": "pending",
        "conversation_id": coalesce(detail.get("conversation_id"), summary.get("conversation_id")),
        "analysis_json": summarize_analysis(detail),
        "token_usage": int(coalesce(detail.get("token_usage"), summary.get("token_usage"), 0) or 0),
        "last_active_at": last_active_at,
        "device_ids": user_record.get("device_ids") or ([device_id] if device_id else []),
    }
    return payload, turns


def main() -> int:
    args = parse_args()
    if not args.database_url and not args.dry_run:
        raise SystemExit("DATABASE_URL is required unless running with --dry-run")

    session_summaries = list(paginate_items(args.api_base_url, "/api/intel/sessions", args.page_size))
    session_summaries.sort(key=lambda item: coalesce(item.get("start_time"), item.get("end_time")) or "")
    if args.limit_sessions > 0:
        session_summaries = session_summaries[: args.limit_sessions]

    user_aliases, fallback_count = resolve_user_records(
        args.api_base_url,
        args.page_size,
        args.firestore_project_id,
    )

    print(
        f"Loaded API summaries: sessions={len(session_summaries)}, user_aliases={len(user_aliases)}, phone_fallback_users={fallback_count}"
    )
    if args.dry_run:
        sample = session_summaries[0] if session_summaries else {}
        print("Dry run sample session:", json.dumps(sample, ensure_ascii=False)[:800])
        return 0

    with psycopg.connect(args.database_url, row_factory=dict_row) as pg_conn:
        if not args.skip_bootstrap:
            ensure_schema(pg_conn)

        imported = 0
        skipped = 0
        for summary in session_summaries:
            session_tag = summary.get("session_tag")
            session_id = summary.get("session_id")
            if not session_tag or not session_id:
                skipped += 1
                continue
            if session_exists(pg_conn, str(session_id)):
                skipped += 1
                continue

            detail = fetch_json(build_url(args.api_base_url, f"/api/intel/sessions/{session_tag}"))
            payload, turns = build_session_payload(summary, detail, user_aliases)
            if not turns:
                skipped += 1
                continue

            upsert_user(
                pg_conn,
                user_id=payload["user_id"],
                name=payload["user_name"],
                device_ids=payload["device_ids"],
            )
            insert_session(pg_conn, payload)
            turn_ids = insert_turns(pg_conn, payload["session_id"], turns)
            update_session_turn_refs(pg_conn, payload["session_id"], turn_ids)
            pg_conn.commit()

            imported += 1
            if args.verbose:
                print(
                    f"Imported session {payload['session_id']} user_id={payload['user_id']} turns={len(turns)}"
                )

        print(
            f"History API backfill complete: imported={imported}, skipped={skipped}, considered={len(session_summaries)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
