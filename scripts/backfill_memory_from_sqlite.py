#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency 'psycopg'. Install with: pip install 'psycopg[binary]'"
    ) from exc


DEFAULT_SQLITE_PATH = "/opt/xiaozhi-esp32-server/data/conversations.db"
SCHEMA_PATH = Path(__file__).with_name("bootstrap_memory_schema.sql")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Supabase memory tables from the legacy local SQLite conversation DB."
    )
    parser.add_argument(
        "--sqlite-path",
        default=os.environ.get("SQLITE_CHAT_DB_PATH", DEFAULT_SQLITE_PATH),
        help="Path to the legacy conversations.db SQLite file.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Target Postgres/Supabase DATABASE_URL.",
    )
    parser.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="Create/repair target schema and exit without copying data.",
    )
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="Do not run the schema bootstrap SQL before backfilling.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of sessions to backfill.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-session progress.",
    )
    return parser.parse_args()


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name = ?",
        (table,),
    ).fetchone()
    return row is not None


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def coalesce_timestamp(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", "null"):
            return value
    return None


def load_schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def ensure_schema(pg_conn: psycopg.Connection) -> None:
    pg_conn.execute(load_schema_sql())
    pg_conn.commit()


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


def merge_device_ids(existing: str, new_device_id: str) -> str:
    if not new_device_id:
        return (existing or "").strip()
    items = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if new_device_id not in items:
        items.append(new_device_id)
    return ",".join(items)


def upsert_user(pg_conn: psycopg.Connection, user_id: str, name: str, device_id: str) -> None:
    merged_device_ids = merge_device_ids(
        get_existing_device_ids(pg_conn, user_id),
        device_id,
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


def load_turns(sqlite_conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return list(
        sqlite_conn.execute(
            """
            select *
            from turns
            where session_id = ?
            order by coalesce(turn_index, 0) asc, coalesce(created_at, timestamp) asc, id asc
            """,
            (session_id,),
        )
    )


def normalize_session_payload(session: sqlite3.Row, turns: list[sqlite3.Row]) -> dict[str, Any]:
    first_turn_ts = coalesce_timestamp(
        *(turn["created_at"] if "created_at" in turn.keys() else None for turn in turns),
        *(turn["timestamp"] if "timestamp" in turn.keys() else None for turn in turns),
    )
    last_turn_ts = None
    for turn in reversed(turns):
        last_turn_ts = coalesce_timestamp(
            turn["created_at"] if "created_at" in turn.keys() else None,
            turn["timestamp"] if "timestamp" in turn.keys() else None,
        )
        if last_turn_ts:
            break

    return {
        "session_id": session["session_id"],
        "user_name": session["user_name"] if "user_name" in session.keys() else None,
        "user_id": session["user_id"] if "user_id" in session.keys() else None,
        "device_id": session["device_id"] if "device_id" in session.keys() else None,
        "created_at": coalesce_timestamp(
            session["created_at"] if "created_at" in session.keys() else None,
            session["start_time"] if "start_time" in session.keys() else None,
            first_turn_ts,
        ),
        "start_time": coalesce_timestamp(
            session["start_time"] if "start_time" in session.keys() else None,
            session["created_at"] if "created_at" in session.keys() else None,
            first_turn_ts,
        ),
        "end_time": session["end_time"] if "end_time" in session.keys() else None,
        "analysis_status": session["analysis_status"] if "analysis_status" in session.keys() else None,
        "memory_status": "pending",
        "conversation_id": session["conversation_id"] if "conversation_id" in session.keys() else None,
        "analysis_json": session["analysis_json"] if "analysis_json" in session.keys() else None,
        "token_usage": session["token_usage"] if "token_usage" in session.keys() else 0,
        "last_active_at": coalesce_timestamp(
            session["last_active_at"] if "last_active_at" in session.keys() else None,
            last_turn_ts,
            session["end_time"] if "end_time" in session.keys() else None,
            session["start_time"] if "start_time" in session.keys() else None,
        ),
    }


def insert_session(pg_conn: psycopg.Connection, payload: dict[str, Any]) -> None:
    analysis_json = payload["analysis_json"]
    if isinstance(analysis_json, str):
        try:
            analysis_json = json.loads(analysis_json)
        except json.JSONDecodeError:
            analysis_json = {"legacy": payload["analysis_json"]}

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
            json.dumps(analysis_json) if analysis_json is not None else None,
            payload["token_usage"],
            payload["last_active_at"],
        ),
    )


def insert_turns(
    pg_conn: psycopg.Connection, session_id: str, turns: list[sqlite3.Row]
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
                turn["turn_index"] if "turn_index" in turn.keys() else None,
                turn["speaker"] if "speaker" in turn.keys() else None,
                turn["text"] if "text" in turn.keys() else None,
                coalesce_timestamp(
                    turn["created_at"] if "created_at" in turn.keys() else None,
                    turn["timestamp"] if "timestamp" in turn.keys() else None,
                ),
                turn["timestamp"] if "timestamp" in turn.keys() else None,
            ),
        ).fetchone()
        inserted_ids.append(int(row["id"]))
    return inserted_ids


