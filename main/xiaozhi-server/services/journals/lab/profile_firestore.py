from __future__ import annotations

import os
from typing import Any, Optional

from google.cloud import firestore


def load_lab_profile(
    *,
    user_id: str,
    character_id: str,
    database_id: Optional[str],
) -> dict[str, Any]:
    """Read user/character profile for journal lab prompts.

    This is read-only. Pass an empty database id to inspect default Firestore,
    even when the runtime server is configured with FIRESTORE_DATABASE_ID for
    development writes.
    """

    project_id = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    kwargs: dict[str, Any] = {}
    if project_id:
        kwargs["project"] = project_id
    normalized_database = _normalize_database_id(database_id)
    if normalized_database:
        kwargs["database"] = normalized_database

    db = firestore.Client(**kwargs)
    user_ref = db.collection("users").document(user_id)
    user_snap = user_ref.get()
    user_data = user_snap.to_dict() or {} if user_snap.exists else {}

    top_level = db.collection("characters").document(character_id).get()
    character_data = top_level.to_dict() or {} if top_level.exists else {}

    return {
        "database": normalized_database or "(default)",
        "userExists": bool(user_snap.exists),
        "characterExists": bool(top_level.exists),
        "userData": user_data,
        "characterData": character_data,
    }


def _normalize_database_id(value: Optional[str]) -> Optional[str]:
    database_id = (value or "").strip()
    if not database_id or database_id == "(default)" or database_id == "default":
        return None
    return database_id
