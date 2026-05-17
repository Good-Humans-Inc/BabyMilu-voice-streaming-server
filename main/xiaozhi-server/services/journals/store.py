from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from core.utils.firestore_factory import build_firestore_client
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def local_date_for_timezone(now: datetime, timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz).date().isoformat()


def _client(client: Optional[firestore.Client] = None) -> firestore.Client:
    return client or build_firestore_client()


def user_ref(client: firestore.Client, user_id: str):
    return client.collection("users").document(user_id)


def character_ref(client: firestore.Client, user_id: str, character_id: str):
    return user_ref(client, user_id).collection("characters").document(character_id)


def get_user_data(client: firestore.Client, user_id: str) -> Dict[str, Any]:
    snap = user_ref(client, user_id).get()
    return snap.to_dict() or {} if snap.exists else {}


def get_user_timezone(client: firestore.Client, user_id: str) -> str:
    user_data = get_user_data(client, user_id)
    for key in ("timezone", "timeZone", "timezoneId", "userTimezone"):
        value = user_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "UTC"


def get_character_data(client: firestore.Client, user_id: str, character_id: str) -> Dict[str, Any]:
    snap = character_ref(client, user_id, character_id).get()
    if snap.exists:
        return snap.to_dict() or {}
    top_level = client.collection("characters").document(character_id).get()
    return top_level.to_dict() or {} if top_level.exists else {}


def write_session_marker(
    *,
    user_id: str,
    device_id: str,
    character_id: str,
    session_id: str,
    user_turn_count: int,
    client: Optional[firestore.Client] = None,
) -> bool:
    if not user_id or not character_id or not session_id:
        return False
    db = _client(client)
    now = iso_now()
    ref = (
        character_ref(db, user_id, character_id)
        .collection("journal_session_state")
        .document(session_id)
    )
    ref.set(
        {
            "userId": user_id,
            "deviceId": device_id,
            "characterId": character_id,
            "sessionId": session_id,
            "userTurnCount": int(user_turn_count or 0),
            "status": "waiting_memory",
            "created_at": now,
            "updated_at": now,
        },
        merge=True,
    )
    return True


def fetch_waiting_session_markers(
    *,
    client: Optional[firestore.Client] = None,
    limit: int = 50,
) -> List[Any]:
    db = _client(client)
    query = (
        db.collection_group("journal_session_state")
        .where(filter=FieldFilter("status", "==", "waiting_memory"))
        .limit(limit)
    )
    return list(query.stream())


def update_marker(doc_ref: Any, updates: Dict[str, Any]) -> None:
    doc_ref.set({**updates, "updated_at": iso_now()}, merge=True)


def get_turn_counter(client: firestore.Client, user_id: str, character_id: str) -> int:
    snap = character_ref(client, user_id, character_id).get()
    data = snap.to_dict() or {} if snap.exists else {}
    try:
        return int(data.get("turns_since_last_journal") or 0)
    except (TypeError, ValueError):
        return 0


def add_turns_to_counter(
    client: firestore.Client,
    user_id: str,
    character_id: str,
    user_turn_count: int,
) -> int:
    ref = character_ref(client, user_id, character_id)
    ref.set(
        {
            "turns_since_last_journal": firestore.Increment(int(user_turn_count or 0)),
            "updated_at": iso_now(),
        },
        merge=True,
    )
    return get_turn_counter(client, user_id, character_id)


def reset_turn_counter(client: firestore.Client, user_id: str, character_id: str) -> None:
    character_ref(client, user_id, character_id).set(
        {"turns_since_last_journal": 0, "updated_at": iso_now()},
        merge=True,
    )


def list_journal_entries(
    client: firestore.Client,
    user_id: str,
    character_id: str,
    *,
    include_deleted: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    query = (
        character_ref(client, user_id, character_id)
        .collection("journal_entries")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    rows: List[Dict[str, Any]] = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        if not include_deleted and data.get("is_deleted") is True:
            continue
        data["_id"] = snap.id
        data["_ref"] = snap.reference
        rows.append(data)
    return rows


def has_prior_visible_journal(client: firestore.Client, user_id: str, character_id: str) -> bool:
    return bool(list_journal_entries(client, user_id, character_id, limit=1))


def queue_session(
    *,
    client: firestore.Client,
    user_id: str,
    character_id: str,
    local_date: str,
    session: Dict[str, Any],
    journal_type: str,
) -> str:
    ref = (
        character_ref(client, user_id, character_id)
        .collection("journal_queue")
        .document(local_date)
    )
    snap = ref.get()
    now = iso_now()
    data = snap.to_dict() or {}
    sessions = data.get("sessions") if isinstance(data.get("sessions"), list) else []
    existing_ids = {str(item.get("sessionId")) for item in sessions if isinstance(item, dict)}
    if str(session.get("sessionId")) not in existing_ids:
        sessions.append(session)
    ref.set(
        {
            "date": local_date,
            "status": "pending",
            "journal_type": data.get("journal_type") or journal_type,
            "sessions": sessions,
            "createdAt": data.get("createdAt") or now,
            "updated_at": now,
        },
        merge=True,
    )
    reset_turn_counter(client, user_id, character_id)
    return ref.path


def fetch_pending_queues(
    *,
    client: Optional[firestore.Client] = None,
    limit: int = 50,
) -> List[Any]:
    db = _client(client)
    query = (
        db.collection_group("journal_queue")
        .where(filter=FieldFilter("status", "==", "pending"))
        .limit(limit)
    )
    return list(query.stream())


def create_journal_entry(
    *,
    client: firestore.Client,
    user_id: str,
    character_id: str,
    text: str,
    journal_type: str,
    display_date: str,
    thread_reference: bool,
    source_session_ids: List[str],
    source_memory_event_ids: List[str],
    status: str = "ready",
    entry_id: Optional[str] = None,
    created_at: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    entry_id = entry_id or str(uuid.uuid4())
    created_at = created_at or iso_now()
    ref = (
        character_ref(client, user_id, character_id)
        .collection("journal_entries")
        .document(entry_id)
    )
    payload = {
            "text": text,
            "journal_type": journal_type,
            "status": status,
            "created_at": created_at,
            "published_at": None,
            "display_date": display_date,
            "thread_reference": bool(thread_reference),
            "is_deleted": False,
            "deleted_at": None,
            "source_session_ids": source_session_ids,
            "source_memory_event_ids": source_memory_event_ids,
            "updated_at": created_at,
        }
    if metadata:
        payload.update(metadata)
    ref.set(
        payload,
        merge=True,
    )
    return entry_id


def fetch_ready_entries(
    *,
    client: Optional[firestore.Client] = None,
    limit: int = 50,
) -> List[Any]:
    db = _client(client)
    query = (
        db.collection_group("journal_entries")
        .where(filter=FieldFilter("status", "==", "ready"))
        .limit(limit)
    )
    return [
        snap
        for snap in query.stream()
        if (snap.to_dict() or {}).get("is_deleted") is not True
    ]


def publish_entry(entry_ref: Any, *, published_at: Optional[str] = None) -> None:
    published_at = published_at or iso_now()
    entry_ref.set(
        {
            "status": "published",
            "published_at": published_at,
            "updated_at": published_at,
        },
        merge=True,
    )


def soft_delete_journal_entry(
    *,
    user_id: str,
    character_id: str,
    entry_id: str,
    client: Optional[firestore.Client] = None,
) -> bool:
    db = _client(client)
    ref = (
        character_ref(db, user_id, character_id)
        .collection("journal_entries")
        .document(entry_id)
    )
    snap = ref.get()
    if not snap.exists:
        return False
    now = iso_now()
    ref.set(
        {
            "is_deleted": True,
            "deleted_at": now,
            "updated_at": now,
        },
        merge=True,
    )
    return True
