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


def _next_starter_table_name() -> str:
    return _env("SUPABASE_CHARACTER_MEMORY_TABLE", "character_memory_model")


def _request_headers(service_role_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {service_role_key}",
        "apikey": service_role_key,
        "Accept": "application/json",
    }


def build_character_memory_payload(
    character_id: str,
    *,
    owner_user_id: str = "",
    last_device_id: str = "",
) -> Dict[str, Any]:
    return {
        "character_id": str(character_id),
        "owner_user_id": str(owner_user_id or "").strip() or None,
        "last_device_id": str(last_device_id or "").strip() or None,
        "summary": "",
        "memory_state": {},
        "next_starter": None,
        "starter_fallback": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_character_memory_row(character_id: str, *, select_fields: str):
    supabase_url = _env("SUPABASE_URL").rstrip("/")
    service_role_key = _env("SUPABASE_SERVICE_ROLE_KEY")
    request_timeout = float(_env("NEXT_STARTER_DB_TIMEOUT_SECONDS", "2.0") or "2.0")
    table_name = _next_starter_table_name()

    if not character_id or not supabase_url or not service_role_key:
        return None, supabase_url, service_role_key, request_timeout

    url = (
        f"{supabase_url}/rest/v1/{table_name}"
        f"?character_id=eq.{quote(character_id, safe='')}"
        f"&select={quote(select_fields, safe=',')}"
        "&limit=1"
    )

    response = requests.get(
        url,
        headers=_request_headers(service_role_key),
        timeout=request_timeout,
    )
    response.raise_for_status()

    rows = response.json()
    return (rows[0] if rows else None), supabase_url, service_role_key, request_timeout


def ensure_character_memory_record(
    character_id: str,
    *,
    owner_user_id: str = "",
    last_device_id: str = "",
) -> bool:
    supabase_url = _env("SUPABASE_URL").rstrip("/")
    service_role_key = _env("SUPABASE_SERVICE_ROLE_KEY")
    request_timeout = float(_env("NEXT_STARTER_DB_TIMEOUT_SECONDS", "2.0") or "2.0")
    table_name = _next_starter_table_name()

    if not character_id or not supabase_url or not service_role_key:
        return False

    row, _, _, _ = _fetch_character_memory_row(
        character_id,
        select_fields="character_id",
    )
    if row:
        url = (
            f"{supabase_url}/rest/v1/{table_name}"
            f"?character_id=eq.{quote(character_id, safe='')}"
        )
        response = requests.patch(
            url,
            headers={
                **_request_headers(service_role_key),
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "owner_user_id": str(owner_user_id or "").strip() or None,
                "last_device_id": str(last_device_id or "").strip() or None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            timeout=request_timeout,
        )
        response.raise_for_status()
        return True

    payload = build_character_memory_payload(
        character_id,
        owner_user_id=owner_user_id,
        last_device_id=last_device_id,
    )
    url = (
        f"{supabase_url}/rest/v1/{table_name}"
        f"?on_conflict={quote('character_id', safe='')}"
    )
    response = requests.post(
        url,
        headers={
            **_request_headers(service_role_key),
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload,
        timeout=request_timeout,
    )
    response.raise_for_status()
    return True


def _fetch_next_starter_row(character_id: str):
    return _fetch_character_memory_row(
        character_id,
        select_fields="next_starter,starter_fallback",
    )


def _is_valid_starter_payload(
    payload: Any,
    *,
    character_id: str,
    max_age_days: int,
    payload_label: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "ready":
        return False
    if payload.get("characterId") and str(payload.get("characterId")) != str(character_id):
        return False
    if not payload.get("audioUrl") and not payload.get("text"):
        return False

    generated_at = _parse_iso8601(payload.get("generatedAt"))
    if generated_at is None:
        return False
    if generated_at < datetime.now(timezone.utc) - timedelta(days=max_age_days):
        logger.bind(tag=TAG).info(
            f"Skipping stale {payload_label} for character_id={character_id}, generated_at={generated_at.isoformat()}"
        )
        return False
    return True


def get_ready_next_starter(character_id: str) -> Optional[Dict[str, Any]]:
    max_age_days = int(_env("NEXT_STARTER_MAX_AGE_DAYS", "7") or "7")
    fallback_max_age_days = int(_env("STARTER_FALLBACK_MAX_AGE_DAYS", "3650") or "3650")
    row, _, _, _ = _fetch_next_starter_row(character_id)
    if not row:
        return None

    payload = row.get("next_starter")
    if _is_valid_starter_payload(
        payload,
        character_id=character_id,
        max_age_days=max_age_days,
        payload_label="next_starter",
    ):
        return payload

    fallback_payload = row.get("starter_fallback")
    if _is_valid_starter_payload(
        fallback_payload,
        character_id=character_id,
        max_age_days=fallback_max_age_days,
        payload_label="starter_fallback",
    ):
        return fallback_payload

    return None


def mark_next_starter_consumed(character_id: str, payload: Dict[str, Any]) -> bool:
    if not character_id or not isinstance(payload, dict):
        return False
    if payload.get("sourceType") == "fallback_hi":
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
    table_name = _next_starter_table_name()

    url = (
        f"{supabase_url}/rest/v1/{table_name}"
        f"?character_id=eq.{quote(character_id, safe='')}"
    )
    response = requests.patch(
        url,
        headers={
            **_request_headers(service_role_key),
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={
            "next_starter": consumed_payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
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
