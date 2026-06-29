from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType, web

from .audio import opus_frames_to_wav, pcm_to_wav
from .background import BackgroundTask, BackgroundTaskRunner
from .device_mcp import DeviceMCP, parse_json_message
from .models import Turn, VoiceSession, new_session_id
from .prompting import PromptBuilder
from .providers.asr import ASRClient
from .providers.llm import LLMClient
from .providers.tts import TTSClient, TTSRequest
from .stores import JsonlSessionStore, PendingStarterStore
from .timeline import InteractionTimeline, InteractionTimelineStore
from .tools import ServerToolRegistry
from .vad import EnergyVAD, SileroONNXVAD, VoiceActivityDetector


logger = logging.getLogger("babymilu.session")


_EMOJI_TO_EMOTION: dict[str, str] = {
    "\U0001f600": "happy",
    "\U0001f603": "happy",
    "\U0001f604": "happy",
    "\U0001f601": "happy",
    "\U0001f642": "happy",
    "\U0001f60a": "happy",
    "\U0001f607": "happy",
    "\U0001f973": "happy",
    "\U0001f917": "happy",
    "\U0001f60c": "happy",
    "\U0001f60b": "happy",
    "\u263a": "happy",
    "\U0001f606": "laugh",
    "\U0001f923": "laugh",
    "\U0001f602": "laugh",
    "\U0001f61c": "laugh",
    "\U0001f61d": "laugh",
    "\U0001f61b": "laugh",
    "\U0001f970": "heart",
    "\U0001f60d": "heart",
    "\U0001f618": "heart",
    "\U0001faf6": "heart",
    "\u2764": "heart",
    "\u2665": "heart",
    "\U0001f9e1": "heart",
    "\U0001f49b": "heart",
    "\U0001f49a": "heart",
    "\U0001f499": "heart",
    "\U0001f49c": "heart",
    "\U0001f90e": "heart",
    "\U0001f5a4": "heart",
    "\U0001f90d": "heart",
    "\U0001f496": "heart",
    "\U0001f497": "heart",
    "\U0001f495": "heart",
    "\U0001f493": "heart",
    "\U0001f49e": "heart",
    "\U0001f498": "heart",
    "\U0001f49d": "heart",
    "\U0001f49f": "heart",
    "\U0001f494": "sad",
    "\U0001f61e": "sad",
    "\U0001f614": "sad",
    "\U0001f641": "sad",
    "\u2639": "sad",
    "\U0001f622": "sad",
    "\U0001f625": "sad",
    "\U0001f630": "sad",
    "\U0001f62d": "cry",
    "\U0001f621": "angry",
    "\U0001f620": "angry",
    "\U0001f92c": "angry",
    "\U0001f624": "angry",
    "\U0001f4a2": "angry",
    "\U0001f525": "angry",
    "\U0001f633": "blush",
    "\U0001f97a": "blush",
    "\U0001f605": "blush",
    "\U0001f648": "blush",
    "\U0001f929": "starry",
    "\u2728": "starry",
    "\u2b50": "starry",
    "\U0001f31f": "starry",
    "\U0001f4ab": "starry",
    "\U0001f914": "starry",
    "\U0001f62e": "starry",
    "\U0001f632": "starry",
    "\U0001f631": "starry",
    "\U0001f609": "smirk",
    "\U0001f60f": "smirk",
    "\U0001f60e": "smirk",
    "\U0001f643": "smirk",
    "\U0001f634": "sleep",
    "\U0001f971": "sleep",
    "\U0001f4a4": "sleep",
    "\U0001f610": "normal",
    "\U0001f611": "normal",
    "\U0001f636": "normal",
}


def _emotion_from_text(text: str) -> str | None:
    for char in text:
        emotion = _EMOJI_TO_EMOTION.get(char)
        if emotion:
            return emotion
    return None


@dataclass
class TTSAudioBatch:
    text: str
    opus_frames: list[bytes]
    sentence_start: bool = False
    sentence_end: bool = False


