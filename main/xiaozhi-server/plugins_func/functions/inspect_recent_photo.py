"""LLM tool: inspect_recent_photo

Inspect the user's most recent photo within a recent time window and return
grounded visual context for the main character model.
"""
from __future__ import annotations

import json
import hashlib
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import google.auth
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import firestore, storage
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
SIGNED_URL_TTL_MINUTES = int(
    os.environ.get(
        "RECENT_PHOTO_SIGNED_URL_TTL_MINUTES",
        os.environ.get("PEEK_SIGNED_URL_TTL_MINUTES", "15"),
    )
)
GCS_BUCKET_ENV_KEYS = (
    "RECENT_PHOTO_GCS_BUCKET",
    "PRIVATE_PHOTOS_BUCKET",
    "PEEK_PHOTOS_BUCKET",
    "FIREBASE_STORAGE_BUCKET",
    "GCS_BUCKET",
    "OUTPUT_BUCKET",
)
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
_IAM_SIGNING_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_storage_client: Optional[storage.Client] = None

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


def _configured_gcs_bucket() -> str:
    for key in GCS_BUCKET_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _resolve_gcs_reference(gcs_path: str) -> Optional[tuple[str, str]]:
    cleaned = str(gcs_path or "").strip()
    if not cleaned:
        return None

    if cleaned.startswith("gs://"):
        remainder = cleaned[len("gs://") :]
        bucket_name, _, blob_path = remainder.partition("/")
        bucket_name = bucket_name.strip()
        blob_path = blob_path.lstrip("/")
        if bucket_name and blob_path:
            return bucket_name, blob_path
        return None

    bucket_name = _configured_gcs_bucket()
    blob_path = cleaned.lstrip("/")
    if bucket_name and blob_path:
        return bucket_name, blob_path
    return None


def _get_storage_client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _runtime_signing_identity() -> tuple[Optional[str], Optional[str]]:
    try:
        credentials, _ = google.auth.default(scopes=_IAM_SIGNING_SCOPES)
    except Exception:
        client = _get_storage_client()
        credentials = getattr(client, "_credentials", None) or getattr(
            client,
            "credentials",
            None,
        )
        if credentials is None:
            return None, None

        if getattr(credentials, "requires_scopes", False) and hasattr(
            credentials,
            "with_scopes",
        ):
            try:
                credentials = credentials.with_scopes(_IAM_SIGNING_SCOPES)
            except Exception:
                return None, None

    try:
        if not getattr(credentials, "token", None) or not getattr(credentials, "valid", False):
            credentials.refresh(GoogleAuthRequest())
    except Exception:
        return None, None

    service_account_email = (
        getattr(credentials, "service_account_email", None)
        or getattr(credentials, "signer_email", None)
    )
    if service_account_email == "default":
        return None, None

    access_token = getattr(credentials, "token", None)
    if not service_account_email or not access_token:
        return None, None

    return service_account_email, access_token


def _build_gcs_signed_url(gcs_path: str) -> Optional[str]:
    resolved = _resolve_gcs_reference(gcs_path)
    if resolved is None:
        return None

    bucket_name, blob_path = resolved
    blob = _get_storage_client().bucket(bucket_name).blob(blob_path)
    kwargs = {
        "version": "v4",
        "method": "GET",
        "expiration": timedelta(minutes=max(1, SIGNED_URL_TTL_MINUTES)),
    }
    try:
        signed_url = blob.generate_signed_url(**kwargs)
    except AttributeError as exc:
        if "private key to sign credentials" not in str(exc).lower():
            raise

        service_account_email, access_token = _runtime_signing_identity()
        if not service_account_email or not access_token:
            raise RuntimeError(
                "GCS signed URL generation requires service account signing credentials"
            ) from exc

        signed_url = blob.generate_signed_url(
            **kwargs,
            service_account_email=service_account_email,
            access_token=access_token,
        )

    logger.bind(tag=TAG).info(
        "Recent photo GCS signed URL generated: "
        f"bucket={bucket_name}, path={_truncate_log_value(blob_path)}, "
        f"ttl_minutes={max(1, SIGNED_URL_TTL_MINUTES)}"
    )
    return signed_url


def _select_photo_url(photo: Dict[str, Any]) -> Optional[str]:
    for key in PHOTO_URL_FIELDS:
        value = str(photo.get(key) or "").strip()
        if value:
            return value
    gcs_path = str(photo.get("gcsPath") or "").strip()
    if gcs_path:
        return _build_gcs_signed_url(gcs_path)
    return None


