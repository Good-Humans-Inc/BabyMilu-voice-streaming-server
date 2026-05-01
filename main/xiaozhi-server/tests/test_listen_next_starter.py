from __future__ import annotations

import asyncio
import pathlib
import queue
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.handle.textHandler import listenMessageHandler as listen_mod
from core.providers.tts.dto.dto import ContentType, SentenceType


class _Logger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def test_maybe_play_next_starter_text_only(monkeypatch):
    consumed = []

    def fake_mark_next_starter_consumed(character_id, payload):
        consumed.append((character_id, payload))
        return True

    monkeypatch.setattr(listen_mod, "mark_next_starter_consumed", fake_mark_next_starter_consumed)

    tts_calls = []
    dialogue_messages = []
    queue_items = []

    class _FakeQueue:
        def put(self, item):
            queue_items.append(item)

    conn = SimpleNamespace(
        active_character_id="char_123",
        next_starter_payload={
            "status": "ready",
            "characterId": "char_123",
            "text": "Starter from text only.",
            "generatedAt": "2026-05-01T07:00:00+00:00",
            "sourceSessionId": "sess_1",
        },
        next_starter_scheduled=False,
        tts=SimpleNamespace(
            tts_text_queue=_FakeQueue(),
            tts_one_sentence=lambda conn, content_type, content_detail: tts_calls.append(
                (content_type, content_detail)
            ),
        ),
        dialogue=SimpleNamespace(put=lambda msg: dialogue_messages.append(msg.content)),
        logger=_Logger(),
        client_abort=True,
        sentence_id=None,
        tts_MessageText="",
    )

    played = asyncio.run(listen_mod._maybe_play_next_starter(conn))

    assert played is True
    assert conn.client_abort is False
    assert conn.next_starter_payload is None
    assert tts_calls == [(ContentType.TEXT, "Starter from text only.")]
    assert dialogue_messages == ["Starter from text only."]
    assert len(queue_items) == 2
    assert queue_items[0].sentence_type == SentenceType.FIRST
    assert queue_items[1].sentence_type == SentenceType.LAST
    assert consumed == [
        (
            "char_123",
            {
                "status": "ready",
                "characterId": "char_123",
                "text": "Starter from text only.",
                "generatedAt": "2026-05-01T07:00:00+00:00",
                "sourceSessionId": "sess_1",
            },
        )
    ]