class VoiceSessionHandler:
    def __init__(
        self,
        *,
        starter_store: PendingStarterStore,
        session_store: JsonlSessionStore,
        prompt_builder: PromptBuilder,
        asr: ASRClient,
        llm: LLMClient,
        tts: TTSClient,
        tools: ServerToolRegistry,
        timeline_store: InteractionTimelineStore | None = None,
        background_runner: BackgroundTaskRunner | None = None,
        auto_capture_seconds: float = 3.0,
        auto_resume_listening_after_tts: bool = True,
        allow_barge_in: bool = False,
        tts_frame_pacing_ms: int = 60,
        vad_provider: str = "silero_onnx",
        vad_energy_threshold: int = 260,
        silero_model_path: str = "models/silero_vad_16k_op15.onnx",
        silero_threshold: float = 0.5,
        silero_negative_threshold: float = 0.2,
        silero_min_speech_rms: int = 500,
        vad_start_ms: int = 120,
        vad_end_silence_ms: int = 720,
        vad_max_capture_seconds: float = 8.0,
        tts_text_queue_size: int = 8,
        tts_audio_queue_size: int = 12,
        tts_prebuffer_frames: int = 3,
    ) -> None:
        self.starter_store = starter_store
        self.session_store = session_store
        self.prompt_builder = prompt_builder
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.tools = tools
        self.timeline_store = timeline_store or InteractionTimelineStore("data/babymilu_interaction_events.jsonl")
        self.background_runner = background_runner or BackgroundTaskRunner()
        self.auto_capture_seconds = auto_capture_seconds
        self.auto_resume_listening_after_tts = auto_resume_listening_after_tts
        self.allow_barge_in = allow_barge_in
        self.tts_frame_pacing_ms = tts_frame_pacing_ms
        self.vad_provider = vad_provider
        self.vad_energy_threshold = vad_energy_threshold
        self.silero_model_path = silero_model_path
        self.silero_threshold = silero_threshold
        self.silero_negative_threshold = silero_negative_threshold
        self.silero_min_speech_rms = silero_min_speech_rms
        self.vad_start_ms = vad_start_ms
        self.vad_end_silence_ms = vad_end_silence_ms
        self.vad_max_capture_seconds = vad_max_capture_seconds
        self.tts_text_queue_size = tts_text_queue_size
        self.tts_audio_queue_size = tts_audio_queue_size
        self.tts_prebuffer_frames = tts_prebuffer_frames

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)

        device_id = request.headers.get("device-id") or request.query.get("device-id") or request.query.get("device_id") or "unknown-device"
        mode = request.query.get("mode") or "normal"
        session = VoiceSession(session_id=new_session_id(), device_id=device_id, mode=mode)
        timeline = InteractionTimeline(store=self.timeline_store, session_id=session.session_id, device_id=device_id)
        send_lock = asyncio.Lock()
        device_mcp: DeviceMCP | None = None
        system_prompt = ""
        audio_frames: list[bytes] = []
        audio_chunk_frames = 0
        audio_chunk_bytes = 0
        audio_format = "opus"
        audio_sample_rate = 16000
        listen_active = False
        processing_audio = False
        audio_capture_task: asyncio.Task[None] | None = None
        response_task: asyncio.Task[None] | None = None
        vad: VoiceActivityDetector | None = None
        timeline.emit(
            "ws.connect",
            actor="device",
            direction="in",
            payload={"mode": mode, "remote": request.remote or ""},
        )
        logger.info("ws_connect session_id=%s device_id=%s mode=%s remote=%s", session.session_id, device_id, mode, request.remote)

        def cancel_capture_timer() -> None:
            nonlocal audio_capture_task
            if audio_capture_task and not audio_capture_task.done():
                audio_capture_task.cancel()
            audio_capture_task = None

        async def process_audio_buffer(reason: str) -> None:
            nonlocal audio_frames, processing_audio, response_task
            if processing_audio:
                logger.info(
                    "audio_stop_skip session_id=%s device_id=%s reason=%s processing_audio=true buffered_frames=%s",
                    session.session_id,
                    session.device_id,
                    reason,
                    len(audio_frames),
                )
                return
            if not audio_frames:
                return
            processing_audio = True
            frames = audio_frames
            audio_frames = []
            try:
                if not ws.closed:
                    await ws.send_json({"type": "listen", "state": "stop"})
                timeline.emit(
                    "listen.stop",
                    payload={"reason": reason, "frames": len(frames), "audioFormat": audio_format, "sampleRate": audio_sample_rate},
                )
                logger.info(
                    "audio_stop session_id=%s device_id=%s reason=%s frames=%s format=%s sample_rate=%s",
                    session.session_id,
                    session.device_id,
                    reason,
                    len(frames),
                    audio_format,
                    audio_sample_rate,
                )
                response_task = asyncio.create_task(
                    self._handle_audio_stop(
                        ws,
                        session,
                        system_prompt,
                        frames,
                        audio_format,
                        audio_sample_rate,
                        device_mcp,
                        timeline,
                        send_lock,
                    )
                )
                await response_task
            except asyncio.CancelledError:
                logger.info("response_cancelled session_id=%s device_id=%s", session.session_id, session.device_id)
                raise
            finally:
                response_task = None
                processing_audio = False

        async def cancel_response(reason: str) -> None:
            nonlocal processing_audio, response_task
            if response_task and not response_task.done():
                logger.info("response_cancel session_id=%s device_id=%s reason=%s", session.session_id, session.device_id, reason)
                response_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await response_task
            response_task = None
            processing_audio = False

        def new_vad() -> VoiceActivityDetector | None:
            provider = self.vad_provider.lower().strip()
            if provider in {"silero", "silero_onnx"}:
                try:
                    return SileroONNXVAD(
                        model_path=self.silero_model_path,
                        audio_format=audio_format,
                        sample_rate=audio_sample_rate,
                        frame_duration_ms=60,
                        threshold=self.silero_threshold,
                        negative_threshold=self.silero_negative_threshold,
                        min_speech_rms=self.silero_min_speech_rms,
                        speech_start_ms=self.vad_start_ms,
                        speech_end_silence_ms=self.vad_end_silence_ms,
                        max_capture_seconds=self.vad_max_capture_seconds,
                    )
                except Exception:
                    logger.exception(
                        "vad_init_failed provider=%s model_path=%s fallback=energy",
                        provider,
                        self.silero_model_path,
                    )
            if provider not in {"energy", "silero", "silero_onnx"}:
                return None
            return EnergyVAD(
                audio_format=audio_format,
                sample_rate=audio_sample_rate,
                frame_duration_ms=60,
                energy_threshold=self.vad_energy_threshold,
                speech_start_ms=self.vad_start_ms,
                speech_end_silence_ms=self.vad_end_silence_ms,
                max_capture_seconds=self.vad_max_capture_seconds,
            )

        async def auto_capture_timeout() -> None:
            nonlocal audio_capture_task
            try:
                await asyncio.sleep(self.auto_capture_seconds)
                audio_capture_task = None
                await process_audio_buffer("auto_capture_timeout")
            except asyncio.CancelledError:
                return

        def start_capture_timer() -> None:
            nonlocal audio_capture_task
            if self.auto_capture_seconds <= 0:
                return
            if audio_capture_task is None or audio_capture_task.done():
                audio_capture_task = asyncio.create_task(auto_capture_timeout())

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = parse_json_message(msg.data)
                    msg_type = data.get("type")
                    if msg_type == "hello":
                        device_id = data.get("device_id") or data.get("deviceId") or device_id
                        mode = _extract_mode(data)
                        timeline.set_device_id(device_id)
                        consume_result = self.starter_store.consume_with_outcome(device_id, mode)
                        starter = consume_result.starter
                        session.device_id = device_id
                        session.mode = mode
                        session.starter = starter
                        timeline.emit(
                            "hello",
                            actor="device",
                            direction="in",
                            payload={
                                "mode": mode,
                                "hasMcp": bool((data.get("features") or {}).get("mcp")),
                                "audioParams": data.get("audio_params") or {},
                                "starterOutcome": consume_result.outcome,
                            },
                        )
                        if consume_result.outcome == "expired" and starter:
                            timeline.emit(
                                "starter.expired",
                                payload={
                                    "starterEventId": starter.starter_event_id,
                                    "mode": starter.mode,
                                    "requestedMode": mode,
                                    "sourceType": starter.source_type,
                                    "sourceId": starter.source_id,
                                },
                            )
                            starter = None
                            session.starter = None
                        elif consume_result.outcome == "mode_mismatch" and starter:
                            timeline.emit(
                                "starter.mode_mismatch",
                                payload={
                                    "starterEventId": starter.starter_event_id,
                                    "mode": starter.mode,
                                    "requestedMode": mode,
                                    "sourceType": starter.source_type,
                                    "sourceId": starter.source_id,
                                },
                            )
                            starter = None
                            session.starter = None
                        elif consume_result.outcome == "consumed" and starter:
                            timeline.emit(
                                "starter.consume",
                                payload={
                                    "starterEventId": starter.starter_event_id,
                                    "mode": starter.mode,
                                    "sourceType": starter.source_type,
                                    "sourceId": starter.source_id,
                                },
                            )
                        logger.info(
                            "ws_hello session_id=%s device_id=%s mode=%s starter=%s mcp=%s",
                            session.session_id,
                            device_id,
                            mode,
                            bool(starter),
                            bool((data.get("features") or {}).get("mcp")),
                        )
                        audio_params = data.get("audio_params") or {}
                        if isinstance(audio_params, dict) and audio_params.get("format"):
                            audio_format = str(audio_params.get("format")).lower()
                        if isinstance(audio_params, dict) and audio_params.get("sample_rate"):
                            audio_sample_rate = int(audio_params.get("sample_rate") or 16000)
                        system_prompt = self.prompt_builder.build_system_prompt(
                            device_id=device_id,
                            mode=mode,
                            starter=starter,
                        )
                        await ws.send_json(
                            {
                                "type": "hello",
                                "transport": "websocket",
                                "session_id": session.session_id,
                                "audio_params": {
                                    "format": "opus",
                                    "sample_rate": audio_sample_rate,
                                    "channels": 1,
                                    "frame_duration": 60,
                                },
                            }
                        )
                        logger.info(
                            "ws_hello_ack session_id=%s device_id=%s mode=%s audio_format=%s sample_rate=%s",
                            session.session_id,
                            device_id,
                            mode,
                            audio_format,
                            audio_sample_rate,
                        )
                        if (data.get("features") or {}).get("mcp"):
                            device_mcp = DeviceMCP(ws)
                            await device_mcp.initialize()
                        if starter:
                            logger.info(
                                "starter_send session_id=%s device_id=%s mode=%s source_type=%s source_id=%s",
                                session.session_id,
                                device_id,
                                mode,
                                starter.source_type,
                                starter.source_id,
                            )
                            timeline.emit(
                                "starter.delivered",
                                payload={
                                    "starterEventId": starter.starter_event_id,
                                    "mode": starter.mode,
                                    "sourceType": starter.source_type,
                                    "sourceId": starter.source_id,
                                },
                            )
                            await self._send_assistant_text(ws, session, starter.text, "starter", timeline=timeline, send_lock=send_lock)
                            timeline.emit(
                                "starter.acknowledged",
                                actor="device",
                                direction="in",
                                payload={"starterEventId": starter.starter_event_id, "mode": starter.mode},
                            )
                        continue
                    if msg_type == "mcp" and device_mcp:
                        payload = data.get("payload") or {}
                        if isinstance(payload, dict):
                            await device_mcp.handle_payload(payload)
                        continue
                    if msg_type in {"text", "listen"}:
                        text = data.get("text") or data.get("content") or ""
                        if text:
                            logger.info("user_text session_id=%s device_id=%s chars=%s", session.session_id, session.device_id, len(text))
                            await self._handle_user_text(ws, session, system_prompt, text, device_mcp, timeline=timeline, send_lock=send_lock)
                            continue
                        state = str(data.get("state") or "").lower()
                        if msg_type == "listen" and state == "start":
                            if response_task and not response_task.done():
                                timeline.emit(
                                    "interrupt",
                                    actor="device",
                                    direction="in",
                                    payload={
                                        "reason": "new_listen_start",
                                        "policy": "cancel" if self.allow_barge_in else "ignored",
                                    },
                                )
                                if self.allow_barge_in:
                                    await cancel_response("new_listen_start")
                                else:
                                    await ws.send_json({"type": "listen", "state": "stop"})
                                    continue
                            if processing_audio:
                                logger.info(
                                    "listen_start_ignored session_id=%s device_id=%s reason=processing_audio",
                                    session.session_id,
                                    session.device_id,
                                )
                                await ws.send_json({"type": "listen", "state": "stop"})
                                continue
                            listen_active = True
                            audio_frames = []
                            audio_chunk_frames = 0
                            audio_chunk_bytes = 0
                            vad = new_vad()
                            cancel_capture_timer()
                            if vad is None:
                                start_capture_timer()
                            timeline.emit(
                                "listen.start",
                                actor="device",
                                direction="in",
                                payload={"mode": str(data.get("mode") or ""), "vad": self.vad_provider},
                            )
                            logger.info(
                                "listen_start session_id=%s device_id=%s mode=%s vad=%s auto_capture_seconds=%.2f",
                                session.session_id,
                                session.device_id,
                                str(data.get("mode") or ""),
                                self.vad_provider,
                                self.auto_capture_seconds,
                            )
                        if msg_type == "listen" and state in {"stop", "end", "finish"}:
                            listen_active = False
                            if not processing_audio:
                                cancel_capture_timer()
                            if not audio_frames:
                                timeline.emit("listen.stop", actor="device", direction="in", payload={"reason": "client_empty_stop"})
                            await process_audio_buffer("client_listen_stop")
                        continue
                elif msg.type == WSMsgType.BINARY:
                    if session.metadata.get("speaking"):
                        timeline.emit(
                            "interrupt",
                            actor="device",
                            direction="in",
                            payload={
                                "bytes": len(msg.data),
                                "policy": "cancel" if self.allow_barge_in else "ignored",
                            },
                        )
                        if self.allow_barge_in and response_task and not response_task.done():
                            await cancel_response("barge_in_audio")
                        else:
                            continue
                    if processing_audio or not listen_active:
                        continue
                    frame = bytes(msg.data)
                    audio_frames.append(frame)
                    audio_chunk_frames += 1
                    audio_chunk_bytes += len(frame)
                    if audio_chunk_frames >= 4:
                        timeline.emit(
                            "audio.chunk_200ms",
                            actor="device",
                            direction="in",
                            payload={"frames": audio_chunk_frames, "bytes": audio_chunk_bytes},
                        )
                        audio_chunk_frames = 0
                        audio_chunk_bytes = 0
                    if listen_active and vad is None:
                        start_capture_timer()
                    if listen_active and vad is not None:
                        try:
                            decision = vad.accept_frame(frame)
                            if decision.speech_started:
                                timeline.emit(
                                    "vad.speech_start",
                                    payload={"elapsedMs": decision.elapsed_ms, "rms": decision.rms},
                                )
                                logger.info(
                                    "vad_speech_start session_id=%s device_id=%s elapsed_ms=%s rms=%s",
                                    session.session_id,
                                    session.device_id,
                                    decision.elapsed_ms,
                                    decision.rms,
                                )
                            if decision.speech_ended:
                                listen_active = False
                                cancel_capture_timer()
                                timeline.emit(
                                    "vad.speech_end",
                                    payload={
                                        "elapsedMs": decision.elapsed_ms,
                                        "silenceMs": decision.silence_ms,
                                        "rms": decision.rms,
                                    },
                                )
                                logger.info(
                                    "vad_speech_end session_id=%s device_id=%s elapsed_ms=%s silence_ms=%s rms=%s",
                                    session.session_id,
                                    session.device_id,
                                    decision.elapsed_ms,
                                    decision.silence_ms,
                                    decision.rms,
                                )
                                await process_audio_buffer("vad_speech_end")
                        except Exception:
                            logger.exception("vad_failed session_id=%s device_id=%s", session.session_id, session.device_id)
                    if len(audio_frames) <= 5 or len(audio_frames) % 50 == 0:
                        logger.info(
                            "audio_frame session_id=%s device_id=%s frames=%s bytes=%s",
                            session.session_id,
                            session.device_id,
                            len(audio_frames),
                            len(msg.data),
                        )
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("ws_error session_id=%s device_id=%s error=%s", session.session_id, session.device_id, ws.exception())
                    break
        finally:
            cancel_capture_timer()
            if response_task and not response_task.done():
                response_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await response_task
            if audio_capture_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await audio_capture_task
            timeline.emit("ws.disconnect", payload={"turns": len(session.turns), "starter": bool(session.starter)})
            session.metadata["timelinePath"] = timeline.path
            session.metadata["eventCount"] = timeline.event_count
            self.session_store.save(session)
            logger.info(
                "ws_disconnect session_id=%s device_id=%s turns=%s starter=%s",
                session.session_id,
                session.device_id,
                len(session.turns),
                bool(session.starter),
            )
        return ws

    async def _handle_audio_stop(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        system_prompt: str,
        frames: list[bytes],
        audio_format: str,
        audio_sample_rate: int,
        device_mcp: DeviceMCP | None,
        timeline: InteractionTimeline,
        send_lock: asyncio.Lock,
    ) -> None:
        turn_started_at = time.perf_counter()
        try:
            logger.info(
                "audio_decode_start session_id=%s device_id=%s frames=%s bytes=%s format=%s sample_rate=%s",
                session.session_id,
                session.device_id,
                len(frames),
                sum(len(frame) for frame in frames),
                audio_format,
                audio_sample_rate,
            )
            if audio_format == "pcm":
                wav_audio = pcm_to_wav(b"".join(frames), sample_rate=audio_sample_rate)
            else:
                wav_audio = opus_frames_to_wav(frames, sample_rate=audio_sample_rate)
            logger.info(
                "audio_decode_done session_id=%s device_id=%s wav_bytes=%s",
                session.session_id,
                session.device_id,
                len(wav_audio),
            )
            logger.info("asr_start session_id=%s device_id=%s", session.session_id, session.device_id)
            timeline.emit(
                "asr.start",
                payload={"frames": len(frames), "audioFormat": audio_format, "sampleRate": audio_sample_rate},
            )
            result = await self.asr.transcribe(wav_audio)
        except AttributeError:
            logger.exception("asr_failed session_id=%s device_id=%s reason=not_configured", session.session_id, session.device_id)
            timeline.emit("asr.error", payload={"error": "not_configured"})
            await ws.send_json({"type": "asr_error", "error": "ASR client is not configured"})
            await self._resume_listening_if_enabled(ws, session, reason="asr_not_configured", timeline=timeline)
            return
        except Exception as exc:
            logger.exception("asr_failed session_id=%s device_id=%s", session.session_id, session.device_id)
            timeline.emit("asr.error", payload={"error": str(exc)})
            await ws.send_json({"type": "asr_error", "error": str(exc)})
            await self._resume_listening_if_enabled(ws, session, reason="asr_error", timeline=timeline)
            return
        if result.text:
            timeline.emit("asr.final", payload={"textChars": len(result.text)})
            logger.info(
                "latency_asr_done session_id=%s device_id=%s elapsed_ms=%.1f chars=%s",
                session.session_id,
                session.device_id,
                (time.perf_counter() - turn_started_at) * 1000,
                len(result.text),
            )
            await ws.send_json({"type": "stt", "text": result.text})
            await self._handle_user_text(
                ws,
                session,
                system_prompt,
                result.text,
                device_mcp,
                turn_started_at=turn_started_at,
                timeline=timeline,
                send_lock=send_lock,
            )
        else:
            logger.info("asr_empty session_id=%s device_id=%s", session.session_id, session.device_id)
            timeline.emit("asr.empty")
            await self._resume_listening_if_enabled(ws, session, reason="asr_empty", timeline=timeline)

    async def _handle_user_text(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        system_prompt: str,
        text: str,
        device_mcp: DeviceMCP | None,
        turn_started_at: float | None = None,
        timeline: InteractionTimeline | None = None,
        send_lock: asyncio.Lock | None = None,
    ) -> None:
        turn_started_at = turn_started_at or time.perf_counter()
        session.turns.append(Turn(role="user", text=text))
        if timeline:
            timeline.emit("user.text", actor="user", direction="in", payload={"textChars": len(text)})
        tool_descriptions = self.tools.descriptions()
        if device_mcp:
            tool_descriptions.extend(device_mcp.tool_descriptions())
        messages: list[dict[str, Any]] = [{"role": turn.role, "content": turn.text} for turn in session.turns if turn.role in {"user", "assistant"}]
        response_text = ""
        for _ in range(3):
            response_text, tool_calls = await self._stream_llm_to_tts(
                ws,
                session,
                system_prompt,
                messages,
                tool_descriptions,
                turn_started_at=turn_started_at,
                timeline=timeline,
            )
            if not tool_calls:
                break
            assistant_tool_calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in tool_calls
            ]
            messages.append({"role": "assistant", "content": response_text or "", "tool_calls": assistant_tool_calls})
            background_calls = [call for call in tool_calls if self._is_background_tool(call.name)]
            if background_calls:
                followup_gate = asyncio.Event()
                for call in background_calls:
                    logger.info("background_tool_call session_id=%s device_id=%s tool=%s", session.session_id, session.device_id, call.name)
                    await self._schedule_background_tool(
                        ws,
                        session,
                        call,
                        device_mcp,
                        timeline=timeline,
                        send_lock=send_lock,
                        followup_gate=followup_gate,
                    )
                if response_text:
                    session.turns.append(Turn(role="assistant", text=response_text, turn_type="conversation"))
                await self._send_assistant_text(
                    ws,
                    session,
                    self._background_ack_text([call.name for call in background_calls]),
                    "tool_ack",
                    timeline=timeline,
                    send_lock=send_lock,
                )
                followup_gate.set()
                return
            for call in tool_calls:
                logger.info("tool_call session_id=%s device_id=%s tool=%s", session.session_id, session.device_id, call.name)
                result = await self._call_tool(call.name, call.arguments, device_mcp, timeline=timeline)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result),
                    }
                )
        if response_text:
            session.turns.append(Turn(role="assistant", text=response_text, turn_type="conversation"))

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        device_mcp: DeviceMCP | None,
        *,
        timeline: InteractionTimeline | None = None,
    ) -> dict[str, Any]:
        if timeline:
            timeline.emit("tool.start", actor="tool", payload={"tool": name, "background": False})
        try:
            if self.tools.has(name):
                result = await self.tools.call(name, arguments)
            elif device_mcp and name in device_mcp.tools:
                device_result = await device_mcp.call_tool(name, arguments)
                result = {"ok": True, "type": "device_mcp", "tool": name, "result": device_result}
            else:
                result = {"ok": False, "error": f"tool_not_found: {name}"}
            if timeline:
                timeline.emit("tool.result", actor="tool", payload={"tool": name, "ok": bool(result.get("ok")), "background": False})
            return result
        except Exception as exc:
            if timeline:
                timeline.emit("tool.error", actor="tool", payload={"tool": name, "error": str(exc), "background": False})
            raise

    def _is_background_tool(self, name: str) -> bool:
        return self.tools.has(name) and self.tools.is_background(name)

    def _background_ack_text(self, tool_names: list[str]) -> str:
        if any(name == "get_weather" for name in tool_names):
            return "I'm checking that now."
        if any(name in {"set_alarm", "set_reminder"} for name in tool_names):
            return "I'll set that up now."
        return "I'm working on that now."

    async def _schedule_background_tool(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        call: Any,
        device_mcp: DeviceMCP | None,
        *,
        timeline: InteractionTimeline | None,
        send_lock: asyncio.Lock | None,
        followup_gate: asyncio.Event | None = None,
    ) -> BackgroundTask:
        async def run_tool() -> dict[str, Any]:
            if timeline:
                timeline.emit("tool.start", actor="tool", payload={"tool": call.name, "background": True})
            return await self._call_tool(call.name, call.arguments, device_mcp, timeline=None)

        async def on_complete(task: BackgroundTask) -> None:
            payload = {
                "taskId": task.task_id,
                "tool": call.name,
                "status": task.status,
                "ok": bool(task.result and task.result.get("ok")),
            }
            if task.status == "completed":
                if timeline:
                    timeline.emit("tool.result", actor="tool", payload=payload | {"background": True})
            else:
                if timeline:
                    timeline.emit("tool.error", actor="tool", payload=payload | {"error": task.error, "background": True})
            if ws.closed:
                session.metadata.setdefault("backgroundResults", []).append(task.to_json())
                return
            if followup_gate:
                await followup_gate.wait()
            text = self._background_result_text(call.name, task)
            await self._send_assistant_text(ws, session, text, "tool_result", timeline=timeline, send_lock=send_lock)

        return await self.background_runner.submit(
            task_type=f"tool:{call.name}",
            handler=run_tool,
            on_complete=on_complete,
            metadata={"sessionId": session.session_id, "deviceId": session.device_id, "tool": call.name},
        )

    def _background_result_text(self, name: str, task: BackgroundTask) -> str:
        if task.status != "completed" or not task.result:
            return "I couldn't finish that check just now."
        result = task.result
        if name == "get_weather" and result.get("ok"):
            location = result.get("location") or "there"
            temp = result.get("temperatureF")
            if temp is not None:
                return f"It's about {round(float(temp))} degrees in {location} right now."
            return f"I found the weather for {location}."
        if name == "set_reminder" and result.get("ok"):
            return "Your reminder is set."
        if name == "set_alarm" and result.get("ok"):
            return "Your alarm is set."
        if name == "magic_camera" and result.get("ok"):
            return "I queued that Magic Camera idea."
        return "I finished that."

    async def _stream_llm_to_tts(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        turn_started_at: float,
        timeline: InteractionTimeline | None = None,
    ) -> tuple[str, list[Any]]:
        text_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.tts_text_queue_size)
        audio_queue: asyncio.Queue[TTSAudioBatch | None] = asyncio.Queue(maxsize=self.tts_audio_queue_size)
        segmenter = StreamingSentenceSegmenter()
        response_parts: list[str] = []
        tool_calls: list[Any] = []
        saw_first_token = False
        queued_first_tts_text = False
        last_emotion_sent: str | None = None

        tts_task = asyncio.create_task(self._tts_worker(session, text_queue, audio_queue, turn_started_at=turn_started_at, timeline=timeline))
        sender_task = asyncio.create_task(self._audio_sender(ws, session, audio_queue, turn_started_at=turn_started_at, timeline=timeline))
        try:
            async for event in self.llm.stream_complete(system_prompt=system_prompt, messages=messages, tools=tools):
                if ws.closed:
                    break
                if event.kind == "content" and event.content:
                    if not saw_first_token:
                        saw_first_token = True
                        if timeline:
                            timeline.emit("llm.first_token", payload={"elapsedMs": int((time.perf_counter() - turn_started_at) * 1000)})
                        logger.info(
                            "latency_llm_first_token session_id=%s device_id=%s elapsed_ms=%.1f",
                            session.session_id,
                            session.device_id,
                            (time.perf_counter() - turn_started_at) * 1000,
                        )
                    response_parts.append(event.content)
                    if timeline:
                        timeline.emit("llm.text_chunk", payload={"chars": len(event.content)})
                    for chunk in segmenter.push(event.content):
                        last_emotion_sent = await self._send_emotion_for_text(
                            ws,
                            session,
                            chunk,
                            timeline=timeline,
                            last_emotion=last_emotion_sent,
                        )
                        await ws.send_json({"type": "text", "role": "assistant", "turn_type": "conversation", "text": chunk})
                        await text_queue.put(chunk)
                        if not queued_first_tts_text:
                            queued_first_tts_text = True
                            logger.info(
                                "latency_tts_first_text session_id=%s device_id=%s elapsed_ms=%.1f chars=%s",
                                session.session_id,
                                session.device_id,
                                (time.perf_counter() - turn_started_at) * 1000,
                                len(chunk),
                            )
                elif event.kind == "done":
                    tool_calls = event.tool_calls or []
            for chunk in segmenter.flush():
                if ws.closed:
                    break
                last_emotion_sent = await self._send_emotion_for_text(
                    ws,
                    session,
                    chunk,
                    timeline=timeline,
                    last_emotion=last_emotion_sent,
                )
                await ws.send_json({"type": "text", "role": "assistant", "turn_type": "conversation", "text": chunk})
                await text_queue.put(chunk)
                if not queued_first_tts_text:
                    queued_first_tts_text = True
                    logger.info(
                        "latency_tts_first_text session_id=%s device_id=%s elapsed_ms=%.1f chars=%s",
                        session.session_id,
                        session.device_id,
                        (time.perf_counter() - turn_started_at) * 1000,
                        len(chunk),
                    )
            await text_queue.put(None)
            await tts_task
            await audio_queue.put(None)
            await sender_task
        except asyncio.CancelledError:
            tts_task.cancel()
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tts_task
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            raise
        except Exception:
            tts_task.cancel()
            sender_task.cancel()
            raise
        return "".join(response_parts).strip(), tool_calls

    async def _tts_worker(
        self,
        session: VoiceSession,
        text_queue: asyncio.Queue[str | None],
        audio_queue: asyncio.Queue[TTSAudioBatch | None],
        *,
        turn_started_at: float,
        timeline: InteractionTimeline | None = None,
    ) -> None:
        logged_first_audio = False
        while True:
            chunk = await text_queue.get()
            if chunk is None:
                return
            opus_frames: list[bytes] = []
            total_opus_frames = 0
            sentence_started = False
            async for frame in self.tts.stream_opus_frames(TTSRequest(text=chunk)):
                opus_frames.append(frame)
                total_opus_frames += 1
                if len(opus_frames) < self.tts_prebuffer_frames:
                    continue
                if not logged_first_audio:
                    logged_first_audio = True
                    if timeline:
                        timeline.emit("tts.first_audio", payload={"textChars": len(chunk), "bufferedFrames": len(opus_frames)})
                    logger.info(
                        "latency_tts_first_audio session_id=%s device_id=%s elapsed_ms=%.1f chars=%s buffered_frames=%s",
                        session.session_id,
                        session.device_id,
                        (time.perf_counter() - turn_started_at) * 1000,
                        len(chunk),
                        len(opus_frames),
                    )
                await audio_queue.put(
                    TTSAudioBatch(
                        text=chunk,
                        opus_frames=opus_frames,
                        sentence_start=not sentence_started,
                    )
                )
                sentence_started = True
                opus_frames = []
            if not logged_first_audio:
                logged_first_audio = True
                if timeline:
                    timeline.emit("tts.first_audio", payload={"textChars": len(chunk), "bufferedFrames": len(opus_frames)})
                logger.info(
                    "latency_tts_first_audio session_id=%s device_id=%s elapsed_ms=%.1f chars=%s buffered_frames=%s",
                    session.session_id,
                    session.device_id,
                    (time.perf_counter() - turn_started_at) * 1000,
                    len(chunk),
                    len(opus_frames),
                )
            logger.info(
                "tts_audio session_id=%s device_id=%s chars=%s opus_frames=%s",
                session.session_id,
                session.device_id,
                len(chunk),
                total_opus_frames,
            )
            await audio_queue.put(
                TTSAudioBatch(
                    text=chunk,
                    opus_frames=opus_frames,
                    sentence_start=not sentence_started,
                    sentence_end=True,
                )
            )

    async def _audio_sender(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        audio_queue: asyncio.Queue[TTSAudioBatch | None],
        *,
        turn_started_at: float | None = None,
        timeline: InteractionTimeline | None = None,
    ) -> None:
        started = False
        logged_first_opus = False
        sentence_frame_count = 0
        while True:
            item = await audio_queue.get()
            if item is None:
                session.metadata["speaking"] = False
                if started and not ws.closed:
                    await ws.send_json({"type": "tts", "state": "stop"})
                    if timeline:
                        timeline.emit("tts.stop")
                    await self._resume_listening_if_enabled(ws, session, reason="tts_stop", timeline=timeline)
                return
            if ws.closed:
                session.metadata["speaking"] = False
                return
            if not started:
                await ws.send_json({"type": "tts", "state": "start"})
                session.metadata["speaking"] = True
                started = True
            if item.sentence_start:
                sentence_frame_count = 0
                await ws.send_json({"type": "tts", "state": "sentence_start", "text": item.text})
            for frame in item.opus_frames:
                if ws.closed:
                    session.metadata["speaking"] = False
                    return
                await ws.send_bytes(frame)
                if not logged_first_opus:
                    logged_first_opus = True
                    if timeline:
                        timeline.emit("tts.first_opus", payload={"elapsedMs": int((time.perf_counter() - turn_started_at) * 1000) if turn_started_at else None})
                    if turn_started_at is not None:
                        logger.info(
                            "latency_first_opus_sent session_id=%s device_id=%s elapsed_ms=%.1f",
                            session.session_id,
                            session.device_id,
                            (time.perf_counter() - turn_started_at) * 1000,
                        )
                sentence_frame_count += 1
                if self.tts_frame_pacing_ms > 0 and sentence_frame_count > self.tts_prebuffer_frames:
                    await asyncio.sleep(self.tts_frame_pacing_ms / 1000)
            if item.sentence_end and not ws.closed:
                await ws.send_json({"type": "tts", "state": "sentence_end", "text": item.text})

    async def _resume_listening_if_enabled(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        *,
        reason: str,
        timeline: InteractionTimeline | None = None,
    ) -> None:
        if not self.auto_resume_listening_after_tts or ws.closed:
            return
        await ws.send_json({"type": "listen", "state": "start", "mode": "auto"})
        if timeline:
            timeline.emit("listen.resume", payload={"reason": reason})
        logger.info(
            "listen_resume session_id=%s device_id=%s reason=%s",
            session.session_id,
            session.device_id,
            reason,
        )

    async def _send_emotion_for_text(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        text: str,
        *,
        timeline: InteractionTimeline | None = None,
        last_emotion: str | None = None,
    ) -> str | None:
        emotion = _emotion_from_text(text)
        if not emotion:
            return last_emotion
        if emotion == last_emotion:
            return last_emotion
        await ws.send_json({"type": "llm", "emotion": emotion})
        if timeline:
            timeline.emit("llm.emotion", payload={"emotion": emotion})
        logger.info(
            "llm_emotion session_id=%s device_id=%s emotion=%s",
            session.session_id,
            session.device_id,
            emotion,
        )
        return emotion

    async def _send_assistant_text(
        self,
        ws: web.WebSocketResponse,
        session: VoiceSession,
        text: str,
        turn_type: str,
        *,
        timeline: InteractionTimeline | None = None,
        send_lock: asyncio.Lock | None = None,
    ) -> None:
        if send_lock:
            async with send_lock:
                await self._send_assistant_text(ws, session, text, turn_type, timeline=timeline)
            return
        text = " ".join(text.split())
        session.turns.append(Turn(role="assistant", text=text, turn_type=turn_type))
        if timeline:
            timeline.emit("assistant.text", payload={"turnType": turn_type, "textChars": len(text)})
        logger.info(
            "assistant_text session_id=%s device_id=%s turn_type=%s chars=%s",
            session.session_id,
            session.device_id,
            turn_type,
            len(text),
        )
        await self._send_emotion_for_text(ws, session, text, timeline=timeline)
        await ws.send_json({"type": "text", "role": "assistant", "turn_type": turn_type, "text": text})
        chunks = _split_tts_chunks(text)
        if not chunks:
            return
        text_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.tts_text_queue_size)
        audio_queue: asyncio.Queue[TTSAudioBatch | None] = asyncio.Queue(maxsize=self.tts_audio_queue_size)
        turn_started_at = time.perf_counter()
        tts_task = asyncio.create_task(self._tts_worker(session, text_queue, audio_queue, turn_started_at=turn_started_at, timeline=timeline))
        sender_task = asyncio.create_task(self._audio_sender(ws, session, audio_queue, turn_started_at=turn_started_at, timeline=timeline))
        for chunk in chunks:
            await text_queue.put(chunk)
        await text_queue.put(None)
        await tts_task
        await audio_queue.put(None)
        await sender_task


