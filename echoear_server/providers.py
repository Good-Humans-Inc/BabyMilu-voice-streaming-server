from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
import ormsgpack

from .audio import (
    OPUS_SAMPLE_RATE,
    OpusStreamEncoder,
    StreamingPcmResampler,
    decode_opus_frames,
    pcm16_to_wav_bytes,
)
from .config import provider_config

LOGGER = logging.getLogger("echoear_server")


class ProviderError(RuntimeError):
    pass


class AsrProvider:
    name = "asr"

    async def transcribe(self, frames: list[bytes], audio_format: str, session_id: str) -> str:
        raise NotImplementedError


class LlmProvider:
    name = "llm"

    async def complete(self, transcript: str, messages: list[dict[str, str]] | None = None) -> str:
        raise NotImplementedError


class TtsProvider:
    name = "tts"

    async def synthesize_opus(self, text: str) -> list[bytes]:
        raise NotImplementedError


class MockAsr(AsrProvider):
    name = "mock-asr"

    async def transcribe(self, frames: list[bytes], audio_format: str, session_id: str) -> str:
        await asyncio.sleep(0)
        frame_word = "frame" if len(frames) == 1 else "frames"
        return f"hello from queued audio ({len(frames)} {frame_word})"


class MockLlm(LlmProvider):
    name = "mock-llm"

    async def complete(self, transcript: str, messages: list[dict[str, str]] | None = None) -> str:
        await asyncio.sleep(0)
        return f"I heard you say: {transcript}. The queued audio path is working."


class MockTts(TtsProvider):
    name = "mock-fish-audio"

    async def synthesize_opus(self, text: str) -> list[bytes]:
        await asyncio.sleep(0)
        return [f"mock-opus:{i}:{text[:20]}".encode("utf-8") for i in range(3)]


