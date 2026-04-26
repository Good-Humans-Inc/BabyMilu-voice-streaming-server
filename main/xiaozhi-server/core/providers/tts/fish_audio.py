import asyncio
import os
import queue
import threading
import time
import traceback

import aiohttp
import numpy as np
import ormsgpack
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import ContentType, InterfaceType, SentenceType
from core.providers.tts.fishspeech import ServeTTSRequest
from core.utils import opus_encoder_utils, textUtils
from core.utils.tts import MarkdownCleaner
from core.utils.util import check_model_key

TAG = __name__
logger = setup_logging()

FISH_AUDIO_SAMPLE_RATE = 44100
OPUS_SAMPLE_RATE = 16000
DEFAULT_MAX_CONCURRENT_REQUESTS = 3
DEFAULT_RETRY_429_ATTEMPTS = 1
DEFAULT_RETRY_429_BACKOFF_MS = 500
DEFAULT_CONNECT_TIMEOUT_SECONDS = 8
DEFAULT_TOTAL_TIMEOUT_SECONDS = 60

_REQUEST_LIMITER = None
_REQUEST_LIMITER_CAPACITY = None
_REQUEST_LIMITER_LOCK = threading.Lock()
_SHARED_CONNECTORS = {}
_SHARED_CONNECTORS_LOCK = threading.Lock()


class _FishRequestLimiter:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._in_flight = 0

    def acquire(self):
        started = time.perf_counter()
        self._semaphore.acquire()
        wait_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self._in_flight += 1
            in_flight = self._in_flight
        return wait_ms, in_flight

    def release(self):
        with self._lock:
            self._in_flight -= 1
            in_flight = self._in_flight
        self._semaphore.release()
        return in_flight


def _get_request_limiter(capacity: int):
    global _REQUEST_LIMITER, _REQUEST_LIMITER_CAPACITY
    with _REQUEST_LIMITER_LOCK:
        if _REQUEST_LIMITER is None or _REQUEST_LIMITER_CAPACITY != capacity:
            _REQUEST_LIMITER = _FishRequestLimiter(capacity)
            _REQUEST_LIMITER_CAPACITY = capacity
        return _REQUEST_LIMITER


def _get_shared_connector():
    loop = asyncio.get_running_loop()
    with _SHARED_CONNECTORS_LOCK:
        connector = _SHARED_CONNECTORS.get(loop)
        if connector is None or connector.closed:
            connector = aiohttp.TCPConnector(
                limit=0,
                ttl_dns_cache=300,
                use_dns_cache=True,
            )
            _SHARED_CONNECTORS[loop] = connector
        return connector


async def close_shared_resources():
    with _SHARED_CONNECTORS_LOCK:
        connectors = list(_SHARED_CONNECTORS.values())
        _SHARED_CONNECTORS.clear()

    for connector in connectors:
        close_result = connector.close()
        if asyncio.iscoroutine(close_result):
            await close_result


