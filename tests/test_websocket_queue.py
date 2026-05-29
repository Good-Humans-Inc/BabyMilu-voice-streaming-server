import asyncio
import json

import pytest
import websockets

from echoear_server.config import load_config
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
    async def complete(self, transcript):
        return "This response should be interrupted before audio plays."


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
