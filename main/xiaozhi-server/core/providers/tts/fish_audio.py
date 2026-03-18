import os
import queue
import audioop
import asyncio
import traceback
import aiohttp
import ormsgpack
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.utils import textUtils
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.fishspeech import ServeTTSRequest
from core.utils import opus_encoder_utils
from core.utils.tts import MarkdownCleaner
from core.providers.tts.dto.dto import SentenceType, ContentType

TAG = __name__
logger = setup_logging()

FISH_AUDIO_SAMPLE_RATE = 44100  # Fish Audio HTTP API default PCM output rate
OPUS_SAMPLE_RATE = 16000


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        self.api_key = config.get("api_key", "YOUR_API_KEY")
        self.api_url = config.get("api_url", "https://api.fish.audio/v1/tts")
        self.default_reference_id = config.get("reference_id")
        self.latency = config.get("latency", "normal")
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
                    self.tts_text_buff = []
                    self.tts_audio_first_sentence = True
                    self.before_stop_play_files.clear()

                elif self.conn.client_abort:
                    continue

                elif ContentType.TEXT == message.content_type:
                    if message.content_detail:
                        self.tts_text_buff.append(message.content_detail)

                elif ContentType.FILE == message.content_type:
                    if message.content_file:
                        if os.path.exists(message.content_file):
                            self._process_audio_file_stream(
                                message.content_file,
                                callback=lambda audio_data: self.handle_audio_file(
                                    audio_data, message.content_detail
                                ),
                            )

                if message.sentence_type == SentenceType.LAST:
                    full_text = "".join(self.tts_text_buff)
                    full_text = textUtils.get_string_no_punctuation_or_emoji(full_text)
                    if full_text and not self.conn.client_abort:
                        try:
                            asyncio.run(self.text_to_speak(full_text))
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

    async def text_to_speak(self, text, _output_file=None):
        reference_id = self._resolve_reference_id()
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
        pcm_carry = b""
        resample_state = None

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.api_url,
                data=ormsgpack.packb(request_data, option=ormsgpack.OPT_SERIALIZE_PYDANTIC),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise Exception(f"Fish Audio TTS failed: {resp.status} - {body}")

                self.tts_audio_queue.put((SentenceType.FIRST, [], text))

                async for chunk in resp.content.iter_chunked(4096):
                    if self.conn and self.conn.client_abort:
                        logger.bind(tag=TAG).info("TTS interrupted by client")
                        return
                    if not chunk:
                        continue

                    # Align to 16-bit boundary before resampling
                    chunk = pcm_carry + chunk
                    if len(chunk) % 2:
                        pcm_carry = chunk[-1:]
                        chunk = chunk[:-1]
                    else:
                        pcm_carry = b""

                    # Resample 44100Hz → 16000Hz
                    resampled, resample_state = audioop.ratecv(
                        chunk, 2, 1, FISH_AUDIO_SAMPLE_RATE, OPUS_SAMPLE_RATE, resample_state
                    )

                    self.opus_encoder.encode_pcm_to_opus_stream(
                        resampled, end_of_stream=False, callback=self.handle_opus
                    )

        # Flush remaining encoder buffer
        self.opus_encoder.encode_pcm_to_opus_stream(
            b"", end_of_stream=True, callback=self.handle_opus
        )

        logger.bind(tag=TAG).info(f"Fish Audio TTS success: {text[:60]}")
        self._process_before_stop_play_files()

    async def close(self):
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
