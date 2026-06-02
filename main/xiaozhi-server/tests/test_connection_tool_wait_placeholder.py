from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.connection import ConnectionHandler
from plugins_func.functions import get_weather as weather_tool


class _Logger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def test_magic_camera_tool_wait_placeholder_queues_ephemeral_hmmm():
    spoken = []

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="final answer",
        logger=_Logger(),
        tts=SimpleNamespace(
            play_interstitial=lambda conn, content_detail: spoken.append(content_detail),
        ),
    )

    ConnectionHandler._maybe_emit_tool_wait_placeholder(
        conn,
        "inspect_recent_magic_camera_photo",
    )

    assert spoken == ["hmmm"]
    assert conn.tts_MessageText == "final answer"


def test_non_magic_camera_tool_skips_wait_placeholder():
    spoken = []

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="final answer",
        logger=_Logger(),
        tts=SimpleNamespace(
            play_interstitial=lambda conn, content_detail: spoken.append(content_detail),
        ),
    )

    ConnectionHandler._maybe_emit_tool_wait_placeholder(conn, "get_weather")

    assert spoken == []
    assert conn.tts_MessageText == "final answer"


def test_missing_interstitial_support_skips_placeholder_cleanly():
    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="final answer",
        logger=_Logger(),
        tts=SimpleNamespace(),
    )

    ConnectionHandler._maybe_emit_tool_wait_placeholder(
        conn,
        "inspect_recent_magic_camera_photo",
    )

    assert conn.tts_MessageText == "final answer"


def test_failed_weather_fallback_reaches_conversation_function_output(monkeypatch):
    monkeypatch.setattr(
        weather_tool,
        "build_weather_report",
        lambda location: (None, "Failed to get weather data: read timed out"),
    )
    result = weather_tool.get_weather(
        SimpleNamespace(
            client_ip="203.0.113.10",
            config={"plugins": {"get_weather": {"default_location": "Boston"}}},
        ),
        location="Boston",
        lang="en_US",
    )

    chat_calls = []
    conn = SimpleNamespace(
        llm=SimpleNamespace(has_conversation=lambda _session_id: True),
        session_id="session-1",
        logger=_Logger(),
        chat=lambda *args, **kwargs: chat_calls.append((args, kwargs)),
    )

    ConnectionHandler._handle_function_result(
        conn,
        result,
        {"id": "call-weather-1"},
        depth=0,
    )

    assert chat_calls
    output = chat_calls[0][1]["extra_inputs"][0]["output"]
    assert "Boston" in output
    assert "temporarily unavailable" in output
    assert "timed out" not in output
    assert "timeout" not in output.lower()
