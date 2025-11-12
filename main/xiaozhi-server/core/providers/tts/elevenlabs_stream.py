import os
import json
import uuid
import requests
from config.logger import setup_logging
from datetime import datetime
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.url = config.get("url")
        self.method = config.get("method", "GET")
        self.headers = config.get("headers", {})
        self.format = config.get("format", "wav")
        self.audio_file_type = config.get("format", "wav")
        self.output_file = config.get("output_dir", "tmp/")
        self.params = config.get("params")
        # Default voice fallback (used if connection doesn't provide one)
        self.default_voice_id = config.get("default_voice_id") or config.get("voice_id")

        if isinstance(self.params, str):
            try:
                self.params = json.loads(self.params)
            except json.JSONDecodeError:
                raise ValueError("Custom TTS配置参数出错,无法将字符串解析为对象")
        elif not isinstance(self.params, dict):
            raise TypeError("Custom TTS配置参数出错, 请参考配置说明")

    def generate_filename(self):
        return os.path.join(self.output_file, f"tts-{datetime.now().date()}@{uuid.uuid4().hex}.{self.format}")

    async def text_to_speak(self, text, output_file):
        request_params = {}
        for k, v in self.params.items():
            if isinstance(v, str) and "{prompt_text}" in v:
                v = v.replace("{prompt_text}", text)
            request_params[k] = v

        # Resolve voice_id: prefer connection value, then default from config
        voice_id = None
        if self.conn and getattr(self.conn, "voice_id", None):
            voice_id = self.conn.voice_id
        elif getattr(self, "default_voice_id", None):
            voice_id = str(self.default_voice_id)

        # Build final URL from base + voice_id; abort if voice_id missing
        if not voice_id:
            logger.bind(tag=TAG).error("No voice_id resolved (conn/default). Abort TTS request")
            raise Exception("No voice_id resolved; cannot call TTS")

        final_url = f"{self.url.rstrip('/')}/{voice_id}"

        safe_headers = dict(self.headers or {})
        for _k in list(safe_headers.keys()):
            if _k.lower() in ("xi-api-key", "authorization"):
                safe_headers[_k] = "***"
        logger.debug(
            f"CustomTTS request: URL={final_url}, JSON={request_params}, HEADERS={safe_headers}"
        )

        if self.method.upper() == "POST":
            resp = requests.post(
                final_url, json=request_params, headers=self.headers, timeout=15, stream=True
            )
        else:
            resp = requests.get(
                final_url, params=request_params, headers=self.headers, timeout=15, stream=True
            )
        if resp.status_code == 200:
            if output_file:
                with open(output_file, "wb") as file:
                    for chunk in resp.iter_content(chunk_size=4096):
                        if self.conn and self.conn.client_abort:
                            logger.bind(tag=TAG).info("TTS interrupted by client")
                            resp.close()
                            return
                        file.write(chunk)
            else:
                audio_data = bytearray()
                for chunk in resp.iter_content(chunk_size=4096):
                    if self.conn and self.conn.client_abort:
                        logger.bind(tag=TAG).info("TTS interrupted by client")
                        resp.close()
                        return None
                    audio_data.extend(chunk)
                return bytes(audio_data)
        else:
            error_msg = f"Custom TTS请求失败: {resp.status_code} - {resp.text}"
            logger.bind(tag=TAG).error(error_msg)
            raise Exception(error_msg)  # 抛出异常，让调用方捕获
