"""LLM tool: inspect_recent_photo

Inspect the user's most recent photo within a recent time window and return
grounded visual context for the main character model.
"""
from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests
from google.cloud import firestore
from openai import APIError, OpenAI

from config.logger import setup_logging
from core.utils.firestore_client import (
    get_firestore_client,
    get_owner_phone_for_device,
)
from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
logger = setup_logging()

RECENCY_WINDOW_HOURS = int(os.environ.get("RECENT_PHOTO_LOOKBACK_HOURS", "24"))
PHOTO_QUERY_LIMIT = int(os.environ.get("RECENT_PHOTO_QUERY_LIMIT", "20"))
OPENAI_MODEL = os.environ.get("RECENT_PHOTO_INSPECT_MODEL", "gpt-4o-mini").strip()
PHOTO_SOURCE_COLLECTIONS = ("moments", "photos")
PHOTO_URL_FIELDS = (
    "photoUrl",
    "processedPhotoUrl",
    "cardUrl",
    "imageUrl",
    "image_url",
    "url",
    "downloadUrl",
    "publicUrl",
    "mediaUrl",
    "thumbnailUrl",
)
PHOTO_DATE_FIELDS = ("createdAt", "displayAt", "updatedAt")

INSPECT_RECENT_PHOTO_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "inspect_recent_photo",
        "description": (
            "Use this tool when the user wants you to inspect, react to, "
            "discuss, or interpret a recent photo in the app. The tool finds "
            "the most recent photo in the allowed recency window, analyzes it, "
            "and returns a rich grounded description for you to use in your "
            "in-character response. If no qualifying photo is found, it returns "
            "a no-match result."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

VISION_ANALYSIS_PROMPT = """
You are analyzing a user's recent photo for BabyMilu's voice interaction
system. Your job is to inspect the image and return structured, grounded visual
context for another LLM that will speak to the user in character. Do not address
the user directly. Do not praise, critique, or give advice. Do not invent
details that are not visible. If something is uncertain, ambiguous, partially
obscured, stylized, or hard to identify, say so clearly. Prefer concrete
observations over vague adjectives. If the image contains art, craft, cosplay,
handwriting, design work, or other creative output, pay close attention to
medium, texture, detail choices, and presentation. If little meaningful content
is visible, say so instead of guessing.

Return valid structured output matching this shape:
{
  "summary": "string",
  "detailed_description": "string",
  "notable_objects": ["string"],
  "people_or_characters": ["string"],
  "colors": ["string"],
  "composition": "string",
  "visible_text": ["string"],
  "style_cues": ["string"],
  "mood_cues": ["string"],
  "grounded_interpretation_hints": ["string"],
  "uncertainties": ["string"]
}

Rules:
- Ground every claim in visible evidence.
- Use cautious wording for interpretation.
- visible_text should quote readable text exactly when possible; if partial or
  unclear, mark that in the string.
- grounded_interpretation_hints should be short and explicitly tied to visible
  evidence, with at most 3 items.
- Leave arrays empty rather than guessing.
""".strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_openai_client_config(conn: Any | None = None) -> Dict[str, str]:
    env_api_key = (
        os.environ.get("RECENT_PHOTO_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()
    env_base_url = (
        os.environ.get("RECENT_PHOTO_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    if env_api_key:
        resolved = {"api_key": env_api_key}
        if env_base_url:
            resolved["base_url"] = env_base_url
        return resolved

    config = getattr(conn, "config", None) or {}
    llm_config = config.get("LLM", {}) if isinstance(config, dict) else {}
    selected_llm = (
        config.get("selected_module", {}).get("LLM") if isinstance(config, dict) else None
    )

    candidate_names: List[str] = []
    if selected_llm:
        candidate_names.append(str(selected_llm))
    if "OpenAILLM" not in candidate_names:
        candidate_names.append("OpenAILLM")

    for candidate_name in candidate_names:
        candidate = llm_config.get(candidate_name)
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("type") or "").strip().lower() != "openai":
            continue

        api_key = str(candidate.get("api_key") or "").strip()
        if not api_key:
            continue

        resolved = {"api_key": api_key}
        base_url = str(candidate.get("base_url") or candidate.get("url") or "").strip()
        if base_url:
            resolved["base_url"] = base_url
        return resolved

    raise RuntimeError(
        "Missing recent photo OpenAI credentials in environment and selected LLM config"
    )


def _get_openai_client(conn: Any | None = None) -> OpenAI:
    client_config = _resolve_openai_client_config(conn)
    if client_config.get("base_url"):
        return OpenAI(
            api_key=client_config["api_key"],
            base_url=client_config["base_url"],
        )
    return OpenAI(api_key=client_config["api_key"])


def _normalize_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _select_photo_url(photo: Dict[str, Any]) -> Optional[str]:
    for key in PHOTO_URL_FIELDS:
        value = str(photo.get(key) or "").strip()
        if value:
            return value
    gcs_path = str(photo.get("gcsPath") or "").strip()
    bucket = (
        os.environ.get("RECENT_PHOTO_GCS_BUCKET")
        or os.environ.get("FIREBASE_STORAGE_BUCKET")
        or os.environ.get("GCS_BUCKET")
        or ""
    ).strip()
    if gcs_path and bucket:
        return f"https://storage.googleapis.com/{bucket}/{gcs_path.lstrip('/')}"
    return None


def _select_photo_timestamp(photo: Dict[str, Any]) -> Optional[datetime]:
    for key in PHOTO_DATE_FIELDS:
        timestamp = _normalize_datetime(photo.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _select_recent_photo(
    photos: Iterable[Dict[str, Any]],
    *,
    lookback_hours: int = RECENCY_WINDOW_HOURS,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = now or _utc_now()
    cutoff = None
    if lookback_hours and lookback_hours > 0:
        cutoff = now - timedelta(hours=lookback_hours)

    candidates: List[tuple[datetime, Dict[str, Any]]] = []
    for photo in photos:
        if photo.get("deletedAt"):
            continue
        created_at = _select_photo_timestamp(photo)
        if created_at is None:
            continue
        if not _select_photo_url(photo):
            continue
        if cutoff is not None and created_at < cutoff:
            continue
        candidates.append((created_at, photo))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _load_collection_items(
    uid: str,
    collection_name: str,
    *,
    limit: int = PHOTO_QUERY_LIMIT,
) -> List[Dict[str, Any]]:
    client = get_firestore_client()
    snaps = (
        client.collection("users")
        .document(uid)
        .collection(collection_name)
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(max(1, limit))
        .stream()
    )

    photos: List[Dict[str, Any]] = []
    for snap in snaps:
        if not getattr(snap, "exists", True):
            continue
        data = snap.to_dict() or {}
        data.setdefault("id", getattr(snap, "id", None))
        data.setdefault("source_collection", collection_name)
        photos.append(data)
    return photos


def _load_candidate_photos(uid: str, *, limit: int = PHOTO_QUERY_LIMIT) -> List[Dict[str, Any]]:
    photos: List[Dict[str, Any]] = []
    for collection_name in PHOTO_SOURCE_COLLECTIONS:
        photos.extend(_load_collection_items(uid, collection_name, limit=limit))
    return photos


def _download_image_as_data_url(photo_url: str) -> str:
    if photo_url.startswith("data:"):
        return photo_url

    response = requests.get(photo_url, timeout=20)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    if not content_type:
        guessed_type, _ = mimetypes.guess_type(photo_url)
        content_type = guessed_type or "image/png"

    import base64

    encoded = base64.b64encode(response.content).decode("utf-8")
    return f"data:{content_type};base64,{encoded}"


def _extract_response_text(response: Any) -> str:
    text = (getattr(response, "output_text", None) or "").strip()
    if text:
        return text

    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        response_dict = to_dict()
        for item in response_dict.get("output", []):
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    candidate = (part.get("text") or "").strip()
                    if candidate:
                        return candidate
    return ""


def _extract_json_object(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Vision model did not return JSON")
    return candidate[start : end + 1]


def _escape_invalid_json_backslashes(text: str) -> str:
    valid_escapes = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}
    sanitized: List[str] = []
    in_string = False
    escaped = False
    i = 0

    while i < len(text):
        char = text[i]

        if not in_string:
            sanitized.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if escaped:
            sanitized.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\":
            next_char = text[i + 1] if i + 1 < len(text) else ""
            if next_char in valid_escapes:
                sanitized.append(char)
                escaped = True
            else:
                sanitized.append("\\\\")
            i += 1
            continue

        sanitized.append(char)
        if char == '"':
            in_string = False
        i += 1

    return "".join(sanitized)


def _validate_analysis_payload(parsed: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(parsed, dict):
        raise ValueError("Vision analysis JSON must be an object")

    required_keys = {
        "summary",
        "detailed_description",
        "notable_objects",
        "people_or_characters",
        "colors",
        "composition",
        "visible_text",
        "style_cues",
        "mood_cues",
        "grounded_interpretation_hints",
        "uncertainties",
    }
    missing = sorted(required_keys - set(parsed.keys()))
    if missing:
        raise ValueError(f"Vision analysis missing required keys: {', '.join(missing)}")
    return parsed


def _parse_analysis_json(text: str) -> Dict[str, Any]:
    candidate = _extract_json_object(text)
    try:
        return _validate_analysis_payload(json.loads(candidate))
    except json.JSONDecodeError as exc:
        sanitized = _escape_invalid_json_backslashes(candidate)
        if sanitized != candidate:
            try:
                return _validate_analysis_payload(json.loads(sanitized))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Vision model returned invalid JSON: {exc}") from exc


def _analyze_recent_photo(photo_url: str, conn: Any | None = None) -> Dict[str, Any]:
    client = _get_openai_client(conn)
    image_input = _download_image_as_data_url(photo_url)
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": VISION_ANALYSIS_PROMPT},
                    {"type": "input_image", "image_url": image_input},
                ],
            }
        ],
        max_output_tokens=900,
    )
    output_text = _extract_response_text(response)
    if not output_text:
        raise ValueError("Vision analysis returned empty text")
    return _parse_analysis_json(output_text)


def _build_tool_result(payload: Dict[str, Any]) -> str:
    guidance = (
        "Recent photo inspection result. "
        "If status is 'found', respond in character like a warm companion who "
        "actually looked at what the user shared: notice 1-2 specific visible "
        "details and respond in a way that fits the subject. If the photo feels "
        "personal, expressive, aesthetic, emotional, or creative, you can be "
        "more interpretive. If it is ordinary or practical, keep the response "
        "grounded and casual. Do not over-romanticize it, sound like a critic, "
        "captioning system, teacher, productivity coach, or use child-directed "
        "language. If status is 'no_match', clearly say you couldn't find a "
        "recent photo to inspect and ask the user to share one in the app. "
        "If status is 'error', briefly say you couldn't look at the photo just now "
        "and invite them to try again."
    )
    return f"{guidance}\n{json.dumps(payload, ensure_ascii=False)}"


@register_function(
    "inspect_recent_photo",
    INSPECT_RECENT_PHOTO_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
def inspect_recent_photo(conn) -> ActionResponse:
    uid = get_owner_phone_for_device(conn.device_id)
    if not uid:
        payload = {
            "status": "no_match",
            "photo_found": False,
            "reason": "No linked user was found for this device.",
            "recency_window_hours": RECENCY_WINDOW_HOURS,
            "source_collections": list(PHOTO_SOURCE_COLLECTIONS),
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

    try:
        photos = _load_candidate_photos(uid)
        photo = _select_recent_photo(photos)
        if not photo:
            payload = {
                "status": "no_match",
                "photo_found": False,
                "reason": "No usable recent photo was found.",
                "recency_window_hours": RECENCY_WINDOW_HOURS,
                "source_collections": list(PHOTO_SOURCE_COLLECTIONS),
            }
            return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

        photo_url = _select_photo_url(photo)
        if not photo_url:
            payload = {
                "status": "no_match",
                "photo_found": False,
                "reason": "A recent photo exists, but no usable image URL was found.",
                "recency_window_hours": RECENCY_WINDOW_HOURS,
                "source_collections": list(PHOTO_SOURCE_COLLECTIONS),
            }
            return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

        analysis = _analyze_recent_photo(photo_url, conn)
        created_at = _normalize_datetime(photo.get("createdAt"))
        payload = {
            "status": "found",
            "photo_found": True,
            "photo_id": photo.get("id"),
            "capture_timestamp": created_at.isoformat() if created_at else None,
            "recency_window_hours": RECENCY_WINDOW_HOURS,
            "source_collection": photo.get("source_collection"),
            "caption": photo.get("caption") or "",
            "text": photo.get("text") or "",
            "analysis": analysis,
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

    except (APIError, requests.RequestException, ValueError, RuntimeError) as exc:
        logger.bind(tag=TAG).error(f"Recent photo inspection failed for {uid}: {exc}")
        payload = {
            "status": "error",
            "photo_found": False,
            "reason": str(exc),
            "recency_window_hours": RECENCY_WINDOW_HOURS,
            "source_collections": list(PHOTO_SOURCE_COLLECTIONS),
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))
    except Exception as exc:  # pragma: no cover - defensive catch for prod safety
        logger.bind(tag=TAG).error(f"Unexpected recent photo inspection failure: {exc}")
        payload = {
            "status": "error",
            "photo_found": False,
            "reason": "Unexpected error while inspecting the recent photo.",
            "recency_window_hours": RECENCY_WINDOW_HOURS,
            "source_collections": list(PHOTO_SOURCE_COLLECTIONS),
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))
