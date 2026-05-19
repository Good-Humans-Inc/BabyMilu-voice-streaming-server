from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from services.logging import setup_logging

TAG = __name__
logger = setup_logging()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalSupabaseClient:
    def __init__(self) -> None:
        self.base_url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
        self.service_role_key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        self.timeout_seconds = int(os.environ.get("SUPABASE_TIMEOUT_SECONDS", "10"))
        self.sessions_table = os.environ.get("SUPABASE_SESSIONS_TABLE", "sessions")
        self.turns_table = os.environ.get("SUPABASE_TURNS_TABLE", "turns")
        self.read_model_table = os.environ.get(
            "SUPABASE_MEMORY_READ_MODEL_TABLE", "memory_read_model"
        )
        self.memory_event_table = os.environ.get(
            "SUPABASE_CHARACTER_MEMORY_EVENT_TABLE",
            os.environ.get("SUPABASE_MEMORY_EVENT_TABLE", "character_memory_event"),
        )
        self.headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self.base_url and self.service_role_key)

    def _request(self, method: str, table: str, query: str = "", **kwargs):
        if not self.is_configured():
            raise RuntimeError("Supabase is not configured")
        url = f"{self.base_url}/rest/v1/{table}{query}"
        response = requests.request(
            method,
            url,
            headers=kwargs.pop("headers", self.headers),
            timeout=self.timeout_seconds,
            **kwargs,
        )
        if not response.ok:
            raise RuntimeError(
                f"Supabase {method} failed table={table} status={response.status_code} body={response.text}"
            )
        if response.text:
            return response.json()
        return None

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._request(
            "GET",
            self.sessions_table,
            f"?session_id=eq.{quote(session_id, safe='')}&select=*",
        )
        return rows[0] if isinstance(rows, list) and rows else None

    def get_turns(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._request(
            "GET",
            self.turns_table,
            f"?session_id=eq.{quote(session_id, safe='')}&select=*&order=turn_index.asc,created_at.asc",
        )
        return rows if isinstance(rows, list) else []

    def get_latest_session_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        rows = self._request(
            "GET",
            self.sessions_table,
            f"?user_id=eq.{quote(user_id, safe='')}&select=*&order=end_time.desc.nullslast,created_at.desc&limit=1",
        )
        return rows[0] if isinstance(rows, list) and rows else None

    def get_sessions_for_context(
        self,
        *,
        user_id: str,
        character_id: str,
        start_at: str,
        end_at: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        rows = self._request(
            "GET",
            self.sessions_table,
            (
                f"?user_id=eq.{quote(user_id, safe='')}"
                f"&character_id=eq.{quote(character_id, safe='')}"
                f"&start_time=gte.{quote(start_at, safe=':-+.TZ')}"
                f"&start_time=lte.{quote(end_at, safe=':-+.TZ')}"
                f"&select=*"
                f"&order=start_time.asc.nullsfirst,created_at.asc"
                f"&limit={int(limit)}"
            ),
        )
        return rows if isinstance(rows, list) else []

    def get_recent_memory_events(self, user_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self._select_memory_events(
            user_id=user_id,
            query=f"&order=created_at.desc&limit={int(limit)}",
        )

    def get_journal_memory_events(
        self,
        user_id: str,
        *,
        character_id: Optional[str] = None,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        return self._select_memory_events(
            user_id=user_id,
            character_id=character_id,
            event_type="journal_written",
            query=f"&order=created_at.desc&limit={int(limit)}",
        )

    def get_memory_events_since(
        self,
        user_id: str,
        *,
        occurred_after: Optional[str],
        event_types: List[str],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for event_type in event_types:
            query = f"&order=created_at.desc&limit={int(limit)}"
            if occurred_after:
                query += f"&created_at=gte.{quote(occurred_after, safe=':-+.TZ')}"
            rows.extend(
                self._select_memory_events(
                    user_id=user_id,
                    event_type=event_type,
                    query=query,
                )
            )
        return rows

    def _select_memory_events(
        self,
        *,
        user_id: str,
        character_id: Optional[str] = None,
        event_type: Optional[str] = None,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        filters = f"?user_id=eq.{quote(user_id, safe='')}"
        if character_id:
            filters += f"&character_id=eq.{quote(character_id, safe='')}"
        if event_type:
            filters += f"&event_type=eq.{quote(event_type, safe='')}"
        filters += f"&select=*{query}"
        try:
            rows = self._request("GET", self.memory_event_table, filters)
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Failed reading journal memory events from {self.memory_event_table}: {exc}"
            )
            return []

    def get_system_memory_block(self, user_id: str) -> str:
        rows = self._request(
            "GET",
            self.read_model_table,
            f"?user_id=eq.{quote(user_id, safe='')}&select=*",
        )
        row = rows[0] if isinstance(rows, list) and rows else {}
        prompt_pack = row.get("prompt_pack") if isinstance(row.get("prompt_pack"), dict) else {}
        block = prompt_pack.get("systemMemoryBlock")
        return block if isinstance(block, str) else ""

    def write_journal_memory_event(
        self,
        *,
        user_id: str,
        character_id: str,
        session_id: Optional[str],
        content: Dict[str, Any],
        occurred_at: str,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "user_id": user_id,
            "character_id": character_id,
            "event_type": "journal_written",
            "modality": "plushie_conversation",
            "content": content,
            "time": {
                "occurredAt": occurred_at,
                "ingestedAt": _now_iso(),
            },
            "source": {
                "sessionId": session_id,
                "characterId": character_id,
            },
            "created_at": _now_iso(),
        }
        headers = {**self.headers, "Prefer": "return=representation"}
        rows = self._request(
            "POST",
            self.memory_event_table,
            headers=headers,
            json=payload,
        )
        return rows[0] if isinstance(rows, list) and rows else None
