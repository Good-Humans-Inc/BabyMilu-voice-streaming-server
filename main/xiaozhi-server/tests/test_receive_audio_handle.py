from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.handle import receiveAudioHandle


def test_start_to_chat_drops_user_query_when_llm_busy(monkeypatch):
    sent_stt = []
    submitted = []

    async def fake_send_stt_message(conn, text):
        sent_stt.append(text)

    async def fake_handle_user_intent(conn, text):
        return False

    monkeypatch.setattr(receiveAudioHandle, "send_stt_message", fake_send_stt_message)
    monkeypatch.setattr(receiveAudioHandle, "handle_user_intent", fake_handle_user_intent)
    monkeypatch.setattr(receiveAudioHandle, "check_device_output_limit", lambda *args, **kwargs: False)

    class _Logger:
        def bind(self, **kwargs):
            return self

        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

    class _Executor:
        def submit(self, fn, *args, **kwargs):
            submitted.append((fn, args, kwargs))

    conn = SimpleNamespace(
        need_bind=False,
        llm_finish_task=False,
        max_output_size=0,
        client_is_speaking=False,
        headers={"device-id": "90:e5:b1:a8:e4:38"},
        logger=_Logger(),
        executor=_Executor(),
        chat=lambda text: text,
    )

    asyncio.run(receiveAudioHandle.startToChat(conn, "set a reminder"))

    assert sent_stt == []
    assert submitted == []
