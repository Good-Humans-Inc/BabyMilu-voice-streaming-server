import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote

import requests

DB_PATH = os.environ.get("CHAT_DB_PATH", "/opt/xiaozhi-esp32-server/data/conversations.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_db_dir_exists(path: str) -> None:
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions(user_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session_created ON turns(session_id, created_at)")


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

    def get_or_create_user(self, user_id: str, name: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] get_or_create_user(user_id={user_id}, name={name})"
            )
        with get_db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO users (user_id, name)
                VALUES (?, ?)
                """,
                (user_id, name),
            )

    def create_session(self, *, session_id, user_id, user_name, device_id):
        if self.logger:
            self.logger.info(
                f"[ChatStore:sqlite] create_session(session_id={session_id}, user_id={user_id}, user_name={user_name})"
            )
        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO sessions (session_id, user_name, user_id, device_id, created_at, start_time, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_name,
                    user_id,
                    device_id,
                    _now_iso(),
                    _now_iso(),
                    _now_iso(),
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
                SET last_active_at = ?
                WHERE session_id = ?
                """,
                (_now_iso(), session_id),
            )
            if self.logger:
                self.logger.info(f"[ChatStore:sqlite] insert_turn rowcount={cur.rowcount}")

    def end_session(self, session_id):
        if self.logger:
            self.logger.info(f"[ChatStore:sqlite] end_session(session_id={session_id})")
        with get_db() as db:
            cur = db.execute(
                """
                UPDATE sessions
                SET end_time = ?,
                    analysis_status = 'pending'
                WHERE session_id = ?
                """,
                (_now_iso(), session_id),
            )

            if self.logger:
                self.logger.info(f"[ChatStore:sqlite] end_session rowcount={cur.rowcount}")

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


class SupabaseChatStore:
    def __init__(self, logger=None):
        self.logger = logger
        self.base_url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
        self.service_role_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self.timeout_seconds = int(os.environ.get("SUPABASE_TIMEOUT_SECONDS", "10"))
        self.users_table = os.environ.get("SUPABASE_USERS_TABLE", "users")
        self.sessions_table = os.environ.get("SUPABASE_SESSIONS_TABLE", "sessions")
        self.turns_table = os.environ.get("SUPABASE_TURNS_TABLE", "turns")

        self.headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

        if self.logger:
            self.logger.info(
                f"[ChatStore:init] backend=supabase base_url={self.base_url}, users_table={self.users_table}, sessions_table={self.sessions_table}, turns_table={self.turns_table}"
            )

    def is_configured(self) -> bool:
        return bool(self.base_url and self.service_role_key)

    def _insert(self, table: str, payload: dict):
        url = f"{self.base_url}/rest/v1/{table}"
        response = requests.post(
            url,
            headers={**self.headers, "Prefer": "return=minimal"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def _upsert(self, table: str, payload: dict, on_conflict: str):
        url = f"{self.base_url}/rest/v1/{table}?on_conflict={quote(on_conflict, safe='')}"
        response = requests.post(
            url,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def _update_eq(self, table: str, field: str, value: str, payload: dict):
        url = f"{self.base_url}/rest/v1/{table}?{field}=eq.{quote(str(value), safe='')}"
        response = requests.patch(
            url,
            headers={**self.headers, "Prefer": "return=minimal"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def get_or_create_user(self, user_id: str, name: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] get_or_create_user(user_id={user_id}, name={name})"
            )
        self._upsert(
            self.users_table,
            {"user_id": user_id, "name": name},
            on_conflict="user_id",
        )

    def create_session(self, *, session_id, user_id, user_name, device_id):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] create_session(session_id={session_id}, user_id={user_id}, user_name={user_name})"
            )
        self._upsert(
            self.sessions_table,
            {
                "session_id": session_id,
                "user_name": user_name,
                "user_id": user_id,
                "device_id": device_id,
                "created_at": _now_iso(),
                "start_time": _now_iso(),
                "last_active_at": _now_iso(),
            },
            on_conflict="session_id",
        )

    def insert_turn(self, session_id, turn_index, speaker, text):
        if self.logger:
            self.logger.info(
                f"[ChatStore:supabase] insert_turn(session_id={session_id}, turn_index={turn_index}, speaker={speaker}, text_len={len(text)})"
            )
        self._insert(
            self.turns_table,
            {
                "session_id": session_id,
                "turn_index": turn_index,
                "speaker": speaker,
                "text": text,
                "created_at": _now_iso(),
                "timestamp": _now_iso(),
            },
        )
        self._update_eq(
            self.sessions_table,
            "session_id",
            session_id,
            {"last_active_at": _now_iso()},
        )

    def end_session(self, session_id):
        if self.logger:
            self.logger.info(f"[ChatStore:supabase] end_session(session_id={session_id})")
        self._update_eq(
            self.sessions_table,
            "session_id",
            session_id,
            {
                "end_time": _now_iso(),
                "analysis_status": "pending",
            },
        )

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
        backend = (os.environ.get("CHAT_STORE_BACKEND", "sqlite") or "sqlite").strip().lower()

        self.store = None
        if backend == "supabase":
            supabase_store = SupabaseChatStore(logger=logger)
            if supabase_store.is_configured():
                self.store = supabase_store
            else:
                if self.logger:
                    self.logger.warning(
                        "[ChatStore] CHAT_STORE_BACKEND=supabase but SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY missing, falling back to sqlite"
                    )

        if self.store is None:
            self.store = SQLiteChatStore(logger=logger)

    def get_or_create_user(self, user_id: str, name: str):
        try:
            self.store.get_or_create_user(user_id=user_id, name=name)
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

    def end_session(self, session_id):
        try:
            self.store.end_session(session_id=session_id)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] end_session failed: {e}")

    def update_session_conversation_id(self, session_id: str, conversation_id: str):
        try:
            self.store.update_session_conversation_id(
                session_id=session_id,
                conversation_id=conversation_id,
            )
        except Exception as e:
            if self.logger:
                self.logger.error(f"[ChatStore] update_session_conversation_id failed: {e}")

