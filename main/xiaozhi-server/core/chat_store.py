import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from core.utils.firestore_client import (
    get_owner_phone_for_device,
    get_user_profile_by_phone,
    extract_user_profile_fields,
)

DB_PATH = os.environ.get("CHAT_DB_PATH", "/opt/xiaozhi-esp32-server/data/conversations.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_db_dir_exists(path: str) -> None:
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    return column in columns


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_def_sql: str,
) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def_sql}")


def _create_index_if_possible(conn: sqlite3.Connection, sql: str) -> None:
    try:
        conn.execute(sql)
    except sqlite3.OperationalError:
        pass


def _merge_device_ids(existing: str, new_device_id: str) -> str:
    if not new_device_id:
        return (existing or "").strip()
    existing_ids = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if new_device_id not in existing_ids:
        existing_ids.append(new_device_id)
    return ",".join(existing_ids)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT,
            device_ids TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_name TEXT,
            user_id TEXT,
            device_id TEXT,
            created_at TEXT,
            start_time TEXT,
            end_time TEXT,
            analysis_status TEXT,
            memory_status TEXT,
            turns TEXT,
            conversation_id TEXT,
            analysis_json TEXT,
            token_usage INTEGER DEFAULT 0,
            last_active_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            turn_index INTEGER,
            speaker TEXT,
            text TEXT,
            created_at TEXT,
            timestamp TEXT
        )
        """
    )

    _add_column_if_missing(conn, "sessions", "created_at", "TEXT")
    _add_column_if_missing(conn, "sessions", "analysis_json", "TEXT")
    _add_column_if_missing(conn, "sessions", "token_usage", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "sessions", "last_active_at", "TEXT")
    _add_column_if_missing(conn, "sessions", "memory_status", "TEXT")
    _add_column_if_missing(conn, "sessions", "turns", "TEXT")
    _add_column_if_missing(conn, "turns", "created_at", "TEXT")
    _add_column_if_missing(conn, "users", "device_ids", "TEXT")

    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions(user_id, created_at DESC)",
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_turns_session_created ON turns(session_id, created_at)",
    )


@contextmanager
def get_db():
    _ensure_db_dir_exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        _init_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


class SQLiteChatStore:
    def __init__(self, logger=None):
        self.logger = logger
        if self.logger:
            self.logger.info(f"[ChatStore:init] backend=sqlite DB_PATH={DB_PATH}")

    def get_or_create_user(self, user_id: str, name: str, device_id: str = ""):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] get_or_create_user(user_id={user_id}, name={name}, device_id={device_id})"
            )
        with get_db() as db:
            existing = db.execute(
                """
                SELECT device_ids FROM users WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            merged_device_ids = _merge_device_ids(existing[0] if existing else "", device_id)
            db.execute(
                """
                INSERT INTO users (user_id, name, device_ids)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    device_ids = excluded.device_ids
                """,
                (user_id, name, merged_device_ids),
            )

    def create_session(self, *, session_id, user_id, user_name, device_id):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] create_session(session_id={session_id}, user_id={user_id}, user_name={user_name})"
            )
        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO sessions (session_id, user_name, user_id, device_id, created_at, start_time, last_active_at, memory_status, turns)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_name = excluded.user_name,
                    user_id = excluded.user_id,
                    device_id = excluded.device_id,
                    created_at = excluded.created_at,
                    start_time = excluded.start_time,
                    end_time = NULL,
                    analysis_status = NULL,
                    memory_status = 'pending',
                    turns = COALESCE(sessions.turns, '[]'),
                    last_active_at = excluded.last_active_at
                """,
                (
                    session_id,
                    user_name,
                    user_id,
                    device_id,
                    _now_iso(),
                    _now_iso(),
                    _now_iso(),
                    "pending",
                    "[]",
                ),
            )

            if self.logger:
                self.logger.info(
                    f"[ChatStore:sqlite] create_session rowcount={cur.rowcount}"
                )

    def insert_turn(self, session_id, turn_index, speaker, text):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] insert_turn(session_id={session_id}, turn_index={turn_index}, speaker={speaker}, text_len={len(text)})"
            )
        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO turns (session_id, turn_index, speaker, text, created_at, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_index,
                    speaker,
                    text,
                    _now_iso(),
                    _now_iso(),
                ),
            )
            db.execute(
                """
                UPDATE sessions
                SET last_active_at = ?,
                    turns = ?
                WHERE session_id = ?
                """,
                (
                    _now_iso(),
                    self._append_turn_id_to_json_array(db, session_id, cur.lastrowid),
                    session_id,
                ),
            )
            if self.logger:
                self.logger.info(f"[ChatStore:sqlite] insert_turn rowcount={cur.rowcount}")

    def _append_turn_id_to_json_array(self, db: sqlite3.Connection, session_id: str, turn_id) -> str:
        existing_row = db.execute(
            """
            SELECT turns FROM sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        existing_turns = []
        if existing_row and existing_row[0]:
            try:
                parsed = json.loads(existing_row[0])
                if isinstance(parsed, list):
                    existing_turns = parsed
            except Exception:
                existing_turns = []
        existing_turns.append(turn_id)
        return json.dumps(existing_turns, ensure_ascii=False)

    def end_session(self, session_id, character_id=None):
        if self.logger:
            self.logger.info(f"[ChatStore:sqlite] end_session(session_id={session_id}, character_id={character_id})")
        with get_db() as db:
            cur = db.execute(
                """
                UPDATE sessions
                SET end_time = ?,
                    analysis_status = 'pending',
                    memory_status = 'pending',
                    character_id = COALESCE(?, character_id)
                WHERE session_id = ?
                """,
                (_now_iso(), character_id, session_id),
            )

            if self.logger:
                self.logger.info(f"[ChatStore:sqlite] end_session rowcount={cur.rowcount}")

    def delete_session(self, session_id):
        if self.logger:
            self.logger.info(f"[ChatStore:sqlite] delete_session(session_id={session_id})")
        with get_db() as db:
            turns_cur = db.execute(
                """
                DELETE FROM turns
                WHERE session_id = ?
                """,
                (session_id,),
            )
            session_cur = db.execute(
                """
                DELETE FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            )
            if self.logger:
                self.logger.info(
                    f"[ChatStore:sqlite] delete_session turns_rowcount={turns_cur.rowcount} session_rowcount={session_cur.rowcount}"
                )

    def update_session_conversation_id(self, session_id: str, conversation_id: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] update_session_conversation_id(session_id={session_id}, conversation_id={conversation_id})"
            )

        with get_db() as db:
            cur = db.execute(
                """
                UPDATE sessions
                SET conversation_id = ?
                WHERE session_id = ?
                """,
                (conversation_id, session_id),
            )

            if self.logger:
                self.logger.info(
                    f"[ChatStore:sqlite] update_session_conversation_id rowcount={cur.rowcount}"
                )

    def ensure_memory_profile_identity(self, user_id: str, device_id: str = ""):
        return

    def get_system_memory_block(self, user_id: str) -> str:
        return ""


class SupabaseChatStore:
    def __init__(self, logger=None):
        self.logger = logger
        self.base_url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
        self.service_role_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self.timeout_seconds = int(os.environ.get("SUPABASE_TIMEOUT_SECONDS", "10"))
        self.users_table = os.environ.get("SUPABASE_USERS_TABLE", "users")
        self.sessions_table = os.environ.get("SUPABASE_SESSIONS_TABLE", "sessions")
        self.turns_table = os.environ.get("SUPABASE_TURNS_TABLE", "turns")
        self.memory_read_model_table = os.environ.get(
            "SUPABASE_MEMORY_READ_MODEL_TABLE", "memory_read_model"
        )

        self.headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

        if self.logger:
            self.logger.info(
                f"[ChatStore:init] backend=supabase base_url={self.base_url}, users_table={self.users_table}, sessions_table={self.sessions_table}, turns_table={self.turns_table}, memory_read_model_table={self.memory_read_model_table}"
            )

    def is_configured(self) -> bool:
        return bool(self.base_url and self.service_role_key)

    def _insert(self, table: str, payload: dict, return_row: bool = False):
        url = f"{self.base_url}/rest/v1/{table}"
        prefer_header = "return=representation" if return_row else "return=minimal"
        response = requests.post(
            url,
            headers={**self.headers, "Prefer": prefer_header},
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase insert failed table={table} status={response.status_code} body={response.text}"
            )
        if return_row:
            rows = response.json()
            if isinstance(rows, list) and rows:
                return rows[0]
            return None

    def _upsert(self, table: str, payload: dict, on_conflict: str):
        url = f"{self.base_url}/rest/v1/{table}?on_conflict={quote(on_conflict, safe='')}"
        response = requests.post(
            url,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase upsert failed table={table} status={response.status_code} body={response.text}"
            )

    def _update_eq(self, table: str, field: str, value: str, payload: dict):
        url = f"{self.base_url}/rest/v1/{table}?{field}=eq.{quote(str(value), safe='')}"
        response = requests.patch(
            url,
            headers={**self.headers, "Prefer": "return=minimal"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase update failed table={table} filter={field}=eq.{value} status={response.status_code} body={response.text}"
            )

    def _delete_eq(self, table: str, field: str, value: str):
        url = f"{self.base_url}/rest/v1/{table}?{field}=eq.{quote(str(value), safe='')}"
        response = requests.delete(
            url,
            headers={**self.headers, "Prefer": "return=minimal"},
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase delete failed table={table} filter={field}=eq.{value} status={response.status_code} body={response.text}"
            )

    def _select_eq(self, table: str, field: str, value: str):
        url = f"{self.base_url}/rest/v1/{table}?{field}=eq.{quote(str(value), safe='')}&select=*"
        response = requests.get(
            url,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase select failed table={table} filter={field}=eq.{value} status={response.status_code} body={response.text}"
            )
        rows = response.json()
        if rows:
            return rows[0]
        return None

    def _build_identity_from_firestore(self, user_id: str, device_id: str = "") -> dict:
        identity = {
            "name": "",
            "pronouns": "",
            "timezone": "",
            "city": "",
            "birthday": "",
        }

        owner_phone = None
        if device_id:
            try:
                owner_phone = get_owner_phone_for_device(device_id)
            except Exception:
                owner_phone = None

        if not owner_phone and isinstance(user_id, str):
            if user_id.startswith("+") or user_id.isdigit():
                owner_phone = user_id

        if not owner_phone:
            return identity

        user_doc = get_user_profile_by_phone(owner_phone) or {}
        user_fields = extract_user_profile_fields(user_doc)

        identity["name"] = (user_fields.get("name") or "").strip()
        identity["pronouns"] = (user_fields.get("pronouns") or "").strip()
        identity["timezone"] = (user_fields.get("timezone") or "").strip()
        identity["birthday"] = (user_fields.get("birthday") or "").strip()

        city = user_doc.get("city")
        if isinstance(city, str):
            identity["city"] = city.strip()

        return identity

    def _build_initial_system_memory_block(self, identity: dict) -> str:
        identity = identity or {}
        name = (identity.get("name") or "").strip() or "unknown"
        pronouns = (identity.get("pronouns") or "").strip() or "unknown"
        timezone = (identity.get("timezone") or "").strip() or "unknown"

        stable_facts = []
        city = (identity.get("city") or "").strip()
        birthday = (identity.get("birthday") or "").strip()
        if city:
            stable_facts.append(f"city: {city}")
        if birthday:
            stable_facts.append(f"birthday: {birthday}")
        if not stable_facts:
            stable_facts.append("none yet")

        facts_text = "\n".join([f"- {fact}" for fact in stable_facts])
        return (
            "## Who they are\n"
            f"[{name}, {pronouns}, {timezone}, stable identity facts]\n\n"
            "stable identity facts:\n"
            f"{facts_text}"
        )

    def _build_memory_read_model_payload(self, user_id: str, device_id: str = "") -> dict:
        identity = self._build_identity_from_firestore(user_id=user_id, device_id=device_id)
        system_memory_block = self._build_initial_system_memory_block(identity)
        return {
            "user_id": user_id,
            "profile": {
                "identity": identity,
                "longTermFacts": [],
                "peoplePetsPlaces": [],
                "ongoingTopics": [],
                "importantDates": [],
                "characterStyleHints": [],
            },
            "active_context": {
                "recentSummary": "",
                "last7dHighlights": [],
                "openLoops": [],
                "dueReminders": [],
            },
            "modality_digests": {
                "conversation": "",
                "audio": "",
                "photo": "",
                "reminder": "",
                "cosmicDraw": "",
            },
            "prompt_pack": {
                "systemMemoryBlock": system_memory_block,
                "structuredFacts": [],
            },
            "stats": {
                "eventCount": 0,
                "lastSessionId": "",
            },
        }

    def _ensure_memory_read_model(self, user_id: str, device_id: str = ""):
        if not user_id:
            return

        existing = self._select_eq(self.memory_read_model_table, "user_id", user_id)
        if existing:
            return

        payload = self._build_memory_read_model_payload(user_id=user_id, device_id=device_id)
        try:
            self._insert(self.memory_read_model_table, payload)
        except RuntimeError as e:
            if "status=409" in str(e):
                return
            raise

    def ensure_memory_profile_identity(self, user_id: str, device_id: str = ""):
        if not user_id:
            return

        existing = self._select_eq(self.memory_read_model_table, "user_id", user_id)
        if not existing:
            self._ensure_memory_read_model(user_id=user_id, device_id=device_id)
            return

        profile = existing.get("profile") if isinstance(existing.get("profile"), dict) else {}
        identity = profile.get("identity") if isinstance(profile.get("identity"), dict) else {}

        keys_to_fill = ("name", "pronouns", "city", "timezone", "birthday")

        def _is_empty(value):
            if value is None:
                return True
            if isinstance(value, str):
                return value.strip() == ""
            return False

        needs_fill = any(_is_empty(identity.get(k)) for k in keys_to_fill)
        if not needs_fill:
            return

        firestore_identity = self._build_identity_from_firestore(user_id=user_id, device_id=device_id)

        updated_identity = dict(identity)
        changed = False
        for key in keys_to_fill:
            current_value = updated_identity.get(key)
            fetched_value = firestore_identity.get(key)
            if _is_empty(current_value) and isinstance(fetched_value, str) and fetched_value.strip():
                updated_identity[key] = fetched_value.strip()
                changed = True

        if not changed:
            return

        updated_profile = dict(profile)
        updated_profile["identity"] = updated_identity

        self._update_eq(
            self.memory_read_model_table,
            "user_id",
            user_id,
            {
                "profile": updated_profile,
                "updated_at": _now_iso(),
            },
        )

    def get_system_memory_block(self, user_id: str) -> str:
        if not user_id:
            return ""

        existing = self._select_eq(self.memory_read_model_table, "user_id", user_id)
        if not isinstance(existing, dict):
            return ""

        prompt_pack = existing.get("prompt_pack") if isinstance(existing.get("prompt_pack"), dict) else {}
        memory_block = prompt_pack.get("systemMemoryBlock")
        if isinstance(memory_block, str):
            return memory_block

        profile = existing.get("profile") if isinstance(existing.get("profile"), dict) else {}
        legacy_block = profile.get("systemMemoryBlock")
        if isinstance(legacy_block, str):
            return legacy_block

        return ""

    def get_or_create_user(self, user_id: str, name: str, device_id: str = ""):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] get_or_create_user(user_id={user_id}, name={name}, device_id={device_id})"
            )
        existing = self._select_eq(self.users_table, "user_id", user_id)
        merged_device_ids = _merge_device_ids(
            (existing or {}).get("device_ids", ""),
            device_id,
        )
        self._upsert(
            self.users_table,
            {"user_id": user_id, "name": name, "device_ids": merged_device_ids},
            on_conflict="user_id",
        )
        try:
            self._ensure_memory_read_model(user_id, device_id)
            if self.logger:
                self.logger.info(
                    f"[ChatStore:supabase] ensured memory_read_model(user_id={user_id})"
                )
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"[ChatStore:supabase] ensure memory_read_model failed for user_id={user_id}: {e}"
                )

    def create_session(self, *, session_id, user_id, user_name, device_id):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] create_session(session_id={session_id}, user_id={user_id}, user_name={user_name})"
            )
        now_iso = _now_iso()
        payload = {
            "session_id": session_id,
            "user_name": user_name,
            "user_id": user_id,
            "device_id": device_id,
            "created_at": now_iso,
            "start_time": now_iso,
            "last_active_at": now_iso,
            "memory_status": "pending",
            "turns": [],
        }
        try:
            self._insert(self.sessions_table, payload)
        except RuntimeError as e:
            if "status=409" in str(e):
                self._update_eq(
                    self.sessions_table,
                    "session_id",
                    session_id,
                    {
                        "user_name": user_name,
                        "user_id": user_id,
                        "device_id": device_id,
                        "created_at": now_iso,
                        "start_time": now_iso,
                        "end_time": None,
                        "analysis_status": None,
                        "last_active_at": now_iso,
                        "memory_status": "pending",
                    },
                )
            else:
                raise

    def insert_turn(self, session_id, turn_index, speaker, text):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] insert_turn(session_id={session_id}, turn_index={turn_index}, speaker={speaker}, text_len={len(text)})"
            )
        inserted_row = self._insert(
            self.turns_table,
            {
                "session_id": session_id,
                "turn_index": turn_index,
                "speaker": speaker,
                "text": text,
                "created_at": _now_iso(),
                "timestamp": _now_iso(),
            },
            return_row=True,
        )
        turn_id = self._extract_turn_id(inserted_row, default=turn_index)
        self._update_eq(
            self.sessions_table,
            "session_id",
            session_id,
            {
                "last_active_at": _now_iso(),
                "turns": self._append_turn_id_to_supabase_array(session_id, turn_id),
            },
        )

    def _append_turn_id_to_supabase_array(self, session_id: str, turn_id):
        existing = self._select_eq(self.sessions_table, "session_id", session_id) or {}
        turns = existing.get("turns")
        if not isinstance(turns, list):
            turns = []
        turns.append(turn_id)
        return turns

    def _extract_turn_id(self, inserted_row: dict, default):
        if isinstance(inserted_row, dict):
            if "id" in inserted_row and inserted_row.get("id") is not None:
                return inserted_row.get("id")
            if "turn_id" in inserted_row and inserted_row.get("turn_id") is not None:
                return inserted_row.get("turn_id")
        return default

    def end_session(self, session_id, character_id=None):
        if self.logger:
            self.logger.info(f"[ChatStore:supabase] end_session(session_id={session_id}, character_id={character_id})")
        payload = {
            "end_time": _now_iso(),
            "analysis_status": "pending",
            "memory_status": "pending",
        }
        if character_id is not None:
            payload["character_id"] = character_id
        self._update_eq(
            self.sessions_table,
            "session_id",
            session_id,
            payload,
        )

    def delete_session(self, session_id):
        if self.logger:
            self.logger.info(f"[ChatStore:supabase] delete_session(session_id={session_id})")
        self._delete_eq(self.turns_table, "session_id", session_id)
        self._delete_eq(self.sessions_table, "session_id", session_id)

    def update_session_conversation_id(self, session_id: str, conversation_id: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] update_session_conversation_id(session_id={session_id}, conversation_id={conversation_id})"
            )
        self._update_eq(
            self.sessions_table,
            "session_id",
            session_id,
            {"conversation_id": conversation_id},
        )


class ChatStore:
    def __init__(self, logger=None):
        self.logger = logger
        backend = (os.environ.get("CHAT_STORE_BACKEND", "auto") or "auto").strip().lower()

        self.store = None
        if backend in ("supabase", "auto"):
            supabase_store = SupabaseChatStore(logger=logger)
            if supabase_store.is_configured():
                self.store = supabase_store
            else:
                if self.logger and backend == "supabase":
                    self.logger.warning(
                        "[ChatStore] CHAT_STORE_BACKEND=supabase but SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY missing, falling back to sqlite"
                    )

        if self.store is None:
            self.store = SQLiteChatStore(logger=logger)

    def get_or_create_user(self, user_id: str, name: str, device_id: str = ""):
        try:
            self.store.get_or_create_user(user_id=user_id, name=name, device_id=device_id)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] get_or_create_user failed: {e}")

    def create_session(self, *, session_id, user_id, user_name, device_id):
        try:
            self.store.create_session(
                session_id=session_id,
                user_id=user_id,
                user_name=user_name,
                device_id=device_id,
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] create_session failed: {e}")

    def insert_turn(self, session_id, turn_index, speaker, text):
        try:
            self.store.insert_turn(
                session_id=session_id,
                turn_index=turn_index,
                speaker=speaker,
                text=text,
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] insert_turn failed: {e}")

    def end_session(self, session_id, character_id=None):
        try:
            self.store.end_session(session_id=session_id, character_id=character_id)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] end_session failed: {e}")

    def delete_session(self, session_id):
        try:
            self.store.delete_session(session_id=session_id)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] delete_session failed: {e}")

    def update_session_conversation_id(self, session_id: str, conversation_id: str):
        try:
            self.store.update_session_conversation_id(
                session_id=session_id,
                conversation_id=conversation_id,
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] update_session_conversation_id failed: {e}")

    def ensure_memory_profile_identity(self, user_id: str, device_id: str = ""):
        try:
            if hasattr(self.store, "ensure_memory_profile_identity"):
                self.store.ensure_memory_profile_identity(user_id=user_id, device_id=device_id)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] ensure_memory_profile_identity failed: {e}")

    def get_system_memory_block(self, user_id: str) -> str:
        try:
            if hasattr(self.store, "get_system_memory_block"):
                result = self.store.get_system_memory_block(user_id=user_id)
                return result if isinstance(result, str) else ""
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] get_system_memory_block failed: {e}")
        return ""

