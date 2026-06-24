from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.handle import receiveAudioHandle


def test_start_to_chat_aborts_busy_response_before_forwarding_user_query(monkeypatch):
    sent_stt = []
    submitted = []
    websocket_messages = []
    queues_cleared = []

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

    class _WebSocket:
        def __init__(self, owner):
            self.owner = owner

        async def send(self, payload):
            websocket_messages.append(payload)
            self.owner.llm_finish_task = True

    conn = SimpleNamespace(
        need_bind=False,
        llm_finish_task=False,
        max_output_size=0,
        client_is_speaking=True,
        headers={"device-id": "90:e5:b1:a8:e4:38"},
        logger=_Logger(),
        executor=_Executor(),
        chat=lambda text: text,
        config={"barge_in_wait_timeout_seconds": 0.2},
        clear_queues=lambda: queues_cleared.append(True),
        clearSpeakStatus=lambda: setattr(conn, "client_is_speaking", False),
        session_id="session-1",
    )
    conn.websocket = _WebSocket(conn)

    asyncio.run(receiveAudioHandle.startToChat(conn, "set a reminder"))

    assert queues_cleared == [True]
    assert '"state": "stop"' in websocket_messages[0]
    assert sent_stt == ["set a reminder"]
    assert len(submitted) == 1
    assert submitted[0][1] == ("set a reminder",)