class _StreamingPcmResampler:
    """Small linear PCM resampler that doesn't depend on audioop."""

    def __init__(self, input_rate: int, output_rate: int):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._step = input_rate / output_rate
        self.reset()

    def reset(self):
        self._phase = 0.0
        self._prev_sample = None

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype="<i2")
        if samples.size == 0:
            return b""

        if self._prev_sample is not None:
            samples = np.concatenate(
                (np.array([self._prev_sample], dtype=np.int16), samples)
            )

        if samples.size < 2:
            self._prev_sample = int(samples[-1])
            return b""

        max_pos = samples.size - 1
        positions = np.arange(self._phase, max_pos, self._step, dtype=np.float64)
        if positions.size == 0:
            self._phase -= max_pos
            self._prev_sample = int(samples[-1])
            return b""

        left = np.floor(positions).astype(np.int64)
        frac = positions - left
        right = left + 1

        left_samples = samples[left].astype(np.float64)
        right_samples = samples[right].astype(np.float64)
        output = np.round(
            (left_samples * (1.0 - frac)) + (right_samples * frac)
        ).astype(np.int16)

        self._phase = positions[-1] + self._step - max_pos
        self._prev_sample = int(samples[-1])
        return output.tobytes()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM

        self.api_key = config.get("api_key", "YOUR_API_KEY")
        self.api_url = config.get("api_url", "https://api.fish.audio/v1/tts")
        self.default_reference_id = config.get("reference_id")
        self.latency = config.get("latency", "normal")
        self.normalize = str(config.get("normalize", True)).lower() in (
            "true",
            "1",
            "yes",
        )

        chunk_length = config.get("chunk_length", "200")
        top_p = config.get("top_p", "0.7")
        temperature = config.get("temperature", "0.7")
        repetition_penalty = config.get("repetition_penalty", "1.2")

        self.chunk_length = int(chunk_length) if chunk_length else 200
        self.top_p = float(top_p) if top_p else 0.7
        self.temperature = float(temperature) if temperature else 0.7
        self.repetition_penalty = (
            float(repetition_penalty) if repetition_penalty else 1.2
        )
        self.max_concurrent_requests = max(
            1,
            int(
                config.get(
                    "max_concurrent_requests", DEFAULT_MAX_CONCURRENT_REQUESTS
                )
            ),
        )
        self.retry_429_attempts = max(
            0,
            int(config.get("retry_429_attempts", DEFAULT_RETRY_429_ATTEMPTS)),
        )
        self.retry_429_backoff_ms = max(
            0,
            int(config.get("retry_429_backoff_ms", DEFAULT_RETRY_429_BACKOFF_MS)),
        )
        self.connect_timeout_seconds = max(
            1,
            int(
                config.get(
                    "connect_timeout_seconds", DEFAULT_CONNECT_TIMEOUT_SECONDS
                )
            ),
        )
        self.total_timeout_seconds = max(
            self.connect_timeout_seconds,
            int(config.get("total_timeout_seconds", DEFAULT_TOTAL_TIMEOUT_SECONDS)),
        )
        self.request_limiter = _get_request_limiter(self.max_concurrent_requests)

        model_key_msg = check_model_key("FishAudio TTS", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)

        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=OPUS_SAMPLE_RATE, channels=1, frame_size_ms=60
        )
        self.resampler = _StreamingPcmResampler(
            input_rate=FISH_AUDIO_SAMPLE_RATE,
            output_rate=OPUS_SAMPLE_RATE,
        )

    def _resolve_reference_id(self):
        reference_id, _ = self._resolve_reference_id_with_source()
        return reference_id

    def _resolve_reference_id_with_source(self):
        if self.conn and getattr(self.conn, "voice_id", None):
            return self.conn.voice_id, "conn.voice_id"
        return self.default_reference_id, "default_config"

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)

                if message.sentence_type == SentenceType.FIRST:
                    self.conn.client_abort = False
                    self.tts_stop_request = False
                    self.tts_text_buff = []
                    self.tts_audio_first_sentence = True
                    self.before_stop_play_files.clear()
                    self.processed_chars = 0
                    self.is_first_sentence = True
                elif self.conn.client_abort:
                    continue

                elif ContentType.TEXT == message.content_type:
                    if message.content_detail:
                        self.tts_text_buff.append(message.content_detail)
                        segment_text = self._get_segment_text()
                        if segment_text and not self.conn.client_abort:
                            try:
                                asyncio.run(
                                    self.text_to_speak(
                                        segment_text, is_last_segment=False
                                    )
                                )
                            except Exception as e:
                                logger.bind(tag=TAG).error(
                                    f"Fish Audio TTS failed: {e}\n{traceback.format_exc()}"
                                )

                elif ContentType.FILE == message.content_type:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    full_text = "".join(self.tts_text_buff)
                    remaining = textUtils.get_string_no_punctuation_or_emoji(
                        full_text[self.processed_chars :]
                    )
                    if remaining and not self.conn.client_abort:
                        try:
                            asyncio.run(self.text_to_speak(remaining))
                        except Exception as e:
                            logger.bind(tag=TAG).error(
                                f"Fish Audio TTS failed: {e}\n{traceback.format_exc()}"
                            )
                            self.tts_audio_queue.put((SentenceType.LAST, [], None))
                    else:
                        self._process_before_stop_play_files()

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"TTS text thread error: {e}\n{traceback.format_exc()}"
                )

    async def text_to_speak(self, text, _output_file=None, is_last_segment=True):
        reference_id, reference_source = self._resolve_reference_id_with_source()
        if not reference_id:
            raise Exception(
                "No Fish Audio reference_id configured. "
                "Set 'reference_id' in FishAudio config or the character's 'voice' field in Firestore."
            )

        text = MarkdownCleaner.clean_markdown(text)

        request_data = ServeTTSRequest(
            text=text,
            reference_id=reference_id,
            format="pcm",
            normalize=self.normalize,
            chunk_length=self.chunk_length,
            top_p=self.top_p,
            temperature=self.temperature,
            repetition_penalty=self.repetition_penalty,
            streaming=True,
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/msgpack",
        }

        self.opus_encoder.reset_state()
        self.resampler.reset()
        pcm_carry = b""

        max_attempts = self.retry_429_attempts + 1
        last_error = None
        timeout = aiohttp.ClientTimeout(
            total=self.total_timeout_seconds,
            connect=self.connect_timeout_seconds,
            sock_connect=self.connect_timeout_seconds,
        )

        for attempt in range(1, max_attempts + 1):
            wait_ms, in_flight = await asyncio.to_thread(self.request_limiter.acquire)
            if wait_ms >= 25:
                logger.bind(tag=TAG).info(
                    f"Fish Audio request queued for {wait_ms:.1f}ms "
                    f"(attempt {attempt}/{max_attempts}, in_flight={in_flight}/{self.max_concurrent_requests})"
                )

            try:
                async with aiohttp.ClientSession(
                    connector=_get_shared_connector(),
                    connector_owner=False,
                    timeout=timeout,
                ) as session:
                    async with session.post(
                        self.api_url,
                        data=ormsgpack.packb(
                            request_data, option=ormsgpack.OPT_SERIALIZE_PYDANTIC
                        ),
                        headers=headers,
                    ) as resp:
                        if resp.status == 429 and attempt < max_attempts:
                            body = await resp.text()
                            last_error = Exception(
                                f"Fish Audio TTS failed: {resp.status} - {body}"
                            )
                            backoff_ms = self.retry_429_backoff_ms * attempt
                            logger.bind(tag=TAG).warning(
                                f"Fish Audio rate limited on attempt {attempt}/{max_attempts}; "
                                f"retrying in {backoff_ms}ms"
                            )
                            if backoff_ms > 0:
                                await asyncio.sleep(backoff_ms / 1000)
                            continue

                        if resp.status != 200:
                            body = await resp.text()
                            if resp.status == 400 and "Reference not found" in body:
                                logger.bind(tag=TAG).warning(
                                    f"Fish Audio reference not found: reference_id={reference_id}, "
                                    f"source={reference_source}"
                                )
                            raise Exception(
                                f"Fish Audio TTS failed: {resp.status} - {body}"
                            )

                        self.tts_audio_queue.put((SentenceType.FIRST, [], text))

                        async for chunk in resp.content.iter_chunked(4096):
                            if self.conn and self.conn.client_abort:
                                logger.bind(tag=TAG).info("TTS interrupted by client")
                                return
                            if not chunk:
                                continue

                            chunk = pcm_carry + chunk
                            if len(chunk) % 2:
                                pcm_carry = chunk[-1:]
                                chunk = chunk[:-1]
                            else:
                                pcm_carry = b""

                            resampled = self.resampler.process(chunk)
                            if not resampled:
                                continue

                            self.opus_encoder.encode_pcm_to_opus_stream(
                                resampled,
                                end_of_stream=False,
                                callback=self.handle_opus,
                            )
                        break
            except Exception as exc:
                last_error = exc
                should_retry = "429" in str(exc) or isinstance(
                    exc, (asyncio.TimeoutError, aiohttp.ClientConnectionError)
                )
                if should_retry and attempt < max_attempts:
                    backoff_ms = self.retry_429_backoff_ms * attempt
                    logger.bind(tag=TAG).warning(
                        f"Fish Audio retrying after exception on attempt {attempt}/{max_attempts}: {exc}"
                    )
                    if backoff_ms > 0:
                        await asyncio.sleep(backoff_ms / 1000)
                    continue
                raise
            finally:
                remaining_in_flight = self.request_limiter.release()
                logger.bind(tag=TAG).debug(
                    f"Fish Audio request finished (attempt {attempt}/{max_attempts}); "
                    f"in_flight={remaining_in_flight}/{self.max_concurrent_requests}"
                )
        else:
            raise last_error or Exception("Fish Audio TTS failed without a response")

        self.opus_encoder.encode_pcm_to_opus_stream(
            b"", end_of_stream=True, callback=self.handle_opus
        )

        logger.bind(tag=TAG).info(f"Fish Audio TTS success: {text[:60]}")
        if is_last_segment:
            self._process_before_stop_play_files()

    async def close(self):
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
