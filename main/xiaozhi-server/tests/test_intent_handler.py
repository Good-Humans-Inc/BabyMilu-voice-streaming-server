from __future__ import annotations

import asyncio
import pathlib
import queue
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.handle import intentHandler
from core.providers.tts.dto.dto import SentenceType


class _Logger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


def test_speak_txt_sends_frontend_llm_message(monkeypatch):
    queued = []
    spoken = []
    sent_messages = []

    class _FakeFuture:
        def result(self, timeout=None):
            return None

    class _FakeWebSocket:
        async def send(self, payload):
            sent_messages.append(payload)

    def fake_run_coroutine_threadsafe(coro, loop):
        asyncio.run(coro)
        return _FakeFuture()

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        session_id="session-1",
        loop=object(),
        websocket=_FakeWebSocket(),
        logger=_Logger(),
        tts_MessageText="",
        _session_created=False,
        dialogue=SimpleNamespace(put=lambda message: spoken.append(message.content)),
        tts=SimpleNamespace(
            tts_text_queue=SimpleNamespace(put=lambda item: queued.append(item)),
            tts_one_sentence=lambda conn, content_type, content_detail: spoken.append(content_detail),
        ),
    )

    monkeypatch.setattr(intentHandler.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    intentHandler.speak_txt(conn, "mirror reply")

    assert conn.tts_MessageText == "mirror reply"
    assert [item.sentence_type for item in queued] == [SentenceType.FIRST, SentenceType.LAST]
    assert "mirror reply" in spoken
    assert sent_messages
    assert '"type": "llm"' in sent_messages[0]
    assert '"text": "mirror reply"' in sent_messages[0]


def test_handle_user_intent_queues_bedtime_audio(monkeypatch):
    sent_stt = []
    played = []
    dialogue_messages = []

    async def fake_send_stt_message(conn, text):
        sent_stt.append(text)

    async def fake_send_audio_message(conn, sentence_type, audios, text):
        played.append((sentence_type, audios, text))

    monkeypatch.setattr(intentHandler, "checkWakeupWords", lambda conn, text: asyncio.sleep(0, result=False))
    monkeypatch.setattr(intentHandler, "send_stt_message", fake_send_stt_message)
    monkeypatch.setattr(intentHandler, "sendAudioMessage", fake_send_audio_message)
    monkeypatch.setattr(intentHandler, "audio_to_data", lambda path: [b"bedtime-audio"] * 1205)
    monkeypatch.setattr(intentHandler.os.path, "exists", lambda path: True)

    conn = SimpleNamespace(
        cmd_exit=[],
        intent_type="function_call",
        logger=_Logger(),
        config={"easter_eggs": {"bedtime_sound_path": "config/assets/goodnight.wav"}},
        tts=SimpleNamespace(tts_audio_queue=queue.Queue()),
        dialogue=SimpleNamespace(put=lambda message: dialogue_messages.append(message)),
        mode_specific_instructions="",
        persistent_mode_specific_instructions="",
        client_abort=True,
    )

    handled = asyncio.run(intentHandler.handle_user_intent(conn, "good night"))

    assert handled is True
    assert sent_stt == ["good night"]
    assert played[0][0] == SentenceType.FIRST
    assert len(played[0][1]) == 1000
    assert played[0][1][0] == b"bedtime-audio"
    assert played[1][0] == SentenceType.LAST
    assert conn.mode_specific_instructions == ""
    assert "잘자요아가씨" in conn.persistent_mode_specific_instructions
    assert "ASMRZ" in conn.persistent_mode_specific_instructions
    assert dialogue_messages
    assert "잘자요아가씨" in dialogue_messages[0].content
    assert conn.client_abort is False


def test_handle_user_intent_replies_to_magic_spell(monkeypatch):
    sent_stt = []
    spoken = []

    async def fake_send_stt_message(conn, text):
        sent_stt.append(text)

    async def fake_check_wakeup_words(conn, text):
        return False

    monkeypatch.setattr(intentHandler, "send_stt_message", fake_send_stt_message)
    monkeypatch.setattr(intentHandler, "checkWakeupWords", fake_check_wakeup_words)
    monkeypatch.setattr(intentHandler.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(intentHandler, "speak_txt", lambda conn, text: spoken.append(text))

    conn = SimpleNamespace(
        cmd_exit=[],
        intent_type="function_call",
        logger=_Logger(),
        config={},
        client_abort=True,
    )

    handled = asyncio.run(
        intentHandler.handle_user_intent(
            conn,
            "milu milu on the wall, who' the fairest of them all",
        )
    )

    assert handled is True
    assert sent_stt == ["milu milu on the wall, who' the fairest of them all"]
    assert spoken == ["Hmm... I checked... and it says... me"]
    assert conn.client_abort is False