def _truncate_log_value(value: Any, *, max_length: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _select_photo_url_field(photo: Dict[str, Any]) -> Optional[str]:
    for key in PHOTO_URL_FIELDS:
        value = str(photo.get(key) or "").strip()
        if value:
            return key
    gcs_path = str(photo.get("gcsPath") or "").strip()
    if gcs_path and _resolve_gcs_reference(gcs_path):
        return "gcsPath"
    return None


def _select_photo_timestamp(photo: Dict[str, Any]) -> Optional[datetime]:
    for key in PHOTO_DATE_FIELDS:
        timestamp = _normalize_datetime(photo.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _select_photo_timestamp_field(photo: Dict[str, Any]) -> Optional[str]:
    for key in PHOTO_DATE_FIELDS:
        if _normalize_datetime(photo.get(key)) is not None:
            return key
    return None


def _cache_busted_photo_url(photo_url: str) -> str:
    parts = urlsplit(photo_url)
    query_keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if any(
        key in query_keys
        for key in ("x-goog-signature", "x-amz-signature", "signature")
    ):
        return photo_url

    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(
        (
            "_recent_photo_cache_bust",
            str(int(_utc_now().timestamp() * 1000)),
        )
    )
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


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
        photo_id = photo.get("id")
        source_collection = photo.get("source_collection")
        if photo.get("deletedAt"):
            logger.bind(tag=TAG).info(
                "Recent photo candidate skipped: "
                f"id={photo_id}, source={source_collection}, reason=deletedAt_present"
            )
            continue
        created_at = _select_photo_timestamp(photo)
        if created_at is None:
            logger.bind(tag=TAG).info(
                "Recent photo candidate skipped: "
                f"id={photo_id}, source={source_collection}, reason=missing_timestamp"
            )
            continue
        timestamp_field = _select_photo_timestamp_field(photo)
        url_field = _select_photo_url_field(photo)
        if not url_field:
            logger.bind(tag=TAG).info(
                "Recent photo candidate skipped: "
                f"id={photo_id}, source={source_collection}, "
                f"timestamp={created_at.isoformat()}, timestamp_field={timestamp_field}, "
                "reason=missing_url"
            )
            continue
        if cutoff is not None and created_at < cutoff:
            logger.bind(tag=TAG).info(
                "Recent photo candidate skipped: "
                f"id={photo_id}, source={source_collection}, "
                f"timestamp={created_at.isoformat()}, timestamp_field={timestamp_field}, "
                f"url_field={url_field}, cutoff={cutoff.isoformat()}, "
                "reason=outside_recency_window"
            )
            continue
        logger.bind(tag=TAG).info(
            "Recent photo candidate accepted: "
            f"id={photo_id}, source={source_collection}, "
            f"timestamp={created_at.isoformat()}, timestamp_field={timestamp_field}, "
            f"url_field={url_field}"
        )
        candidates.append((created_at, photo))

    if not candidates:
        logger.bind(tag=TAG).info(
            "Recent photo selection found no usable candidates: "
            f"lookback_hours={lookback_hours}, cutoff={cutoff.isoformat() if cutoff else None}"
        )
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected_at, selected_photo = candidates[0]
    logger.bind(tag=TAG).info(
        "Recent photo selected: "
        f"id={selected_photo.get('id')}, "
        f"source={selected_photo.get('source_collection')}, "
        f"timestamp={selected_at.isoformat()}, "
        f"timestamp_field={_select_photo_timestamp_field(selected_photo)}, "
        f"url_field={_select_photo_url_field(selected_photo)}, "
        f"accepted_candidates={len(candidates)}"
    )
    return selected_photo


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
    logger.bind(tag=TAG).info(
        "Recent photo collection loaded: "
        f"uid={uid}, collection={collection_name}, count={len(photos)}, limit={limit}"
    )
    return photos


def _load_candidate_photos(uid: str, *, limit: int = PHOTO_QUERY_LIMIT) -> List[Dict[str, Any]]:
    photos: List[Dict[str, Any]] = []
    for collection_name in PHOTO_SOURCE_COLLECTIONS:
        photos.extend(_load_collection_items(uid, collection_name, limit=limit))
    logger.bind(tag=TAG).info(
        "Recent photo candidates loaded: "
        f"uid={uid}, total={len(photos)}, collections={list(PHOTO_SOURCE_COLLECTIONS)}"
    )
    return photos


def _download_image_as_data_url(photo_url: str) -> str:
    if photo_url.startswith("data:"):
        logger.bind(tag=TAG).info(
            "Recent photo image input is already a data URL: "
            f"length={len(photo_url)}"
        )
        return photo_url

    request_url = _cache_busted_photo_url(photo_url)
    response = requests.get(
        request_url,
        timeout=20,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    if not content_type:
        guessed_type, _ = mimetypes.guess_type(photo_url)
        content_type = guessed_type or "image/png"

    import base64

    image_sha256 = hashlib.sha256(response.content).hexdigest()
    logger.bind(tag=TAG).info(
        "Recent photo image downloaded: "
        f"status={response.status_code}, content_type={content_type}, "
        f"bytes={len(response.content)}, sha256={image_sha256[:16]}"
    )

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
        "Treat this tool output as the authoritative fresh photo context, even "
        "if earlier conversation summaries mention a different image. "
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
        selected_at = _select_photo_timestamp(photo)
        timestamp_field = _select_photo_timestamp_field(photo)
        logger.bind(tag=TAG).info(
            "Recent photo analysis completed: "
            f"id={photo.get('id')}, source={photo.get('source_collection')}, "
            f"timestamp={selected_at.isoformat() if selected_at else None}, "
            f"timestamp_field={timestamp_field}, "
            f"summary={_truncate_log_value(analysis.get('summary'))}, "
            f"notable_objects={_truncate_log_value(analysis.get('notable_objects'))}"
        )
        payload = {
            "status": "found",
            "photo_found": True,
            "photo_id": photo.get("id"),
            "capture_timestamp": selected_at.isoformat() if selected_at else None,
            "capture_timestamp_field": timestamp_field,
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
