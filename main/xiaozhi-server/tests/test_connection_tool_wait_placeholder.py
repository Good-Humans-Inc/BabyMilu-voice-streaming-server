from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.connection import ConnectionHandler
from core.providers.tts.dto.dto import SentenceType


class _Logger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def test_magic_camera_tool_wait_placeholder_queues_ephemeral_hmmm():
    queued = []
    spoken = []

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="final answer",
        logger=_Logger(),
        tts=SimpleNamespace(
            tts_text_queue=SimpleNamespace(put=lambda item: queued.append(item)),
            tts_one_sentence=lambda conn, content_type, content_detail: spoken.append(content_detail),
        ),
    )

    ConnectionHandler._maybe_emit_tool_wait_placeholder(
        conn,
        "inspect_recent_magic_camera_photo",
    )

    assert [item.sentence_type for item in queued] == [SentenceType.FIRST, SentenceType.LAST]
    assert spoken == ["hmmm"]
    assert conn.tts_MessageText == "final answer"


def test_non_magic_camera_tool_skips_wait_placeholder():
    queued = []
    spoken = []

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="final answer",
        logger=_Logger(),
        tts=SimpleNamespace(
            tts_text_queue=SimpleNamespace(put=lambda item: queued.append(item)),
            tts_one_sentence=lambda conn, content_type, content_detail: spoken.append(content_detail),
        ),
    )

    ConnectionHandler._maybe_emit_tool_wait_placeholder(conn, "get_weather")

    assert queued == []
    assert spoken == []
    assert conn.tts_MessageText == "final answer"
