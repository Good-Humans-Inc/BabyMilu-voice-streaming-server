import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = "/srv/dev/data/conversations.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class ChatStore:

    def get_or_create_user(self, user_id: str, name: str):
        with get_db() as db:
            db.execute("""
                INSERT OR IGNORE INTO users (user_id, name)
                VALUES (?, ?)
            """, (user_id, name))

    def create_session(self, session_id, user_name, user_id=None):
        with get_db() as db:
            db.execute("""
                INSERT INTO sessions (session_id, user_name, user_id, start_time)
                VALUES (?, ?, ?, ?)
            """, (session_id, user_name, user_id, datetime.utcnow()))

    def insert_turn(self, session_id, turn_index, speaker, text):
        with get_db() as db:
            db.execute("""
                INSERT INTO turns (session_id, turn_index, speaker, text, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                turn_index,
                speaker,
                text,
                datetime.utcnow()
            ))

    def end_session(self, session_id):
        with get_db() as db:
            db.execute("""
                UPDATE sessions
                SET end_time = ?
                WHERE session_id = ?
            """, (datetime.utcnow(), session_id))
