import os
import functools
from typing import Optional, Tuple, Dict, Any

from google.cloud import firestore
from config.settings import get_gcp_credentials_path
from config.logger import setup_logging


TAG = __name__
logger = setup_logging()


@functools.lru_cache(maxsize=1)
def _build_client() -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)
    return firestore.Client()


def get_active_character_for_device(device_id: str, timeout: float = 3.0) -> Optional[str]:
    try:
        client = _build_client()
        doc = client.collection("devices").document(device_id).get(timeout=timeout)
        if not doc.exists:
            logger.bind(tag=TAG).warning(f"Firestore devices/{device_id} not found")
            return None
        data = doc.to_dict() or {}
        return data.get("activeCharacterId")
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore get device error: {e}")
        return None


def get_device_doc(device_id: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        client = _build_client()
        doc = client.collection("devices").document(device_id).get(timeout=timeout)
        if not doc.exists:
            logger.bind(tag=TAG).warning(f"Firestore devices/{device_id} not found")
            return None
        return doc.to_dict() or {}
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore get device doc error: {e}")
        return None


def get_owner_phone_for_device(device_id: str, timeout: float = 3.0) -> Optional[str]:
    data = get_device_doc(device_id, timeout=timeout)
    if not data:
        return None
    return data.get("ownerPhone")


def get_character_profile(character_id: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        client = _build_client()
        doc = client.collection("characters").document(character_id).get(timeout=timeout)
        if not doc.exists:
            logger.bind(tag=TAG).warning(f"Firestore characters/{character_id} not found")
            return None
        return doc.to_dict() or {}
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore get character error: {e}")
        return None


def extract_character_profile_fields(character_doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract name, age, pronouns, relationship, bio, callMe, voice.

    Supports both top-level fields and nested under "profile". For voice, also
    falls back to "voiceId" if present.
    """
    character_profile_fields = ("name", "age", "pronouns", "relationship", "bio", "callMe", "voice")
    result: Dict[str, Optional[str]] = {k: None for k in character_profile_fields}
    if not character_doc:
        return result

    profile = character_doc.get("profile", {}) or {}
    for key in character_profile_fields:
        # Prefer top-level; fallback to profile
        value = character_doc.get(key)
        if value is None:
            value = profile.get(key)
        result[key] = value

    # voice fallback for voiceId variants
    if result.get("voice") is None:
        result["voice"] = profile.get("voiceId") or character_doc.get("voiceId")

    return result


def extract_voice_and_bio(character_doc: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    fields = extract_character_profile_fields(character_doc)
    return fields.get("voice"), fields.get("bio")


def get_user_profile_by_phone(owner_phone: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """Fetch users/{owner_phone} (doc id is phone)."""
    try:
        client = _build_client()
        doc = client.collection("users").document(owner_phone).get(timeout=timeout)
        if not doc.exists:
            logger.bind(tag=TAG).warning(f"Firestore users/{owner_phone} not found")
            return None
        return doc.to_dict() or {}
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore get user error: {e}")
        return None


def extract_user_profile_fields(user_doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    wanted = ("name", "birthday", "pronouns", "phoneNumber")
    result: Dict[str, Optional[str]] = {k: None for k in wanted}
    if not user_doc:
        return result
    for k in wanted:
        v = user_doc.get(k)
        # Convert timestamp-like values to string if needed
        result[k] = str(v) if v is not None else None
    return result


