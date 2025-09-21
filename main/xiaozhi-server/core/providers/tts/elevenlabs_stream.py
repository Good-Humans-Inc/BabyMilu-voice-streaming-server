import os
import json
import time
import queue
import asyncio
import aiohttp
import traceback
from config.logger import setup_logging
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.utils import opus_encoder_utils
from core.providers.tts.dto.dto import SentenceType, ContentType

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        # Required
        self.api_key = config.get("api_key")
        self.voice_id = config.get("voice_id")

        # Optional tuning
        self.model_id = config.get("model_id", "eleven_multilingual_v2")
        # 0..4 (4 = lowest latency, potentially more artifacts)
        self.optimize_streaming_latency = int(str(config.get("optimize_streaming_latency", 3)))
        # Use raw PCM so we can encode to Opus immediately
        self.output_format = config.get("output_format", "pcm_16000")

        # PCM format expectations when using pcm_16000
        self.sample_rate = 16000
        self.channels = 1
        self.audio_file_type = "pcm"

        # Optional per-voice settings passthrough
        self.voice_settings = config.get("voice_settings", {})

        # HTTP streaming endpoint
        # Ref: POST /v1/text-to-speech/{voice_id}/stream
        self.api_url = config.get(
            "api_url",
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream",
        )

        # Opus encoder: encode 60ms frames for our transport
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=self.sample_rate, channels=self.channels, frame_size_ms=60
        )

        # PCM buffer for chunking into opus frames
        self.pcm_buffer = bytearray()

    def tts_text_priority_thread(self):
        """Streaming text processing loop (single-sentence chunks)."""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    # Reset state for a new turn
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()

                if message.content_type == ContentType.TEXT and message.content_detail:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text, is_last=False)

                elif message.content_type == ContentType.FILE:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    # Flush remaining text
                    self._process_remaining_text_stream()

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self):
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = MarkdownCleaner.clean_markdown(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last=True)
                self.processed_chars += len(full_text)
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        """Stream a single sentence via HTTP chunked streaming and pipe to Opus."""
        text = MarkdownCleaner.clean_markdown(text)
        max_repeat_time = 5
        try:
            asyncio.run(self.text_to_speak(text, is_last))
        except Exception as e:
            logger.bind(tag=TAG).warning(
                f"语音生成失败{5 - max_repeat_time + 1}次: {text}，错误: {e}"
            )
            max_repeat_time -= 1
        finally:
            return None

    async def text_to_speak(self, text, is_last):
        """HTTP streaming to ElevenLabs stream endpoint, output raw PCM chunks."""
        headers = {
            "Content-Type": "application/json",
            "xi-api-key": self.api_key,
            # Request raw PCM if supported by server
            "Accept": "application/octet-stream",
        }
        payload = {
            "text": text,
            "model_id": self.model_id,
            "optimize_streaming_latency": self.optimize_streaming_latency,
            "output_format": self.output_format,  # expect pcm_16000
        }
        if isinstance(self.voice_settings, dict) and len(self.voice_settings) > 0:
            payload["voice_settings"] = self.voice_settings

        # Frame size in bytes (16-bit PCM)
        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=15,
                ) as resp:
                    if resp.status != 200:
                        try:
                            err_text = await resp.text()
                        except Exception:
                            err_text = f"status={resp.status}"
                        logger.bind(tag=TAG).error(f"TTS请求失败: {err_text}")
                        self.tts_audio_queue.put((SentenceType.LAST, [], None))
                        return

                    # First-sentence signal for clients
                    self.pcm_buffer.clear()
                    self.tts_audio_queue.put((SentenceType.FIRST, [], text))

                    async for chunk in resp.content.iter_any():
                        if not chunk:
                            continue
                        # When requesting pcm_16000, ElevenLabs streams raw 16-bit PCM bytes
                        self.pcm_buffer.extend(chunk)

                        while len(self.pcm_buffer) >= frame_bytes:
                            frame = bytes(self.pcm_buffer[:frame_bytes])
                            del self.pcm_buffer[:frame_bytes]
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                frame, end_of_stream=False, callback=self.handle_opus
                            )

                    # flush remainder
                    if self.pcm_buffer:
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            bytes(self.pcm_buffer),
                            end_of_stream=True,
                            callback=self.handle_opus,
                        )
                        self.pcm_buffer.clear()

                    if is_last:
                        self._process_before_stop_play_files()

        except Exception as e:
            logger.bind(tag=TAG).error(f"TTS请求异常: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()

    def to_tts(self, text: str) -> list:
        """Sync helper for tests: request non-stream and return Opus packets."""
        # For simplicity, defer to streaming path and accumulate.
        try:
            opus_list = []

            def _collect(opus_bytes: bytes):
                opus_list.append(opus_bytes)

            # Create a temp event loop to run streaming call
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run():
                headers = {
                    "Content-Type": "application/json",
                    "xi-api-key": self.api_key,
                    "Accept": "application/octet-stream",
                }
                payload = {
                    "text": MarkdownCleaner.clean_markdown(text),
                    "model_id": self.model_id,
                    "optimize_streaming_latency": self.optimize_streaming_latency,
                    "output_format": self.output_format,
                }
                frame_bytes_local = int(
                    self.opus_encoder.sample_rate
                    * self.opus_encoder.channels
                    * self.opus_encoder.frame_size_ms
                    / 1000
                    * 2
                )
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.api_url, headers=headers, data=json.dumps(payload), timeout=15
                    ) as resp:
                        if resp.status != 200:
                            return []
                        pcm_buf = bytearray()
                        async for chunk in resp.content.iter_any():
                            if not chunk:
                                continue
                            pcm_buf.extend(chunk)
                            while len(pcm_buf) >= frame_bytes_local:
                                frame = bytes(pcm_buf[:frame_bytes_local])
                                del pcm_buf[:frame_bytes_local]
                                self.opus_encoder.encode_pcm_to_opus_stream(
                                    frame, end_of_stream=False, callback=_collect
                                )
                        if pcm_buf:
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                bytes(pcm_buf), end_of_stream=True, callback=_collect
                            )
                return opus_list

            result = loop.run_until_complete(_run())
            loop.close()
            return result or []
        except Exception:
            return []


