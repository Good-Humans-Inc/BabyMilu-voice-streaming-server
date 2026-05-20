from __future__ import annotations

import os
import uuid
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
        explicit_memory_event_table = os.environ.get("SUPABASE_CHARACTER_MEMORY_EVENT_TABLE")
        self.memory_event_table = explicit_memory_event_table or "character_memory_events"
        self.legacy_memory_event_table = os.environ.get("SUPABASE_MEMORY_EVENT_TABLE")
        self._memory_event_table_explicit = bool(explicit_memory_event_table)
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
        return _normalize_session(rows[0]) if isinstance(rows, list) and rows else None

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
        return _normalize_session(rows[0]) if isinstance(rows, list) and rows else None

    def get_character_ids_for_user(self, user_id: str, limit: int = 100) -> List[str]:
        rows = self._request(
            "GET",
            self.sessions_table,
            (
                f"?user_id=eq.{quote(user_id, safe='')}"
                f"&select=character_id"
                f"&order=created_at.desc"
                f"&limit={int(limit)}"
            ),
        )
        seen = set()
        values: List[str] = []
        for row in rows if isinstance(rows, list) else []:
            character_id = str(row.get("character_id") or "").strip()
            if character_id and character_id not in seen:
                seen.add(character_id)
                values.append(character_id)
        return values

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
        return [_normalize_session(row) for row in rows] if isinstance(rows, list) else []

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
        for table, style in self._memory_event_table_candidates():
            filters = _memory_event_filters(
                style=style,
                user_id=user_id,
                character_id=character_id,
                event_type=event_type,
            )
            filters += f"&select=*{query}"
            try:
                rows = self._request("GET", table, filters)
                return [_normalize_memory_event(row) for row in rows] if isinstance(rows, list) else []
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed reading journal memory events from {table}: {exc}"
                )
        return []

    def get_system_memory_block(self, user_id: str) -> str:
        try:
            rows = self._request(
                "GET",
                self.read_model_table,
                f"?user_id=eq.{quote(user_id, safe='')}&select=*",
            )
        except Exception:
            rows = self._request(
                "GET",
                "user_memory_model",
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
        headers = {**self.headers, "Prefer": "return=representation"}
        for table, style in self._memory_event_table_candidates():
            payload = _journal_memory_event_payload(
                style=style,
                user_id=user_id,
                character_id=character_id,
                session_id=session_id,
                content=content,
                occurred_at=occurred_at,
            )
            try:
                rows = self._request(
                    "POST",
                    table,
                    headers=headers,
                    json=payload,
                )
                return _normalize_memory_event(rows[0]) if isinstance(rows, list) and rows else None
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed writing journal memory event to {table}: {exc}"
                )
        raise RuntimeError("Failed writing journal memory event")

    def _memory_event_table_candidates(self) -> List[tuple[str, str]]:
        candidates = []
        if self._memory_event_table_explicit:
            candidates.append((self.memory_event_table, _memory_event_style(self.memory_event_table)))
        for fallback in (
            "character_memory_events",
            self.legacy_memory_event_table,
            "character_memory_event",
        ):
            if not fallback:
                continue
            item = (fallback, _memory_event_style(fallback))
            if item not in candidates:
                candidates.append(item)
        return candidates


def _normalize_session(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if not data.get("memory_status"):
        data["memory_status"] = (
            data.get("memory_status_character")
            or data.get("memory_status_user")
            or data.get("analysis_status")
            or ""
        )
    return data


def _memory_event_style(table: str) -> str:
    return "camel" if table.endswith("events") else "snake"


def _memory_event_filters(
    *,
    style: str,
    user_id: str,
    character_id: Optional[str],
    event_type: Optional[str],
) -> str:
    if style == "camel":
        filters = f"?userId=eq.{quote(user_id, safe='')}"
        if character_id:
            filters += f"&characterId=eq.{quote(character_id, safe='')}"
        if event_type:
            filters += f"&eventType=eq.{quote(event_type, safe='')}"
        return filters
    filters = f"?user_id=eq.{quote(user_id, safe='')}"
    if character_id:
        filters += f"&character_id=eq.{quote(character_id, safe='')}"
    if event_type:
        filters += f"&event_type=eq.{quote(event_type, safe='')}"
    return filters


def _normalize_memory_event(row: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    if "eventId" in data and "event_id" not in data:
        data["event_id"] = data.get("eventId")
    if "eventType" in data and "event_type" not in data:
        data["event_type"] = data.get("eventType")
    if "userId" in data and "user_id" not in data:
        data["user_id"] = data.get("userId")
    if "characterId" in data and "character_id" not in data:
        data["character_id"] = data.get("characterId")
    return data


def _journal_memory_event_payload(
    *,
    style: str,
    user_id: str,
    character_id: str,
    session_id: Optional[str],
    content: Dict[str, Any],
    occurred_at: str,
) -> Dict[str, Any]:
    common = {
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
    if style == "camel":
        return {
            **common,
            "eventId": str(uuid.uuid4()),
            "userId": user_id,
            "characterId": character_id,
            "eventType": "journal_written",
        }
    return {
        **common,
        "user_id": user_id,
        "character_id": character_id,
        "event_type": "journal_written",
    }
