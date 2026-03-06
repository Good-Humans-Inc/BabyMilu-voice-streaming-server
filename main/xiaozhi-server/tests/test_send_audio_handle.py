from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

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


def test_tts_watchdog_forces_stop_when_no_stop_arrives(monkeypatch):
    sent = []

    async def fake_send_audio(conn, audios):
        return None

    monkeypatch.setattr(sendAudioHandle, "sendAudio", fake_send_audio)

    class _Ws:
        async def send(self, payload):
            sent.append(payload)

    conn = SimpleNamespace(
        tts=SimpleNamespace(tts_audio_first_sentence=True),
        llm_finish_task=False,
        client_is_speaking=True,
        close_after_chat=False,
        logger=SimpleNamespace(
            bind=lambda **kwargs: SimpleNamespace(
                info=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            )
        ),
        config={"tts_stop_watchdog_seconds": 0.01},
        websocket=_Ws(),
        session_id="sess-1",
        device_id="dev-1",
        tts_stop_watchdog_task=None,
        tts_stop_watchdog_seq=0,
        clearSpeakStatus=lambda: None,
    )

    async def _run():
        await sendAudioHandle.sendAudioMessage(conn, SentenceType.FIRST, [], "hello")
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert any('"state": "stop"' in payload for payload in sent)
