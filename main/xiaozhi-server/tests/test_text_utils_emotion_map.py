from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.utils import textUtils


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class _FakeLogger:
    def bind(self, **_kwargs):
        return self

    def warning(self, *_args, **_kwargs):
        pass


def _llm_emotion_payload(text: str) -> dict:
    conn = SimpleNamespace(
        websocket=_FakeWebSocket(),
        session_id="session-1",
        logger=_FakeLogger(),
    )
    asyncio.run(textUtils.get_emotion(conn, text))
    return conn.websocket.messages[-1]


def test_cheerful_emojis_are_hardcoded_and_allowed():
    cheerful_emojis = {"😊", "☺️", "🙂", "😌", "😇", "🤭"}

    assert set(textUtils.CANONICAL_EMOTION_MAP["cheerful"]) == cheerful_emojis
    assert textUtils.CANONICAL_EMOTION_MAP["blush"] == ["😳"]
    assert "🫥" in textUtils.CANONICAL_EMOTION_MAP["smirk"]
    assert {"🥲", "🫠", "😔"}.issubset(textUtils.CANONICAL_EMOTION_MAP["sad"])

    allowed_emojis = set(textUtils.get_allowed_emoji_list_string().split())
    assert cheerful_emojis.issubset(allowed_emojis)
    assert {"🫥", "🥲", "🫠", "😔"}.issubset(allowed_emojis)


def test_cheerful_leading_emoji_sends_cheerful_llm_emotion():
    payload = _llm_emotion_payload("😊 I am happy")

    assert payload == {
        "type": "llm",
        "text": "😊",
        "emotion": "cheerful",
        "session_id": "session-1",
    }


def test_multi_codepoint_cheerful_emoji_maps_to_cheerful():
    payload = _llm_emotion_payload("☺️ feeling sunny")

    assert payload["text"] == "☺️"
    assert payload["emotion"] == "cheerful"


def test_blush_and_default_emotions_still_work():
    assert _llm_emotion_payload("😳 whoops")["emotion"] == "blush"

    payload = _llm_emotion_payload("plain response")
    assert payload["text"] == "😄"
    assert payload["emotion"] == "normal"
