import requests
import ormsgpack
from core.utils.util import check_model_key
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.fishspeech import ServeTTSRequest, ServeReferenceAudio
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("api_key", "YOUR_API_KEY")
        self.api_url = config.get("api_url", "https://api.fish.audio/v1/tts")
        self.default_reference_id = config.get("reference_id")
        self.format = config.get("format", "mp3")
        self.audio_file_type = self.format
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

    async def text_to_speak(self, text, output_file):
        reference_id = None
        if self.conn and getattr(self.conn, "voice_id", None):
            reference_id = self.conn.voice_id
        if not reference_id:
            reference_id = self.default_reference_id

        if not reference_id:
            raise Exception(
                "No Fish Audio reference_id configured. "
                "Set 'reference_id' in FishAudio config or the character's 'voice' field in Firestore."
            )

        request_data = ServeTTSRequest(
            text=text,
            reference_id=reference_id,
            format=self.format,
            normalize=self.normalize,
            chunk_length=self.chunk_length,
            top_p=self.top_p,
            temperature=self.temperature,
            repetition_penalty=self.repetition_penalty,
        )

        response = requests.post(
            self.api_url,
            data=ormsgpack.packb(request_data, option=ormsgpack.OPT_SERIALIZE_PYDANTIC),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/msgpack",
            },
        )

        if response.status_code == 200:
            if output_file:
                with open(output_file, "wb") as f:
                    f.write(response.content)
            else:
                return response.content
        else:
            raise Exception(
                f"Fish Audio TTS failed: {response.status_code} - {response.text}"
            )
