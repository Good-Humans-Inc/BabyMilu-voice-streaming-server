from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests


class JournalLabSourceSupabase:
    """Read-only Supabase client for journal lab source data.

    Configure this with lab-specific env vars, not the runtime SUPABASE_* vars:

    - JOURNAL_LAB_SOURCE_SUPABASE_URL
    - JOURNAL_LAB_SOURCE_SUPABASE_SERVICE_ROLE_KEY

    The class intentionally exposes only GET helpers. Do not add write methods
    unless a future task explicitly designs a dev-only write path.
    """

    def __init__(self) -> None:
        self.base_url = _env("JOURNAL_LAB_SOURCE_SUPABASE_URL").rstrip("/")
        self.service_role_key = _env("JOURNAL_LAB_SOURCE_SUPABASE_SERVICE_ROLE_KEY")
        self.timeout_seconds = int(_env("JOURNAL_LAB_SOURCE_SUPABASE_TIMEOUT_SECONDS", "20"))
        self.sessions_table = _env("JOURNAL_LAB_SOURCE_SESSIONS_TABLE", "sessions")
        self.turns_table = _env("JOURNAL_LAB_SOURCE_TURNS_TABLE", "turns")
        self.read_model_table = _env("JOURNAL_LAB_SOURCE_MEMORY_READ_MODEL_TABLE", "user_memory_model")
        self.memory_events_table = _env("JOURNAL_LAB_SOURCE_MEMORY_EVENTS_TABLE", "character_memory_events")
        self.headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self.base_url and self.service_role_key)

    def require_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError(
                "Missing JOURNAL_LAB_SOURCE_SUPABASE_URL or "
                "JOURNAL_LAB_SOURCE_SUPABASE_SERVICE_ROLE_KEY"
            )

    @property
    def host(self) -> str:
        return self.base_url.split("//")[-1].split("/")[0] if self.base_url else ""

    def get_sessions_for_user(self, user_id: str, *, limit: int = 20000) -> List[Dict[str, Any]]:
        return self._request(
            self.sessions_table,
            (
                f"?user_id=eq.{_q(user_id)}"
                f"&select=*"
                f"&order=start_time.asc.nullsfirst,created_at.asc"
                f"&limit={int(limit)}"
            ),
        )

    def get_turn_inventory_for_sessions(self, session_ids: Iterable[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for batch in _chunks([sid for sid in session_ids if sid], 75):
            encoded = ",".join(_q(session_id) for session_id in batch)
            rows.extend(
                self._request(
                    self.turns_table,
                    f"?session_id=in.({encoded})&select=session_id,speaker,created_at,timestamp&limit=20000",
                )
            )
        return rows

    def get_turns_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        return self._request_with_first_working_query(
            self.turns_table,
            [
                f"?session_id=eq.{_q(session_id)}&select=*&order=turn_index.asc,created_at.asc",
                f"?session_id=eq.{_q(session_id)}&select=*&order=created_at.asc",
                f"?session_id=eq.{_q(session_id)}&select=*&order=timestamp.asc",
            ],
        )

    def get_memory_events_for_user(self, user_id: str, *, limit: int = 20000) -> List[Dict[str, Any]]:
        return self._request_with_first_working_query(
            self.memory_events_table,
            [
                f"?userId=eq.{_q(user_id)}&select=*&order=created_at.asc&limit={int(limit)}",
                f"?user_id=eq.{_q(user_id)}&select=*&order=created_at.asc&limit={int(limit)}",
            ],
        )

    def get_read_model_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        rows = self._request_with_first_working_query(
            self.read_model_table,
            [
                f"?user_id=eq.{_q(user_id)}&select=*&limit=1",
                f"?userId=eq.{_q(user_id)}&select=*&limit=1",
            ],
        )
        return rows[0] if rows else None

    def table_names(self) -> Dict[str, str]:
        return {
            "sessions": self.sessions_table,
            "turns": self.turns_table,
            "memoryReadModel": self.read_model_table,
            "memoryEvents": self.memory_events_table,
        }

    def _request_with_first_working_query(self, table: str, queries: List[str]) -> List[Dict[str, Any]]:
        last_error: Optional[Exception] = None
        for query in queries:
            try:
                return self._request(table, query)
            except RuntimeError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []

    def _request(self, table: str, query: str) -> List[Dict[str, Any]]:
        self.require_configured()
        url = f"{self.base_url}/rest/v1/{table}{query}"
        response = requests.get(url, headers=self.headers, timeout=self.timeout_seconds)
        if not response.ok:
            raise RuntimeError(
                f"Supabase read failed table={table} status={response.status_code} "
                f"body={response.text[:500]}"
            )
        rows = response.json() if response.text else []
        return rows if isinstance(rows, list) else []


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _q(value: str) -> str:
    return quote(str(value), safe="")


def _chunks(values: List[str], size: int):
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]
