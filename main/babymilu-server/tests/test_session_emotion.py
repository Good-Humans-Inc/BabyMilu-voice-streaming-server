from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from babymilu_server.models import VoiceSession
from babymilu_server.providers.llm import LLMStreamEvent
from babymilu_server.session import VoiceSessionHandler, _emotion_from_text


class FakeWebSocket:
    closed = False

    def __init__(self) -> None:
        self.json_messages: list[dict[str, Any]] = []
        self.binary_messages: list[bytes] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.json_messages.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


class FakeTTS:
    async def stream_opus_frames(self, request: Any) -> AsyncIterator[bytes]:
        if request is None:
            yield b""


class FakeLLM:
    async def stream_complete(self, **kwargs: Any) -> AsyncIterator[LLMStreamEvent]:
        yield LLMStreamEvent(kind="content", content="That feels bright and happy for our little test ")
        yield LLMStreamEvent(kind="content", content="\U0001f604. ")
        yield LLMStreamEvent(kind="content", content="Now it is soft and sleepy after the second turn ")
        yield LLMStreamEvent(kind="content", content="\U0001f634.")
        yield LLMStreamEvent(kind="done", tool_calls=[])


def _handler(*, llm: Any = None) -> VoiceSessionHandler:
    return VoiceSessionHandler(
        starter_store=None,  # type: ignore[arg-type]
        session_store=None,  # type: ignore[arg-type]
        prompt_builder=None,  # type: ignore[arg-type]
        asr=None,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
        tts=FakeTTS(),  # type: ignore[arg-type]
        tools=None,  # type: ignore[arg-type]
        auto_resume_listening_after_tts=False,
        tts_frame_pacing_ms=0,
    )


def test_emotion_from_text_maps_common_emoji_to_firmware_emotions() -> None:
    assert _emotion_from_text("Yay \U0001f604") == "happy"
    assert _emotion_from_text("Oh no \U0001f62d") == "cry"
    assert _emotion_from_text("Plain text") is None


def test_assistant_text_sends_llm_emotion_before_text() -> None:
    asyncio.run(_run_assistant_text_sends_llm_emotion_before_text())


async def _run_assistant_text_sends_llm_emotion_before_text() -> None:
    handler = _handler()
    session = VoiceSession(session_id="S1", device_id="D1")
    ws = FakeWebSocket()

    await handler._send_assistant_text(ws, session, "I love that idea \u2764\ufe0f", "conversation")  # noqa: SLF001

    assert ws.json_messages[0] == {"type": "llm", "emotion": "heart"}
    assert ws.json_messages[1]["type"] == "text"


def test_streaming_response_sends_emotion_for_each_emoji_chunk() -> None:
    asyncio.run(_run_streaming_response_sends_emotion_for_each_emoji_chunk())


async def _run_streaming_response_sends_emotion_for_each_emoji_chunk() -> None:
    handler = _handler(llm=FakeLLM())
    session = VoiceSession(session_id="S1", device_id="D1")
    ws = FakeWebSocket()

    response, tool_calls = await handler._stream_llm_to_tts(  # noqa: SLF001
        ws,
        session,
        system_prompt="",
        messages=[],
        tools=[],
        turn_started_at=0.0,
    )

    emotions = [message["emotion"] for message in ws.json_messages if message.get("type") == "llm"]
    assert emotions == ["happy", "sleep"]
    assert response.endswith("\U0001f634.")
    assert tool_calls == []
