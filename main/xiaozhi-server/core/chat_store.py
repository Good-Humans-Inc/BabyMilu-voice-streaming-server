# core/connectionstore.py

import sqlite3
from contextlib import contextmanager
from datetime import datetime

import os

DB_PATH = os.environ.get(
    "CHAT_DB_PATH",
    "/opt/xiaozhi-esp32-server/data/conversations.db"
)


print("DEBUG DB_PATH =", DB_PATH)
print("DEBUG exists =", os.path.exists(DB_PATH))
print("DEBUG dir writable =", os.access(os.path.dirname(DB_PATH), os.W_OK))



@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

class ChatStore:
    def __init__(self, logger=None):
        self.logger = logger
        if self.logger:
            self.logger.info(
                f"[ChatStore:init] DB_PATH={DB_PATH}, exists={os.path.exists(DB_PATH)}"
            )

    def get_or_create_user(self, user_id: str, name: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore] get_or_create_user(user_id={user_id}, name={name})"
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
                f"[ChatStore] create_session(session_id={session_id}, user_id={user_id}, user_name={user_name})"
            )
        with get_db() as db:
            db.execute(
                """
                INSERT INTO sessions (session_id, user_name, user_id, device_id, start_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_name, user_id, device_id, datetime.utcnow()),
            )

            if self.logger:
                self.logger.info(
                    f"[ChatStore] create_session rowcount={cur.rowcount}"
                )

    def insert_turn(self, session_id, turn_index, speaker, text):
        if self.logger:
            self.logger.info(
                f"[ChatStore] insert_turn(session_id={session_id}, turn_index={turn_index}, speaker={speaker}, text_len={len(text)})"
            )
        with get_db() as db:
            cur = db.execute(
                """
                INSERT INTO turns (session_id, turn_index, speaker, text, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_index,
                    speaker,
                    text,
                    datetime.utcnow(),
                ),
            )
            if self.logger:
                self.logger.info(
                    f"[ChatStore] insert_turn rowcount={cur.rowcount}"
                )

    def end_session(self, session_id):
        if self.logger:
            self.logger.info(
                f"[ChatStore] end_session(session_id={session_id})"
            )
        with get_db() as db:
            cur = db.execute(
                """
                UPDATE sessions
                SET end_time = ?,
                    last_active_at = ?,
                    analysis_status = 'pending'
                WHERE session_id = ?
                """,
                (datetime.utcnow(), datetime.utcnow(), session_id),
            )

            if self.logger:
                self.logger.info(
                    f"[ChatStore] end_session rowcount={cur.rowcount}"
                )

        

            if self.logger:
                self.logger.info(
                    f"[ChatStore] end_session rowcount={cur.rowcount}"
                )
    
    def update_session_conversation_id(self, session_id: str, conversation_id: str):
        if self.logger:
            self.logger.info(
                f"[ChatStore] update_session_conversation_id(session_id={session_id}, conversation_id={conversation_id})"
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
                    f"[ChatStore] update_session_conversation_id rowcount={cur.rowcount}"
                )

    def update_token_usage(self, session_id: str, token_usage: int):
        if token_usage is None:
            return
        if self.logger:
            self.logger.info(
                f"[ChatStore] update_token_usage(session_id={session_id}, token_usage={token_usage})"
            )

        with get_db() as db:
            cur = db.execute(
                """
                UPDATE sessions
                SET token_usage = ?
                WHERE session_id = ?
                """,
                (token_usage, session_id),
            )

            if self.logger:
                self.logger.info(
                    f"[ChatStore] update_token_usage rowcount={cur.rowcount}"
                )

