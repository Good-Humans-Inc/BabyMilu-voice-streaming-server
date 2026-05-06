"""LLM tool: inspect_recent_magic_camera_photo

Inspect the user's most recent Magic Camera photo within a recent time window
and return grounded visual context for the main character model.
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

RECENCY_WINDOW_HOURS = int(os.environ.get("MAGIC_CAMERA_LOOKBACK_HOURS", "24"))
PHOTO_QUERY_LIMIT = int(os.environ.get("MAGIC_CAMERA_QUERY_LIMIT", "5"))
OPENAI_MODEL = os.environ.get("MAGIC_CAMERA_INSPECT_MODEL", "gpt-4o-mini").strip()

INSPECT_MAGIC_CAMERA_PHOTO_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "inspect_recent_magic_camera_photo",
        "description": (
            "Use this when the user wants you to inspect, react to, discuss, or "
            "interpret a recent photo taken with Magic Camera in the app. "
            "The tool finds the most recent qualifying Magic Camera photo in the "
            "allowed recency window, analyzes it, and returns a rich grounded "
            "description for your in-character response. If no qualifying photo "
            "is found, it returns a no-match result instead of guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

VISION_ANALYSIS_PROMPT = """
You are analyzing a user's recent Magic Camera photo for BabyMilu's voice
interaction system. Your job is to inspect the image and return structured,
grounded visual context for another LLM that will speak to the user in
character. Do not address the user directly. Do not praise, critique, or give
advice. Do not invent details that are not visible. If something is uncertain,
ambiguous, partially obscured, stylized, or hard to identify, say so clearly.
Prefer concrete observations over vague adjectives. If the image contains art,
craft, cosplay, handwriting, design work, or other creative output, pay close
attention to medium, texture, detail choices, and presentation. If little
meaningful content is visible, say so instead of guessing.

Return valid JSON matching this exact shape:
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


def _get_openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable")
    return OpenAI(api_key=api_key)


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
    for key in ("photoUrl", "processedPhotoUrl", "cardUrl"):
        value = str(photo.get(key) or "").strip()
        if value:
            return value
    return None


def _select_recent_magic_photo(
    photos: Iterable[Dict[str, Any]],
    *,
    lookback_hours: int = RECENCY_WINDOW_HOURS,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = now or _utc_now()
    cutoff = now - timedelta(hours=max(1, lookback_hours))
    for photo in photos:
        if photo.get("deletedAt"):
            continue
        created_at = _normalize_datetime(photo.get("createdAt"))
        if created_at is None or created_at < cutoff:
            continue
        if not _select_photo_url(photo):
            continue
        return photo
    return None


def _load_candidate_photos(uid: str, *, limit: int = PHOTO_QUERY_LIMIT) -> List[Dict[str, Any]]:
    client = get_firestore_client()
    snaps = (
        client.collection("users")
        .document(uid)
        .collection("magicPhotos")
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
        photos.append(data)
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


def _parse_analysis_json(text: str) -> Dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Vision model did not return JSON")
    parsed = json.loads(candidate[start : end + 1])

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


def _analyze_magic_camera_photo(photo_url: str) -> Dict[str, Any]:
    client = _get_openai_client()
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
        "Magic Camera inspection result. "
        "If status is 'found', respond in character as a creative companion: "
        "mention specific visible details, react with genuine feeling, add only "
        "light grounded interpretation, and invite the user's story when natural. "
        "If status is 'no_match', clearly say you couldn't find a recent Magic "
        "Camera photo to inspect, ask the user to take one in the app, and say "
        "you can patiently wait for them to come back with their masterpiece. "
        "If status is 'error', briefly say you couldn't look at the photo just now "
        "and invite them to try again."
    )
    return f"{guidance}\n{json.dumps(payload, ensure_ascii=False)}"


@register_function(
    "inspect_recent_magic_camera_photo",
    INSPECT_MAGIC_CAMERA_PHOTO_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
def inspect_recent_magic_camera_photo(conn) -> ActionResponse:
    uid = get_owner_phone_for_device(conn.device_id)
    if not uid:
        payload = {
            "status": "no_match",
            "photo_found": False,
            "reason": "No linked user was found for this device.",
            "recency_window_hours": RECENCY_WINDOW_HOURS,
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

    try:
        photos = _load_candidate_photos(uid)
        photo = _select_recent_magic_photo(photos)
        if not photo:
            payload = {
                "status": "no_match",
                "photo_found": False,
                "reason": "No recent Magic Camera photo was found in the allowed recency window.",
                "recency_window_hours": RECENCY_WINDOW_HOURS,
            }
            return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

        photo_url = _select_photo_url(photo)
        if not photo_url:
            payload = {
                "status": "no_match",
                "photo_found": False,
                "reason": "A recent Magic Camera photo exists, but no usable image URL was found.",
                "recency_window_hours": RECENCY_WINDOW_HOURS,
            }
            return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

        analysis = _analyze_magic_camera_photo(photo_url)
        created_at = _normalize_datetime(photo.get("createdAt"))
        payload = {
            "status": "found",
            "photo_found": True,
            "photo_id": photo.get("id"),
            "capture_timestamp": created_at.isoformat() if created_at else None,
            "recency_window_hours": RECENCY_WINDOW_HOURS,
            "caption": photo.get("caption") or "",
            "analysis": analysis,
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))

    except (APIError, requests.RequestException, ValueError, RuntimeError) as exc:
        logger.bind(tag=TAG).error(f"Magic Camera inspection failed for {uid}: {exc}")
        payload = {
            "status": "error",
            "photo_found": False,
            "reason": str(exc),
            "recency_window_hours": RECENCY_WINDOW_HOURS,
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))
    except Exception as exc:  # pragma: no cover - defensive catch for prod safety
        logger.bind(tag=TAG).error(f"Unexpected Magic Camera inspection failure: {exc}")
        payload = {
            "status": "error",
            "photo_found": False,
            "reason": "Unexpected error while inspecting the recent Magic Camera photo.",
            "recency_window_hours": RECENCY_WINDOW_HOURS,
        }
        return ActionResponse(action=Action.REQLLM, result=_build_tool_result(payload))
