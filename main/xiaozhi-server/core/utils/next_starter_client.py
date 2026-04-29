import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from config.logger import setup_logging


TAG = __name__
logger = setup_logging()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _parse_iso8601(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fetch_next_starter_row(character_id: str):
    supabase_url = _env("SUPABASE_URL").rstrip("/")
    service_role_key = _env("SUPABASE_SERVICE_ROLE_KEY")
    request_timeout = float(_env("NEXT_STARTER_DB_TIMEOUT_SECONDS", "2.0") or "2.0")

    if not character_id or not supabase_url or not service_role_key:
        return None, supabase_url, service_role_key, request_timeout

    url = (
        f"{supabase_url}/rest/v1/character_memory_model"
        f"?character_id=eq.{quote(character_id, safe='')}"
        "&select=next_starter"
        "&limit=1"
    )

    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
            "Accept": "application/json",
        },
        timeout=request_timeout,
    )
    response.raise_for_status()

    rows = response.json()
    return (rows[0] if rows else None), supabase_url, service_role_key, request_timeout


def get_ready_next_starter(character_id: str) -> Optional[Dict[str, Any]]:
    max_age_days = int(_env("NEXT_STARTER_MAX_AGE_DAYS", "7") or "7")
    row, _, _, _ = _fetch_next_starter_row(character_id)
    if not row:
        return None

    payload = row.get("next_starter")
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "ready":
        return None
    if payload.get("characterId") and str(payload.get("characterId")) != str(character_id):
        return None
    if not payload.get("audioUrl"):
        return None

    generated_at = _parse_iso8601(payload.get("generatedAt"))
    if generated_at is None:
        return None
    if generated_at < datetime.now(timezone.utc) - timedelta(days=max_age_days):
        logger.bind(tag=TAG).info(
            f"Skipping stale next_starter for character_id={character_id}, generated_at={generated_at.isoformat()}"
        )
        return None

    return payload


def mark_next_starter_consumed(character_id: str, payload: Dict[str, Any]) -> bool:
    if not character_id or not isinstance(payload, dict):
        return False

    row, supabase_url, service_role_key, request_timeout = _fetch_next_starter_row(character_id)
    if not row or not supabase_url or not service_role_key:
        return False

    current_payload = row.get("next_starter")
    if not isinstance(current_payload, dict):
        return False

    # Only consume the payload we just played; avoid overwriting a newer starter.
    if current_payload.get("generatedAt") != payload.get("generatedAt"):
        return False
    if current_payload.get("sourceSessionId") != payload.get("sourceSessionId"):
        return False

    consumed_payload = dict(current_payload)
    consumed_payload["status"] = "consumed"
    consumed_payload["consumedAt"] = datetime.now(timezone.utc).isoformat()

    url = (
        f"{supabase_url}/rest/v1/character_memory_model"
        f"?character_id=eq.{quote(character_id, safe='')}"
    )
    response = requests.patch(
        url,
        headers={
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"next_starter": consumed_payload},
        timeout=request_timeout,
    )
    response.raise_for_status()
    return True


def fetch_next_starter_audio(audio_url: str) -> bytes:
    if not audio_url:
        raise ValueError("audio_url is missing")

    request_timeout = float(_env("NEXT_STARTER_FETCH_TIMEOUT_SECONDS", "2.0") or "2.0")
    response = requests.get(audio_url, timeout=request_timeout)
    response.raise_for_status()
    return response.content
