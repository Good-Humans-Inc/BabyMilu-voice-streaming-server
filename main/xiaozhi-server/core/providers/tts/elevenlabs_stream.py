import os
import json
import uuid
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

TAG = __name__
logger = setup_logging()

class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.api_key = config.get("xi-api-key")
        if not self.api_key:
            raise ValueError("xi-api-key is required in config for ElevenLabs TTS")

        self.client = ElevenLabs(
            api_key=self.api_key,
            base_url=config.get("base_url", "https://api.elevenlabs.io")
        )
        
        # Provider settings
        self.output_dir = config.get("output_dir", "tmp/")
        self.audio_file_type = config.get("format", "mp3")
        
        # API parameters
        self.default_voice_id = config.get("default_voice_id")
        self.model_id = config.get("model_id", "eleven_multilingual_v2")
        self.optimize_streaming_latency = config.get("optimize_streaming_latency")
        self.output_format = config.get("output_format")
        self.voice_settings_dict = config.get("voice_settings")


    def generate_filename(self):
        return os.path.join(self.output_dir, f"tts-{datetime.now().date()}@{uuid.uuid4().hex}.{self.audio_file_type}")

    async def text_to_speak(self, text, output_file):
        # Resolve voice_id: prefer connection value, then default from config
        voice_id = None
        if self.conn and getattr(self.conn, "voice_id", None):
            voice_id = self.conn.voice_id
        elif getattr(self, "default_voice_id", None):
            voice_id = str(self.default_voice_id)

        # Abort if voice_id missing
        if not voice_id:
            logger.bind(tag=TAG).error("No voice_id resolved (conn/default). Abort TTS request")
            raise Exception("No voice_id resolved; cannot call TTS")

        logger.debug(
            f"ElevenLabs TTS request: voice_id={voice_id}, model_id={self.model_id}"
        )

        try:
            voice_settings = None
            if self.voice_settings_dict:
                voice_settings = VoiceSettings(**self.voice_settings_dict)

            audio_stream = self.client.text_to_speech.stream(
                text=text,
                voice_id=voice_id,
                model_id=self.model_id,
                voice_settings=voice_settings,
                optimize_streaming_latency=self.optimize_streaming_latency,
                output_format=self.output_format
            )

            if output_file:
                with open(output_file, "wb") as file:
                    for chunk in audio_stream:
                        if self.conn and self.conn.client_abort:
                            logger.bind(tag=TAG).info("TTS interrupted by client")
                            # We can't really 'close' the stream from the SDK in the same way.
                            # We just stop iterating.
                            return
                        if chunk:
                            file.write(chunk)
            else:
                audio_data = bytearray()
                for chunk in audio_stream:
                    if self.conn and self.conn.client_abort:
                        logger.bind(tag=TAG).info("TTS interrupted by client")
                        return None
                    if chunk:
                        audio_data.extend(chunk)
                return bytes(audio_data)

        except Exception as e:
            error_msg = f"ElevenLabs TTS request failed: {str(e)}"
            logger.bind(tag=TAG).error(error_msg)
            raise Exception(error_msg)