def _extract_mode(data: dict[str, Any]) -> str:
    request = data.get("request")
    if isinstance(request, dict) and request.get("mode"):
        return str(request["mode"]).strip().lower()
    return str(data.get("mode") or "normal").strip().lower()


class StreamingSentenceSegmenter:
    def __init__(
        self,
        *,
        first_flush_chars: int = 90,
        max_chunk_chars: int = 260,
        min_sentence_chars: int = 35,
    ) -> None:
        self.buffer = ""
        self.first = True
        self.first_flush_chars = first_flush_chars
        self.max_chunk_chars = max_chunk_chars
        self.min_sentence_chars = min_sentence_chars

    def push(self, text: str) -> list[str]:
        self.buffer += text
        chunks: list[str] = []
        while True:
            split_at = self._find_split()
            if split_at <= 0:
                break
            chunk = self.buffer[:split_at].strip()
            self.buffer = self.buffer[split_at:].lstrip()
            if chunk:
                chunks.append(chunk)
                self.first = False
        return chunks

    def flush(self) -> list[str]:
        chunk = self.buffer.strip()
        self.buffer = ""
        return [chunk] if chunk else []

    def _find_split(self) -> int:
        if self.first:
            punctuation_match = self._terminal_punctuation_match()
            if punctuation_match and punctuation_match.end() >= self.min_sentence_chars:
                return punctuation_match.end()
            if len(self.buffer) >= self.first_flush_chars:
                return self._word_boundary(self.first_flush_chars)
            return -1

        punctuation_match = self._terminal_punctuation_match()
        if punctuation_match and punctuation_match.end() >= self.min_sentence_chars:
            return punctuation_match.end()
        if len(self.buffer) >= self.max_chunk_chars:
            return self._word_boundary(self.max_chunk_chars)
        return -1

    def _terminal_punctuation_match(self) -> re.Match[str] | None:
        return re.search(r"[。.!?！？；;]\s*", self.buffer)

    def _word_boundary(self, max_chars: int) -> int:
        prefix = self.buffer[:max_chars]
        boundary = prefix.rfind(" ")
        return boundary if boundary > 20 else max_chars


def _split_tts_chunks(text: str, max_chars: int = 220) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences or [text.strip()]:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks
