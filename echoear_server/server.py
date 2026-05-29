from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web
import websockets

from .config import public_summary
from .protocol import SessionState, dumps, error, hello, llm, stt, tts
from .providers import AsrProvider, LlmProvider, TtsProvider, build_providers

LOGGER = logging.getLogger("echoear_server")


class EchoEarServer:
    def __init__(
        self,
        config: dict[str, Any],
        providers: tuple[AsrProvider, LlmProvider, TtsProvider] | None = None,
    ) -> None:
        self.config = config
        self.asr, self.llm, self.tts = providers or build_providers(config)
        self._ws_server = None
        self._http_runner: web.AppRunner | None = None

    async def start(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        server_cfg = self.config.get("server") or {}
        host = server_cfg.get("ip", "0.0.0.0")
        ws_port = int(server_cfg.get("port", 8000))
        http_port = int(server_cfg.get("http_port", 8003))

        self._ws_server = await websockets.serve(
            self._handle_ws,
            host,
            ws_port,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        self._http_runner = await self._start_http(host, http_port)
        LOGGER.info("EchoEar server ready: ws=%s:%s http=%s:%s", host, ws_port, host, http_port)

    async def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._http_runner is not None:
            await self._http_runner.cleanup()
            self._http_runner = None

    async def _start_http(self, host: str, port: int) -> web.AppRunner:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/healthz", self._health)
        app.router.add_post("/debug/turn", self._debug_turn)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        return runner

    async def _index(self, request: web.Request) -> web.Response:
        summary = public_summary(self.config)
        return web.json_response({"service": "echoear-ground-server", **summary})

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **public_summary(self.config)})

    async def _debug_turn(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        text = str(payload.get("text") or "").strip()
        include_tts = bool(payload.get("tts", True))
        if not text:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)
        try:
            response_text = await self.llm.complete(text)
            audio_frames = await self.tts.synthesize_opus(response_text) if include_tts else []
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response(
            {
                "ok": True,
                "transcript": text,
                "reply": response_text,
                "audio_frames": len(audio_frames),
                "audio_bytes": sum(len(frame) for frame in audio_frames),
            }
        )

    async def _handle_ws(self, websocket) -> None:
        session = SessionState()
        LOGGER.info("ws connected session=%s path=%s", session.session_id, self._path_for(websocket))
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    session.append_audio(message)
                    continue
                await self._handle_text(websocket, session, message)
        except websockets.ConnectionClosed:
            LOGGER.info("ws closed session=%s", session.session_id)
        except Exception:
            LOGGER.exception("ws failed session=%s", session.session_id)
            try:
                await websocket.send(dumps(error(session.session_id, "websocket", "internal server error")))
            except Exception:
                pass

    @staticmethod
    def _path_for(websocket) -> str:
        request = getattr(websocket, "request", None)
        return getattr(request, "path", None) or getattr(websocket, "path", "")

    async def _handle_text(self, websocket, session: SessionState, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send(dumps(error(session.session_id, "protocol", "invalid JSON text frame")))
            return

        msg_type = message.get("type")
        if msg_type == "hello":
            audio_params = message.get("audio_params") or {}
            session.audio_format = audio_params.get("format") or session.audio_format
            version = int(message.get("version") or 3)
            await websocket.send(dumps(hello(session.session_id, version=version)))
            return

        if msg_type == "listen":
            await self._handle_listen(websocket, session, message)
            return

        if msg_type == "abort":
            session.audio_frames.clear()
            session.listening = False
            await websocket.send(dumps(tts(session.session_id, "stop")))
            return

        await websocket.send(dumps(error(session.session_id, "protocol", f"unsupported type: {msg_type}")))

    async def _handle_listen(self, websocket, session: SessionState, message: dict[str, Any]) -> None:
        state = message.get("state")
        if state == "start":
            mode = message.get("format")
            session.begin_listen(audio_format=mode)
            LOGGER.info("listen start session=%s", session.session_id)
            return

        if state == "stop":
            frames = session.stop_listen()
            LOGGER.info("listen stop session=%s frames=%s", session.session_id, len(frames))
            if frames:
                await self._process_audio_turn(websocket, session, frames)
            return

        if state == "detect":
            text = str(message.get("text") or "").strip()
            if text:
                await self._process_text_turn(websocket, session, text)
            return

        await websocket.send(dumps(error(session.session_id, "protocol", f"unsupported listen state: {state}")))

    async def _process_audio_turn(self, websocket, session: SessionState, frames: list[bytes]) -> None:
        if session.processing:
            await websocket.send(dumps(error(session.session_id, "turn", "turn already in progress")))
            return
        session.processing = True
        try:
            transcript = await self.asr.transcribe(frames, session.audio_format, session.session_id)
            if not transcript:
                await websocket.send(dumps(error(session.session_id, "asr", "empty transcript")))
                return
            await self._respond_to_transcript(websocket, session, transcript)
        except Exception as exc:
            LOGGER.exception("audio turn failed session=%s", session.session_id)
            await websocket.send(dumps(error(session.session_id, "turn", str(exc))))
            await websocket.send(dumps(tts(session.session_id, "stop")))
        finally:
            session.processing = False

    async def _process_text_turn(self, websocket, session: SessionState, transcript: str) -> None:
        if session.processing:
            await websocket.send(dumps(error(session.session_id, "turn", "turn already in progress")))
            return
        session.processing = True
        try:
            await self._respond_to_transcript(websocket, session, transcript)
        except Exception as exc:
            LOGGER.exception("text turn failed session=%s", session.session_id)
            await websocket.send(dumps(error(session.session_id, "turn", str(exc))))
            await websocket.send(dumps(tts(session.session_id, "stop")))
        finally:
            session.processing = False

    async def _respond_to_transcript(self, websocket, session: SessionState, transcript: str) -> None:
        await websocket.send(dumps(stt(session.session_id, transcript)))
        await websocket.send(dumps(tts(session.session_id, "start")))
        response_text = await self.llm.complete(transcript)
        await websocket.send(dumps(llm(session.session_id, response_text)))
        await websocket.send(dumps(tts(session.session_id, "sentence_start", response_text)))
        frames = await self.tts.synthesize_opus(response_text)
        for packet in frames:
            await websocket.send(packet)
            await asyncio.sleep(0)
        await websocket.send(dumps(tts(session.session_id, "stop")))

