import os
import queue
import audioop
import asyncio
import traceback
import ormsgpack
import websockets
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils
from core.utils.tts import MarkdownCleaner
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()

# Fish Audio PCM output sample rate (44100Hz mono 16-bit)
FISH_AUDIO_SAMPLE_RATE = 44100
OPUS_SAMPLE_RATE = 16000


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.DUAL_STREAM

        self.api_key = config.get("api_key", "YOUR_API_KEY")
        self.ws_url = config.get("ws_url", "wss://api.fish.audio/v1/tts/live")
        self.model = config.get("model", "speech-1.5")
        self.default_reference_id = config.get("reference_id")
        self.latency = config.get("latency", "balanced")
        self.normalize = str(config.get("normalize", True)).lower() in ("true", "1", "yes")

        chunk_length = config.get("chunk_length", "200")
        top_p = config.get("top_p", "0.7")
        temperature = config.get("temperature", "0.7")
        repetition_penalty = config.get("repetition_penalty", "1.2")

        self.chunk_length = int(chunk_length) if chunk_length else 200
        self.top_p = float(top_p) if top_p else 0.7
        self.temperature = float(temperature) if temperature else 0.7
        self.repetition_penalty = float(repetition_penalty) if repetition_penalty else 1.2

        model_key_msg = check_model_key("FishAudio TTS", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

        self.ws = None
        self._monitor_task = None
        self.pending_text = ""

        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=OPUS_SAMPLE_RATE, channels=1, frame_size_ms=60
        )

    def _resolve_reference_id(self):
        if self.conn and getattr(self.conn, "voice_id", None):
            return self.conn.voice_id
        return self.default_reference_id

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)

                if message.sentence_type == SentenceType.FIRST:
                    self.conn.client_abort = False
                    self.pending_text = ""
                    self.tts_audio_first_sentence = True
                    self.before_stop_play_files.clear()
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.start_session(getattr(self.conn, "sentence_id", None)),
                            loop=self.conn.loop,
                        )
                        future.result()
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Failed to start Fish Audio session: {e}")
                        self.tts_audio_queue.put((SentenceType.LAST, [], None))
                        continue

                elif self.conn.client_abort:
                    logger.bind(tag=TAG).info("Client aborted, skipping TTS")
                    self.pending_text = ""
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.finish_session(getattr(self.conn, "sentence_id", None)),
                            loop=self.conn.loop,
                        )
                        future.result(timeout=3)
                    except Exception:
                        pass
                    continue

                elif ContentType.TEXT == message.content_type:
                    if message.content_detail:
                        self.pending_text += message.content_detail

                        # Send when we reach a safe word/clause boundary
                        safe_chars = ["\n", ".", "!", "?", "！", "？", "，", ","]
                        last_idx = -1
                        for ch in safe_chars:
                            idx = self.pending_text.rfind(ch)
                            if idx > last_idx:
                                last_idx = idx

                        if last_idx != -1:
                            to_send = self.pending_text[: last_idx + 1]
                            self.pending_text = self.pending_text[last_idx + 1 :]
                            if to_send.strip():
                                try:
                                    future = asyncio.run_coroutine_threadsafe(
                                        self.text_to_speak(to_send, None),
                                        loop=self.conn.loop,
                                    )
                                    future.result()
                                except Exception as e:
                                    logger.bind(tag=TAG).error(f"Failed to send text: {e}")

                elif ContentType.FILE == message.content_type:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.finish_session(getattr(self.conn, "sentence_id", None)),
                            loop=self.conn.loop,
                        )
                        future.result()
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Failed to finish Fish Audio session: {e}")
                        self.tts_audio_queue.put((SentenceType.LAST, [], None))

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"TTS text thread error: {e}\n{traceback.format_exc()}"
                )

    async def start_session(self, session_id):
        reference_id = self._resolve_reference_id()
        if not reference_id:
            raise Exception(
                "No Fish Audio reference_id configured. "
                "Set 'reference_id' in FishAudio config or the character's 'voice' field in Firestore."
            )

        # Close any stale connection
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        logger.bind(tag=TAG).info("Connecting to Fish Audio WebSocket...")
        self.ws = await websockets.connect(
            self.ws_url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                "model": self.model,
            },
            ping_interval=20,
            ping_timeout=10,
        )

        self.opus_encoder.reset_state()

        start_event = {
            "event": "start",
            "request": {
                "text": "",
                "reference_id": reference_id,
                "format": "pcm",
                "latency": self.latency,
                "chunk_length": self.chunk_length,
                "normalize": self.normalize,
                "top_p": self.top_p,
                "temperature": self.temperature,
                "repetition_penalty": self.repetition_penalty,
            },
        }
        await self.ws.send(ormsgpack.packb(start_event))

        self._monitor_task = asyncio.create_task(self._monitor_response())
        logger.bind(tag=TAG).info("Fish Audio WebSocket session started")

    async def text_to_speak(self, text, _output_file):
        """Send a text chunk to the open Fish Audio WebSocket."""
        if not self.ws:
            logger.bind(tag=TAG).warning("WebSocket not open, dropping text")
            return
        text = MarkdownCleaner.clean_markdown(text)
        if text.strip():
            await self.ws.send(ormsgpack.packb({"event": "text", "text": text}))
            logger.bind(tag=TAG).debug(f"Sent text chunk: {text[:60]}")

    async def finish_session(self, session_id):
        logger.bind(tag=TAG).info("Finishing Fish Audio session")
        try:
            if self.ws:
                # Flush any remaining pending text
                if self.pending_text.strip():
                    try:
                        await self.text_to_speak(self.pending_text, None)
                    except Exception:
                        pass
                    self.pending_text = ""

                try:
                    await self.ws.send(ormsgpack.packb({"event": "flush"}))
                    await self.ws.send(ormsgpack.packb({"event": "stop"}))
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"Error sending stop signal: {e}")

                # Wait for monitor to drain remaining audio
                if self._monitor_task:
                    try:
                        await self._monitor_task
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"Monitor task error: {e}")
                    finally:
                        self._monitor_task = None

                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in finish_session: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None))
            raise

    async def _monitor_response(self):
        """Receive PCM audio from Fish Audio, resample, encode to Opus, push to queue."""
        resample_state = None
        pcm_carry = b""
        try:
            while True:
                if not self.ws:
                    break

                raw = await self.ws.recv()
                data = ormsgpack.unpackb(raw)
                event = data.get("event")

                if event == "audio":
                    audio_bytes = data.get("audio", b"")
                    if not audio_bytes:
                        continue

                    # Signal start of audio to playback thread on first chunk
                    if self.tts_audio_first_sentence:
                        self.tts_audio_queue.put((SentenceType.FIRST, [], None))
                        self.tts_audio_first_sentence = False

                    # Align to 16-bit boundary
                    audio_bytes = pcm_carry + audio_bytes
                    if len(audio_bytes) % 2:
                        pcm_carry = audio_bytes[-1:]
                        audio_bytes = audio_bytes[:-1]
                    else:
                        pcm_carry = b""

                    # Resample 44100Hz → 16000Hz
                    resampled, resample_state = audioop.ratecv(
                        audio_bytes, 2, 1, FISH_AUDIO_SAMPLE_RATE, OPUS_SAMPLE_RATE, resample_state
                    )

                    # Encode to Opus frames and push to audio queue
                    self.opus_encoder.encode_pcm_to_opus_stream(
                        resampled, end_of_stream=False, callback=self.handle_opus
                    )

                elif event == "finish":
                    reason = data.get("reason", "stop")
                    if reason == "error":
                        logger.bind(tag=TAG).error("Fish Audio stream ended with error")
                        raise Exception("Fish Audio stream error")

                    # Flush remaining samples in encoder buffer
                    self.opus_encoder.encode_pcm_to_opus_stream(
                        b"", end_of_stream=True, callback=self.handle_opus
                    )
                    self._process_before_stop_play_files()
                    logger.bind(tag=TAG).info("Fish Audio stream finished successfully")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Fish Audio monitor error: {e}\n{traceback.format_exc()}"
            )
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        if self._monitor_task:
            try:
                self._monitor_task.cancel()
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
