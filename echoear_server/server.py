from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import web
import websockets

from .audio import OPUS_FRAME_MS
from .config import public_summary
from .profile_context import (
    DEFAULT_SYSTEM_PROMPT,
    SupabaseProfileStore,
    build_llm_messages,
    build_profile_system_prompt,
    normalize_device_id,
)
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
        self.profile_store = SupabaseProfileStore(config)
        self._ws_server = None
        self._http_runner: web.AppRunner | None = None
        server_cfg = self.config.get("server") or {}
        frame_interval_ms = float(server_cfg.get("tts_frame_interval_ms", OPUS_FRAME_MS))
        self._tts_frame_interval_seconds = max(0.0, frame_interval_ms / 1000.0)

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
        device_id = normalize_device_id(payload.get("device_id") or payload.get("device-id"))
        include_tts = bool(payload.get("tts", True))
        if not text:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)
        try:
            context = await self.profile_store.load_for_device(device_id)
            system_prompt = build_profile_system_prompt(context)
            messages = build_llm_messages(system_prompt, [], text)
            response_text = await self.llm.complete(text, messages=messages)
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
                "device_id": context.device_id,
                "profile_loaded": context.loaded,
                "user_name": context.user_name,
            }
        )

    async def _handle_ws(self, websocket) -> None:
        session = SessionState()
        session.device_id = self._device_id_for(websocket)
        await self._load_profile(session)
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
        finally:
            await self._cancel_turn(session)

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
            supplied_device_id = normalize_device_id(
                message.get("device_id") or message.get("device-id") or message.get("device_mac")
            )
            if supplied_device_id and supplied_device_id != session.device_id:
                session.device_id = supplied_device_id
                session.profile_loaded = False
            await self._load_profile(session)
            version = int(message.get("version") or 3)
            await websocket.send(dumps(hello(session.session_id, version=version)))
            return

        if msg_type == "listen":
            await self._handle_listen(websocket, session, message)
            return

        if msg_type == "abort":
            await self._cancel_turn(session)
            session.stop_listen()
            await websocket.send(dumps(tts(session.session_id, "stop")))
            return

        await websocket.send(dumps(error(session.session_id, "protocol", f"unsupported type: {msg_type}")))

    async def _handle_listen(self, websocket, session: SessionState, message: dict[str, Any]) -> None:
        state = message.get("state")
        if state == "start":
            mode = message.get("format")
            await self._load_profile(session)
            session.begin_listen(audio_format=mode)
            LOGGER.info("listen start session=%s device_id=%s", session.session_id, session.device_id or "-")
            return

        if state == "stop":
            listen_stop_at = time.perf_counter()
            frames = session.stop_listen()
            LOGGER.info("listen stop session=%s frames=%s", session.session_id, len(frames))
            if frames:
                await self._start_turn(
                    websocket,
                    session,
                    self._process_audio_turn(websocket, session, frames, listen_stop_at),
                )
            return

        if state == "detect":
            supplied_device_id = normalize_device_id(message.get("device_id") or message.get("device-id"))
            if supplied_device_id and supplied_device_id != session.device_id:
                session.device_id = supplied_device_id
                session.profile_loaded = False
            await self._load_profile(session)
            text = str(message.get("text") or "").strip()
            if text:
                await self._start_turn(websocket, session, self._process_text_turn(websocket, session, text))
            return

        await websocket.send(dumps(error(session.session_id, "protocol", f"unsupported listen state: {state}")))

    async def _start_turn(self, websocket, session: SessionState, turn) -> None:
        if session.turn_task is not None and not session.turn_task.done():
            turn.close()
            await websocket.send(dumps(error(session.session_id, "turn", "turn already in progress")))
            return

        async def run_turn() -> None:
            session.processing = True
            try:
                await turn
            except asyncio.CancelledError:
                LOGGER.info("turn cancelled session=%s", session.session_id)
                raise
            finally:
                session.processing = False

        session.turn_task = asyncio.create_task(run_turn())

    async def _cancel_turn(self, session: SessionState) -> None:
        task = session.turn_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _process_audio_turn(
        self,
        websocket,
        session: SessionState,
        frames: list[bytes],
        listen_stop_at: float | None = None,
    ) -> None:
        try:
            transcript = await self.asr.transcribe(frames, session.audio_format, session.session_id)
            if not transcript:
                await websocket.send(dumps(error(session.session_id, "asr", "empty transcript")))
                return
            await self._respond_to_transcript(
                websocket,
                session,
                transcript,
                turn_started_at=listen_stop_at,
                input_audio_frames=len(frames),
            )
        except Exception as exc:
            LOGGER.exception("audio turn failed session=%s", session.session_id)
            await websocket.send(dumps(error(session.session_id, "turn", str(exc))))
            await websocket.send(dumps(tts(session.session_id, "stop")))

    async def _process_text_turn(self, websocket, session: SessionState, transcript: str) -> None:
        try:
            await self._respond_to_transcript(websocket, session, transcript)
        except Exception as exc:
            LOGGER.exception("text turn failed session=%s", session.session_id)
            await websocket.send(dumps(error(session.session_id, "turn", str(exc))))
            await websocket.send(dumps(tts(session.session_id, "stop")))

    async def _respond_to_transcript(
        self,
        websocket,
        session: SessionState,
        transcript: str,
        turn_started_at: float | None = None,
        input_audio_frames: int | None = None,
    ) -> None:
        await websocket.send(dumps(stt(session.session_id, transcript)))
        await websocket.send(dumps(tts(session.session_id, "start")))
        tts_start_at = time.perf_counter()
        if turn_started_at is not None:
            LOGGER.info(
                "turn timing session=%s input_frames=%s listen_stop_to_tts_start_ms=%.1f",
                session.session_id,
                input_audio_frames,
                (tts_start_at - turn_started_at) * 1000.0,
            )
        messages = build_llm_messages(session.system_prompt or DEFAULT_SYSTEM_PROMPT, session.history, transcript)
        response_text = await self.llm.complete(transcript, messages=messages)
        session.append_history("user", transcript)
        session.append_history("assistant", response_text)
        await websocket.send(dumps(llm(session.session_id, response_text)))
        await websocket.send(dumps(tts(session.session_id, "sentence_start", response_text)))
        frames = await self.tts.synthesize_opus(response_text)
        bytes_sent = 0
        for index, packet in enumerate(frames):
            await websocket.send(packet)
            bytes_sent += len(packet)
            if index == 0 and turn_started_at is not None:
                first_opus_at = time.perf_counter()
                LOGGER.info(
                    "turn timing session=%s input_frames=%s listen_stop_to_first_opus_ms=%.1f "
                    "tts_start_to_first_opus_ms=%.1f output_frames=%s first_opus_bytes=%s",
                    session.session_id,
                    input_audio_frames,
                    (first_opus_at - turn_started_at) * 1000.0,
                    (first_opus_at - tts_start_at) * 1000.0,
                    len(frames),
                    len(packet),
                )
            if self._tts_frame_interval_seconds > 0 and index + 1 < len(frames):
                await asyncio.sleep(self._tts_frame_interval_seconds)
        LOGGER.info(
            "tts sent session=%s frames=%s bytes=%s interval_ms=%.1f",
            session.session_id,
            len(frames),
            bytes_sent,
            self._tts_frame_interval_seconds * 1000.0,
        )
        await websocket.send(dumps(tts(session.session_id, "stop")))

    def _device_id_for(self, websocket) -> str:
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", {}) or {}
        header_device_id = headers.get("device-id") or headers.get("Device-Id") or headers.get("device_id")
        if header_device_id:
            return normalize_device_id(header_device_id)

        path = self._path_for(websocket)
        query = parse_qs(urlparse(path).query)
        for key in ("device-id", "device_id", "deviceId"):
            if query.get(key):
                return normalize_device_id(query[key][0])

        return normalize_device_id((self.config.get("profile") or {}).get("default_device_id"))

    async def _load_profile(self, session: SessionState) -> None:
        if session.profile_loaded:
            return
        if not session.device_id:
            session.device_id = normalize_device_id((self.config.get("profile") or {}).get("default_device_id"))

        context = await self.profile_store.load_for_device(session.device_id)
        session.device_id = context.device_id or session.device_id
        session.user_id = context.user_id
        session.user_name = context.user_name
        session.system_prompt = build_profile_system_prompt(context)
        session.profile_loaded = True
        LOGGER.info(
            "profile loaded session=%s device_id=%s user_id=%s user_name=%s loaded=%s",
            session.session_id,
            session.device_id or "-",
            session.user_id or "-",
            session.user_name or "-",
            context.loaded,
        )
