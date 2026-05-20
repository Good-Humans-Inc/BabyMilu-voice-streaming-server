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


def journal_character_state_ref(client: firestore.Client, user_id: str, character_id: str):
    return user_ref(client, user_id).collection("journal_character_state").document(_safe_doc_id(character_id))


def _safe_doc_id(value: str) -> str:
    return str(value or "").replace("/", "_")


def _queue_doc_id(character_id: str, local_date: str) -> str:
    return f"{_safe_doc_id(character_id)}_{_safe_doc_id(local_date)}"


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
        user_ref(db, user_id)
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
    snap = journal_character_state_ref(client, user_id, character_id).get()
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
    ref = journal_character_state_ref(client, user_id, character_id)
    ref.set(
        {
            "characterId": character_id,
            "turns_since_last_journal": firestore.Increment(int(user_turn_count or 0)),
            "updated_at": iso_now(),
        },
        merge=True,
    )
    return get_turn_counter(client, user_id, character_id)


def reset_turn_counter(client: firestore.Client, user_id: str, character_id: str) -> None:
    journal_character_state_ref(client, user_id, character_id).set(
        {"characterId": character_id, "turns_since_last_journal": 0, "updated_at": iso_now()},
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
        user_ref(client, user_id)
        .collection("moments")
        .limit(max(limit * 3, limit))
    )
    rows: List[Dict[str, Any]] = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        if data.get("type") != "journal" or data.get("characterId") != character_id:
            continue
        if not include_deleted and data.get("status") == "deleted":
            continue
        data["_id"] = snap.id
        data["_ref"] = snap.reference
        rows.append(data)
    rows.sort(key=lambda item: str(item.get("displayAt") or ""), reverse=True)
    return rows


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
        user_ref(client, user_id)
        .collection("journal_queue")
        .document(_queue_doc_id(character_id, local_date))
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
            "characterId": character_id,
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
    display_at: str,
    status: str = "ready",
    entry_id: Optional[str] = None,
    memory_event_id: Any = None,
) -> str:
    entry_id = entry_id or str(uuid.uuid4())
    ref = user_ref(client, user_id).collection("moments").document(entry_id)
    app_journal_type = "lure-back" if journal_type == "lure_back" else journal_type
    payload = {
        "id": entry_id,
        "type": "journal",
        "characterId": character_id,
        "displayAt": display_at,
        "text": text,
        "status": status,
        "journalType": app_journal_type,
        "memoryEventId": str(memory_event_id) if memory_event_id is not None else None,
        "deletedAt": None,
    }
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
        db.collection_group("moments")
        .where(filter=FieldFilter("status", "==", "ready"))
        .limit(limit)
    )
    return [
        snap
        for snap in query.stream()
        if (snap.to_dict() or {}).get("type") == "journal"
    ]


def publish_entry(entry_ref: Any, *, published_at: Optional[str] = None) -> None:
    entry_ref.set(
        {
            "status": "published",
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
    ref = user_ref(db, user_id).collection("moments").document(entry_id)
    snap = ref.get()
    if not snap.exists:
        return False
    now = iso_now()
    ref.set(
        {
            "status": "deleted",
            "deletedAt": now,
        },
        merge=True,
    )
    return True
