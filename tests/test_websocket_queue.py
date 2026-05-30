import asyncio
import json
import logging
import time

import pytest
import websockets

from echoear_server.config import load_config
from echoear_server.protocol import SessionState
from echoear_server.providers import AsrProvider, LlmProvider, TtsProvider
from echoear_server.server import EchoEarServer


async def _recv_json(ws, timeout=2):
    while True:
        message = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(message, str):
            return json.loads(message)


@pytest.mark.asyncio
async def test_audio_is_not_processed_until_listen_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["ip"] = "127.0.0.1"
    cfg["server"]["port"] = 0
    cfg["server"]["http_port"] = 0
    server = EchoEarServer(cfg)
    await server.start()
    ws_socket = server._ws_server.sockets[0]
    port = ws_socket.getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/") as ws:
            await ws.send(json.dumps({"type": "hello", "version": 3, "audio_params": {"format": "opus"}}))
            hello = await _recv_json(ws)
            assert hello["type"] == "hello"
            assert hello["session_id"]

            await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
            await ws.send(b"frame-1")
            await ws.send(b"frame-2")

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.15)

            await ws.send(json.dumps({"type": "listen", "state": "stop"}))
            stt = await _recv_json(ws)
            assert stt["type"] == "stt"
            assert "2 frames" in stt["text"]
            tts_start = await _recv_json(ws)
            assert tts_start == {"type": "tts", "session_id": hello["session_id"], "state": "start"}
    finally:
        await server.stop()


class FixedAsr(AsrProvider):
    async def transcribe(self, frames, audio_format, session_id):
        return "ignored"


class FixedLlm(LlmProvider):
    async def complete(self, transcript, messages=None):
        return "This response should be interrupted before audio plays."


class CapturingLlm(LlmProvider):
    def __init__(self):
        self.calls = []

    async def complete(self, transcript, messages=None):
        self.calls.append(messages or [])
        return "I remember Jackson's profile."


class SlowTts(TtsProvider):
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def synthesize_opus(self, text):
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return [b"late-audio"]


class MultiFrameTts(TtsProvider):
    async def synthesize_opus(self, text):
        return [b"frame-1", b"frame-2", b"frame-3", b"frame-4"]


class RecordingWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append((time.monotonic(), message))


@pytest.mark.asyncio
async def test_abort_cancels_active_tts_before_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["ip"] = "127.0.0.1"
    cfg["server"]["port"] = 0
    cfg["server"]["http_port"] = 0
    slow_tts = SlowTts()
    server = EchoEarServer(cfg, providers=(FixedAsr(), FixedLlm(), slow_tts))
    await server.start()
    ws_socket = server._ws_server.sockets[0]
    port = ws_socket.getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/") as ws:
            await ws.send(json.dumps({"type": "hello", "version": 3, "audio_params": {"format": "opus"}}))
            hello = await _recv_json(ws)

            await ws.send(json.dumps({"type": "listen", "state": "detect", "text": "interrupt me"}))
            assert (await _recv_json(ws))["type"] == "stt"
            assert (await _recv_json(ws)) == {"type": "tts", "session_id": hello["session_id"], "state": "start"}
            assert (await _recv_json(ws))["type"] == "llm"
            sentence_start = await _recv_json(ws)
            assert sentence_start["type"] == "tts"
            assert sentence_start["state"] == "sentence_start"
            await asyncio.wait_for(slow_tts.started.wait(), timeout=1)

            await ws.send(json.dumps({"type": "abort", "reason": "synthetic_vad_interrupt"}))
            assert await _recv_json(ws) == {"type": "tts", "session_id": hello["session_id"], "state": "stop"}
            await asyncio.wait_for(slow_tts.cancelled.wait(), timeout=1)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.15)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tts_binary_frames_are_paced(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["tts_frame_interval_ms"] = 5
    cfg["server"]["tts_prebuffer_frames"] = 0
    server = EchoEarServer(cfg, providers=(FixedAsr(), FixedLlm(), MultiFrameTts()))
    websocket = RecordingWebSocket()
    session = SessionState()

    await server._respond_to_transcript(websocket, session, "ignored")

    binary_times = [sent_at for sent_at, message in websocket.messages if isinstance(message, bytes)]
    assert len(binary_times) == 4
    assert binary_times[-1] - binary_times[0] >= 0.012


@pytest.mark.asyncio
async def test_tts_prebuffer_sends_initial_frames_without_delay(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["tts_frame_interval_ms"] = 20
    cfg["server"]["tts_prebuffer_frames"] = 3
    server = EchoEarServer(cfg, providers=(FixedAsr(), FixedLlm(), MultiFrameTts()))
    websocket = RecordingWebSocket()
    session = SessionState()

    await server._respond_to_transcript(websocket, session, "ignored")

    binary_times = [sent_at for sent_at, message in websocket.messages if isinstance(message, bytes)]
    assert len(binary_times) == 4
    assert binary_times[2] - binary_times[0] < 0.01
    assert binary_times[3] - binary_times[2] >= 0.015


@pytest.mark.asyncio
async def test_audio_turn_logs_timing_from_listen_stop(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["tts_frame_interval_ms"] = 0
    server = EchoEarServer(cfg, providers=(FixedAsr(), FixedLlm(), MultiFrameTts()))
    websocket = RecordingWebSocket()
    session = SessionState()

    caplog.set_level(logging.INFO, logger="echoear_server")
    await server._process_audio_turn(websocket, session, [b"frame-1", b"frame-2"], time.perf_counter())

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "listen_stop_to_tts_start_ms=" in logs
    assert "listen_stop_to_first_opus_ms=" in logs
    assert "tts_start_to_first_opus_ms=" in logs


@pytest.mark.asyncio
async def test_profile_prompt_and_history_are_sent_to_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    cfg["server"]["tts_frame_interval_ms"] = 0
    llm = CapturingLlm()
    server = EchoEarServer(cfg, providers=(FixedAsr(), llm, MultiFrameTts()))
    websocket = RecordingWebSocket()
    session = SessionState(
        device_id="90:e5:b1:d6:f8:64",
        system_prompt="Supabase profile: User's name: Jackson",
        profile_loaded=True,
    )

    await server._respond_to_transcript(websocket, session, "what do you know about me?")
    await server._respond_to_transcript(websocket, session, "and my device?")

    assert "Jackson" in llm.calls[0][0]["content"]
    assert llm.calls[1][1] == {"role": "user", "content": "what do you know about me?"}
    assert llm.calls[1][2] == {"role": "assistant", "content": "I remember Jackson's profile."}
