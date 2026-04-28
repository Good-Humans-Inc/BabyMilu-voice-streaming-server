from __future__ import annotations

import asyncio
import pathlib
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

util_stub = types.ModuleType("core.utils.util")
util_stub.audio_to_data = lambda *args, **kwargs: []
sys.modules.setdefault("core.utils.util", util_stub)

from core.handle import sendAudioHandle
from core.providers.tts.dto.dto import SentenceType


def test_send_audio_message_sends_stop_on_last_even_if_llm_not_finished(monkeypatch):
    states = []

    async def fake_send_tts_message(conn, state, text=None):
        states.append((state, text))

    async def fake_send_audio(conn, audios):
        return None

    monkeypatch.setattr(sendAudioHandle, "send_tts_message", fake_send_tts_message)
    monkeypatch.setattr(sendAudioHandle, "sendAudio", fake_send_audio)

    conn = SimpleNamespace(
        tts=SimpleNamespace(tts_audio_first_sentence=False),
        llm_finish_task=False,
        client_is_speaking=True,
        close_after_chat=False,
        logger=SimpleNamespace(bind=lambda **kwargs: SimpleNamespace(info=lambda *args, **kwargs: None)),
    )

    asyncio.run(sendAudioHandle.sendAudioMessage(conn, SentenceType.LAST, [], None))

    assert ("stop", None) in states
    assert conn.client_is_speaking is False


def test_send_audio_message_keeps_mqtt_sequences_monotonic_across_segments():
    sent = []

    class _WebSocket:
        async def send(self, payload):
            sent.append(payload)

    conn = SimpleNamespace(
        tts=SimpleNamespace(tts_audio_first_sentence=True),
        client_abort=False,
        client_is_speaking=False,
        close_after_chat=False,
        conn_from_mqtt_gateway=True,
        last_activity_time=0,
        session_id="session-1",
        websocket=_WebSocket(),
        config={},
        clearSpeakStatus=lambda: None,
        logger=SimpleNamespace(
            bind=lambda **kwargs: SimpleNamespace(
                info=lambda *args, **kwargs: None,
                debug=lambda *args, **kwargs: None,
            )
        ),
    )

    asyncio.run(
        sendAudioHandle.sendAudioMessage(conn, SentenceType.FIRST, b"a", "segment one")
    )
    asyncio.run(
        sendAudioHandle.sendAudioMessage(conn, SentenceType.MIDDLE, b"b", None)
    )
    asyncio.run(
        sendAudioHandle.sendAudioMessage(conn, SentenceType.FIRST, b"c", "segment two")
    )
    asyncio.run(
        sendAudioHandle.sendAudioMessage(conn, SentenceType.MIDDLE, b"d", None)
    )

    packets = [payload for payload in sent if isinstance(payload, (bytes, bytearray))]
    sequences = [int.from_bytes(packet[4:8], "big") for packet in packets]

    assert sequences == [0, 1, 2, 3]