@dataclass
class OpenAiAsr(AsrProvider):
    config: dict[str, Any]
    name: str = "openai-asr"

    async def transcribe(self, frames: list[bytes], audio_format: str, session_id: str) -> str:
        if not frames:
            return ""

        if audio_format == "pcm":
            pcm = b"".join(frames)
        else:
            pcm = await asyncio.to_thread(decode_opus_frames, frames)

        wav_bytes = pcm16_to_wav_bytes(pcm)
        output_dir = Path(self.config.get("output_dir") or "tmp")
        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"asr_{session_id}_{uuid.uuid4().hex}.wav"
        wav_path.write_bytes(wav_bytes)

        api_key = self.config.get("api_key")
        if not api_key:
            raise ProviderError("OpenAI ASR api_key is not configured")

        form = aiohttp.FormData()
        form.add_field("model", self.config.get("model_name", "gpt-4o-mini-transcribe"))
        language = self.config.get("language")
        if language:
            form.add_field("language", str(language))
        form.add_field(
            "file",
            wav_bytes,
            filename=wav_path.name,
            content_type="audio/wav",
        )

        timeout = aiohttp.ClientTimeout(total=float(self.config.get("timeout_seconds", 45)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.config.get("base_url", "https://api.openai.com/v1/audio/transcriptions"),
                headers={"Authorization": f"Bearer {api_key}"},
                data=form,
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise ProviderError(f"ASR failed: {response.status} {body[:200]}")
                try:
                    data = await response.json()
                except Exception as exc:
                    raise ProviderError(f"ASR returned non-JSON response: {body[:200]}") from exc
        return (data.get("text") or "").strip()


@dataclass
class OpenAiChatLlm(LlmProvider):
    config: dict[str, Any]
    name: str = "openai-llm"

    async def complete(self, transcript: str, messages: list[dict[str, str]] | None = None) -> str:
        api_key = self.config.get("api_key")
        if not api_key:
            raise ProviderError("OpenAI LLM api_key is not configured")

        base_url = str(self.config.get("base_url") or self.config.get("url") or "https://api.openai.com/v1")
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.get("model_name", "gpt-4o"),
            "messages": messages or [
                {
                    "role": "system",
                    "content": (
                        "You are EchoEar, a concise voice companion. "
                        "Reply naturally in one or two spoken sentences."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            "temperature": float(self.config.get("temperature", 0.6)),
            "top_p": float(self.config.get("top_p", 1)),
            "max_tokens": int(self.config.get("max_tokens", 160)),
        }
        timeout = aiohttp.ClientTimeout(total=float(self.config.get("timeout", 45)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise ProviderError(f"LLM failed: {response.status} {body[:200]}")
                data = await response.json()

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise ProviderError(f"LLM response missing text: {data}") from exc


@dataclass
class FishAudioTts(TtsProvider):
    config: dict[str, Any]
    name: str = "fish-audio"

    async def synthesize_opus(self, text: str) -> list[bytes]:
        api_key = self.config.get("api_key")
        reference_id = self.config.get("reference_id")
        if not api_key:
            raise ProviderError("Fish Audio api_key is not configured")
        if not reference_id:
            raise ProviderError("Fish Audio reference_id is not configured")

        fish_sample_rate = int(self.config.get("sample_rate", 44100))
        request_data = {
            "text": text,
            "reference_id": reference_id,
            "format": "pcm",
            "sample_rate": fish_sample_rate,
            "normalize": bool(self.config.get("normalize", True)),
            "chunk_length": int(self.config.get("chunk_length", 100)),
            "top_p": float(self.config.get("top_p", 0.7)),
            "temperature": float(self.config.get("temperature", 0.7)),
            "repetition_penalty": float(self.config.get("repetition_penalty", 1.2)),
            "streaming": True,
        }
        timeout = aiohttp.ClientTimeout(total=float(self.config.get("total_timeout_seconds", 120)))
        resampler = StreamingPcmResampler(input_rate=fish_sample_rate, output_rate=OPUS_SAMPLE_RATE)
        encoder = OpusStreamEncoder()
        frames: list[bytes] = []
        fish_pcm_chunks: list[bytes] = []
        pcm_carry = b""

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                self.config.get("api_url", "https://api.fish.audio/v1/tts"),
                data=ormsgpack.packb(request_data),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/msgpack",
                },
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise ProviderError(f"Fish Audio failed: {response.status} {body[:200]}")

                async for chunk in response.content.iter_chunked(4096):
                    if not chunk:
                        continue
                    chunk = pcm_carry + chunk
                    if len(chunk) % 2:
                        pcm_carry = chunk[-1:]
                        chunk = chunk[:-1]
                    else:
                        pcm_carry = b""
                    if chunk:
                        fish_pcm_chunks.append(chunk)
                    pcm16_16k = resampler.process(chunk)
                    if pcm16_16k:
                        frames.extend(encoder.feed(pcm16_16k))

        frames.extend(encoder.finish())
        self._write_debug_audio(text, fish_sample_rate, fish_pcm_chunks, frames)
        return frames

    def _write_debug_audio(
        self,
        text: str,
        fish_sample_rate: int,
        fish_pcm_chunks: list[bytes],
        frames: list[bytes],
    ) -> None:
        debug_dir_value = self.config.get("debug_audio_dir", "generated_audio")
        if not debug_dir_value:
            return

        try:
            debug_dir = Path(debug_dir_value)
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_prefix = "".join(ch.lower() if ch.isalnum() else "_" for ch in text[:36]).strip("_")
            if not safe_prefix:
                safe_prefix = "tts"
            stem = f"tts_{uuid.uuid4().hex[:12]}_{safe_prefix}"

            fish_pcm = b"".join(fish_pcm_chunks)
            fish_wav = debug_dir / f"{stem}_fish_{fish_sample_rate}.wav"
            fish_wav.write_bytes(pcm16_to_wav_bytes(fish_pcm, fish_sample_rate))

            sent_pcm = decode_opus_frames(frames) if frames else b""
            sent_wav = debug_dir / f"{stem}_sent_{OPUS_SAMPLE_RATE}.wav"
            sent_wav.write_bytes(pcm16_to_wav_bytes(sent_pcm, OPUS_SAMPLE_RATE))

            LOGGER.info(
                "tts debug audio saved fish=%s sent=%s frames=%s",
                fish_wav,
                sent_wav,
                len(frames),
            )
        except Exception:
            LOGGER.exception("failed to save tts debug audio")


def _sine_pcm(seconds: float = 0.3, rate: int = OPUS_SAMPLE_RATE) -> bytes:
    sample_count = int(seconds * rate)
    t = np.arange(sample_count, dtype=np.float32) / rate
    signal = 0.08 * np.sin(2 * math.pi * 440 * t)
    return np.round(signal * 32767).astype("<i2").tobytes()


def build_providers(config: dict[str, Any]) -> tuple[AsrProvider, LlmProvider, TtsProvider]:
    if config.get("mock_providers"):
        return MockAsr(), MockLlm(), MockTts()

    _, asr_cfg = provider_config(config, "ASR")
    _, llm_cfg = provider_config(config, "LLM")
    _, tts_cfg = provider_config(config, "TTS")

    asr = OpenAiAsr(asr_cfg)
    llm = OpenAiChatLlm(llm_cfg)
    tts = FishAudioTts(tts_cfg)
    return asr, llm, tts
