import os
import time
from typing import Optional, Tuple, List, Any, Dict

import requests

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    """
    ElevenLabs Speech-to-Text provider.
    API reference: https://elevenlabs.io/docs/api-reference/speech-to-text/convert

    Notes:
    - This provider uses NON_STREAM mode (single request per utterance).
    - It uploads a WAV file produced by the existing pipeline.
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM

        # ElevenLabs uses `xi-api-key` header. We accept either `xi_api_key` or `api_key`.
        self.xi_api_key = config.get("xi_api_key") or config.get("api_key")
        if not self.xi_api_key:
            raise ValueError("ElevenLabs ASR requires `xi_api_key` (or `api_key`) in config")

        # Endpoint is fixed in their docs, but allow override for proxies.
        self.api_url = config.get("base_url") or "https://api.elevenlabs.io/v1/speech-to-text"

        # Form fields (see docs). Keep minimal defaults; allow extra params via `params`.
        self.model_id = config.get("model_id", "scribe_v1")
        self.language_code = config.get("language_code")  # ISO-639-1 or ISO-639-3, optional
        self.params: Dict[str, Any] = config.get("params") or {}

        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        os.makedirs(self.output_dir, exist_ok=True)

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus"
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path: Optional[str] = None
        try:
            start_time = time.time()

            if audio_format == "pcm":
                pcm_data = opus_data
            else:
                pcm_data = self.decode_opus(opus_data)

            file_path = self.save_audio_to_file(pcm_data, session_id)
            logger.bind(tag=TAG).debug(
                f"ElevenLabs ASR: audio saved in {time.time() - start_time:.3f}s | path={file_path}"
            )

            headers = {"xi-api-key": self.xi_api_key}

            data: Dict[str, Any] = {"model_id": self.model_id}
            if self.language_code:
                data["language_code"] = self.language_code

            # Allow optional extra fields (e.g., diarize, timestamps_granularity, tag_audio_events...)
            # We don't validate schema here; ElevenLabs will return 4xx on invalid fields.
            for k, v in (self.params or {}).items():
                if v is None:
                    continue
                data[k] = v

            with open(file_path, "rb") as audio_file:
                files = {"file": audio_file}
                start_time = time.time()
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=60,
                )
                logger.bind(tag=TAG).debug(
                    f"ElevenLabs ASR: request took {time.time() - start_time:.3f}s | status={response.status_code}"
                )

            if response.status_code != 200:
                raise Exception(f"ElevenLabs API request failed: {response.status_code} - {response.text}")

            payload = response.json()

            # Try common shapes; fall back to stringified payload.
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text, file_path

            text = payload.get("transcript")
            if isinstance(text, str) and text.strip():
                return text, file_path

            transcripts = payload.get("transcripts")
            if isinstance(transcripts, dict):
                # Join per-channel transcripts deterministically.
                parts: List[str] = []
                for channel_key in sorted(transcripts.keys()):
                    channel_obj = transcripts[channel_key]
                    if isinstance(channel_obj, dict):
                        t = channel_obj.get("text") or channel_obj.get("transcript")
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
                    elif isinstance(channel_obj, str) and channel_obj.strip():
                        parts.append(channel_obj.strip())
                if parts:
                    return "\n".join(parts), file_path

            return str(payload), file_path

        except Exception as e:
            logger.bind(tag=TAG).error(f"ElevenLabs ASR failed: {e}")
            return "", None
        finally:
            if self.delete_audio_file and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.bind(tag=TAG).debug(f"Deleted temp audio file: {file_path}")
                except Exception as e:
                    logger.bind(tag=TAG).error(f"Failed to delete temp file: {file_path} | err={e}")

