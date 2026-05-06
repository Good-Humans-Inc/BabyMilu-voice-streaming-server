from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.connection import ConnectionHandler


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
