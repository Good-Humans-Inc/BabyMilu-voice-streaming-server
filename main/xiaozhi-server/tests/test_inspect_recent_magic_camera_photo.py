from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = DATA_DIR / ".config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text((ROOT / "config.yaml").read_text())

sys.path.insert(0, str(ROOT))

from plugins_func.functions import inspect_recent_magic_camera_photo as inspect_module
from plugins_func.register import Action


def _conn(device_id: str = "AA:BB:CC:DD:EE:FF", config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(device_id=device_id, config=config or {})


def _extract_payload(result) -> dict:
    lines = [line for line in result.result.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_select_recent_magic_photo_prefers_recent_non_deleted_photo():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    photos = [
        {
            "id": "old-photo",
            "processedPhotoUrl": "https://example.com/old.png",
            "createdAt": now - timedelta(hours=30),
        },
        {
            "id": "fresh-photo",
            "processedPhotoUrl": "https://example.com/fresh.png",
            "createdAt": now - timedelta(hours=2),
        },
    ]

    selected = inspect_module._select_recent_magic_photo(photos, now=now)

    assert selected["id"] == "fresh-photo"


def test_select_recent_magic_photo_skips_deleted_or_url_less_entries():
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    photos = [
        {
            "id": "deleted",
            "processedPhotoUrl": "https://example.com/deleted.png",
            "createdAt": now - timedelta(hours=1),
            "deletedAt": now.isoformat(),
        },
        {
            "id": "missing-url",
            "createdAt": now - timedelta(hours=1),
        },
    ]

    selected = inspect_module._select_recent_magic_photo(photos, now=now)

    assert selected is None


def test_select_photo_url_prefers_original_photo_url_over_processed_url():
    selected_url = inspect_module._select_photo_url(
        {
            "photoUrl": "https://example.com/original.png",
            "processedPhotoUrl": "https://example.com/processed.png",
            "cardUrl": "https://example.com/card.png",
        }
    )

    assert selected_url == "https://example.com/original.png"


def test_get_openai_client_falls_back_to_selected_llm_config(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.delenv("MAGIC_CAMERA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MAGIC_CAMERA_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(inspect_module, "OpenAI", FakeOpenAI)

    conn = _conn(
        config={
            "selected_module": {"LLM": "OpenAILLM"},
            "LLM": {
                "OpenAILLM": {
                    "type": "openai",
                    "api_key": "config-openai-key",
                    "base_url": "https://api.openai.com/v1",
                }
            },
        }
    )

    inspect_module._get_openai_client(conn)

    assert captured == {
        "api_key": "config-openai-key",
        "base_url": "https://api.openai.com/v1",
    }


def test_tool_returns_no_match_when_uid_is_missing(monkeypatch):
    monkeypatch.setattr(inspect_module, "get_owner_phone_for_device", lambda _: None)

    result = inspect_module.inspect_recent_magic_camera_photo(_conn())
    payload = _extract_payload(result)

    assert result.action == Action.REQLLM
    assert payload["status"] == "no_match"
    assert payload["recency_window_hours"] == inspect_module.RECENCY_WINDOW_HOURS


def test_tool_returns_no_match_when_recent_photo_is_missing(monkeypatch):
    monkeypatch.setattr(inspect_module, "get_owner_phone_for_device", lambda _: "+15551234567")
    monkeypatch.setattr(
        inspect_module,
        "_load_candidate_photos",
        lambda uid: [
            {
                "id": "old-photo",
                "processedPhotoUrl": "https://example.com/old.png",
                "createdAt": "2026-05-04T12:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        inspect_module,
        "_utc_now",
        lambda: datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
    )

    result = inspect_module.inspect_recent_magic_camera_photo(_conn())
    payload = _extract_payload(result)

    assert result.action == Action.REQLLM
    assert payload["status"] == "no_match"


def test_tool_returns_analysis_payload_for_recent_photo(monkeypatch):
    monkeypatch.setattr(inspect_module, "get_owner_phone_for_device", lambda _: "+15551234567")
    monkeypatch.setattr(
        inspect_module,
        "_load_candidate_photos",
        lambda uid: [
            {
                "id": "photo-123",
                "processedPhotoUrl": "https://example.com/fresh.png",
                "caption": "look at this",
                "createdAt": "2026-05-06T10:30:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(
        inspect_module,
        "_utc_now",
        lambda: datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        inspect_module,
        "_analyze_magic_camera_photo",
        lambda photo_url, conn: {
            "summary": "A painted figurine sits on a desk.",
            "detailed_description": "The image shows a hand-painted figurine with blue accents on a light desk surface.",
            "notable_objects": ["painted figurine", "desk"],
            "people_or_characters": [],
            "colors": ["blue", "white"],
            "composition": "The figurine is centered in the frame.",
            "visible_text": [],
            "style_cues": ["handmade"],
            "mood_cues": ["careful"],
            "grounded_interpretation_hints": ["The visible brushwork suggests handmade effort."],
            "uncertainties": [],
        },
    )

    result = inspect_module.inspect_recent_magic_camera_photo(_conn())
    payload = _extract_payload(result)

    assert result.action == Action.REQLLM
    assert payload["status"] == "found"
    assert payload["photo_id"] == "photo-123"
    assert payload["caption"] == "look at this"
    assert payload["analysis"]["summary"] == "A painted figurine sits on a desk."