def update_session_turn_ids(
    pg_conn: psycopg.Connection, session_id: str, turn_ids: list[int]
) -> None:
    pg_conn.execute(
        """
        update public.sessions
        set turns = %s::jsonb
        where session_id = %s
        """,
        (json.dumps(turn_ids), session_id),
    )


def main() -> int:
    args = parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    if not args.skip_bootstrap and not SCHEMA_PATH.exists():
        print(f"Schema file not found: {SCHEMA_PATH}", file=sys.stderr)
        return 2

    sqlite_path = Path(args.sqlite_path)
    if not args.bootstrap_only and not sqlite_path.exists():
        print(f"SQLite DB not found: {sqlite_path}", file=sys.stderr)
        return 2

    with psycopg.connect(
        args.database_url,
        row_factory=dict_row,
        prepare_threshold=None,
    ) as pg_conn:
        if not args.skip_bootstrap:
            ensure_schema(pg_conn)
            print("Supabase schema ensured.")

        if args.bootstrap_only:
            return 0

        sqlite_conn = sqlite3.connect(str(sqlite_path))
        sqlite_conn.row_factory = sqlite3.Row
        try:
            if not sqlite_table_exists(sqlite_conn, "sessions") or not sqlite_table_exists(sqlite_conn, "turns"):
                print("SQLite DB does not contain the expected sessions/turns tables.", file=sys.stderr)
                return 2

            session_rows = list(sqlite_conn.execute("select * from sessions"))
            session_rows.sort(
                key=lambda row: (
                    coalesce_timestamp(
                        row["created_at"] if "created_at" in row.keys() else None,
                        row["start_time"] if "start_time" in row.keys() else None,
                        row["end_time"] if "end_time" in row.keys() else None,
                    )
                    or ""
                )
            )

            if args.limit > 0:
                session_rows = session_rows[: args.limit]

            copied = 0
            skipped_empty = 0
            skipped_existing = 0

            for session in session_rows:
                session_id = session["session_id"]
                turns = load_turns(sqlite_conn, session_id)
                if not turns:
                    skipped_empty += 1
                    if args.verbose:
                        print(f"skip empty session {session_id}")
                    continue

                if session_exists(pg_conn, session_id):
                    skipped_existing += 1
                    if args.verbose:
                        print(f"skip existing session {session_id}")
                    continue

                payload = normalize_session_payload(session, turns)
                user_id = payload["user_id"] or f"device:{payload['device_id'] or session_id}"
                user_name = payload["user_name"] or user_id
                payload["user_id"] = user_id
                payload["user_name"] = user_name

                with pg_conn.transaction():
                    upsert_user(pg_conn, user_id, user_name, payload["device_id"] or "")
                    insert_session(pg_conn, payload)
                    turn_ids = insert_turns(pg_conn, session_id, turns)
                    update_session_turn_ids(pg_conn, session_id, turn_ids)

                copied += 1
                if args.verbose:
                    print(f"copied session {session_id} ({len(turns)} turns)")

            pg_conn.commit()
            print(
                json.dumps(
                    {
                        "copied_sessions": copied,
                        "skipped_empty_sessions": skipped_empty,
                        "skipped_existing_sessions": skipped_existing,
                        "sqlite_path": str(sqlite_path),
                    },
                    indent=2,
                )
            )
            return 0
        finally:
            sqlite_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
