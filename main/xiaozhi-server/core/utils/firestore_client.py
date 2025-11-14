import os
import json
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
        # Debug visibility into what we actually fetched from Firestore
        try:
            pretty_doc = json.dumps(data, ensure_ascii=False, default=str, indent=2)
            logger.bind(tag=TAG).info(
                f"Firestore devices/{device_id} read: project={getattr(client, 'project', None)}, "
                f"exists={doc.exists}, full_doc=\n{pretty_doc}"
            )
        except Exception:
            # Logging must not break the read path
            pass
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



def get_conversation_id_for_device(device_id: str, timeout: float = 3.0) -> Optional[str]:
    """Return devices/{device_id}.conversationId if present."""
    try:
        client = _build_client()
        doc = client.collection("devices").document(device_id).get(timeout=timeout)
        if not doc.exists:
            logger.bind(tag=TAG).warning(f"Firestore devices/{device_id} not found")
            return None
        data = doc.to_dict() or {}
        return data.get("conversationId")
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore get conversationId error: {e}")
        return None


def set_conversation_id_for_device(device_id: str, conversation_id: str, timeout: float = 3.0) -> bool:
    """Upsert devices/{deviceId}.conversationId = conversation_id."""
    try:
        client = _build_client()
        client.collection("devices").document(device_id).set(
            {"conversationId": conversation_id}, merge=True, timeout=timeout
        )
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"Firestore set conversationId error: {e}")
        return False


def get_most_recent_character_via_user_for_device(device_id: str, timeout: float = 3.0) -> Optional[str]:
    """Return the most recently created characterId for the owner of devices/{device_id}.

    Assumes users/{ownerPhone} contains an array field "characterIds" where the last
    element is the most recent character.
    """
    try:
        device_doc = get_device_doc(device_id, timeout=timeout)
        if not device_doc:
            logger.bind(tag=TAG).warning(f"⚠️ Device doc missing for fallback: devices/{device_id}")
            return None
        owner_phone = device_doc.get("ownerPhone")
        if not owner_phone:
            logger.bind(tag=TAG).warning(
                f"Device devices/{device_id} has no ownerPhone; cannot derive recent character"
            )
            return None
        user_doc = get_user_profile_by_phone(owner_phone, timeout=timeout)
        if not user_doc:
            logger.bind(tag=TAG).warning(f"⚠️ users/{owner_phone} not found for fallback")
            return None
        char_ids = user_doc.get("characterIds") or []
        if isinstance(char_ids, list) and len(char_ids) > 0:
            most_recent = str(char_ids[-1])
            logger.bind(tag=TAG).info(
                f"Fallback chose most recent character for device {device_id} -> users/{owner_phone}: {most_recent}"
            )
            return most_recent
        logger.bind(tag=TAG).warning(
            f"⚠️ users/{owner_phone} has no characterIds; cannot derive recent character"
        )
        return None
    except Exception as e:
        logger.bind(tag=TAG).error(f"Fallback most recent character error: {e}")
        return None
