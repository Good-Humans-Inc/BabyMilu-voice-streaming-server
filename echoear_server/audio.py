import io
import wave
from dataclasses import dataclass, field

import numpy as np

OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_MS = 60
OPUS_FRAME_SAMPLES = OPUS_SAMPLE_RATE * OPUS_FRAME_MS // 1000
PCM_SAMPLE_WIDTH_BYTES = 2


def pcm16_to_wav_bytes(pcm: bytes, sample_rate: int = OPUS_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def decode_opus_frames(frames: list[bytes]) -> bytes:
    try:
        import opuslib_next
    except Exception as exc:  # pragma: no cover - depends on system libopus
        raise RuntimeError("opuslib_next is required to decode device Opus frames") from exc

    decoder = opuslib_next.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS)
    pcm_chunks: list[bytes] = []
    for frame in frames:
        if not frame:
            continue
        pcm_chunks.append(decoder.decode(frame, OPUS_FRAME_SAMPLES, decode_fec=False))
    return b"".join(pcm_chunks)


@dataclass
class StreamingPcmResampler:
    input_rate: int
    output_rate: int
    _phase: float = 0.0
    _prev_sample: int | None = None

    def reset(self) -> None:
        self._phase = 0.0
        self._prev_sample = None

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype="<i2")
        if samples.size == 0:
            return b""

        if self._prev_sample is not None:
            samples = np.concatenate((np.array([self._prev_sample], dtype=np.int16), samples))

        if samples.size < 2:
            self._prev_sample = int(samples[-1])
            return b""

        step = self.input_rate / self.output_rate
        max_pos = samples.size - 1
        positions = np.arange(self._phase, max_pos, step, dtype=np.float64)
        if positions.size == 0:
            self._phase -= max_pos
            self._prev_sample = int(samples[-1])
            return b""

        left = np.floor(positions).astype(np.int64)
        right = left + 1
        frac = positions - left
        output = np.round(
            samples[left].astype(np.float64) * (1.0 - frac)
            + samples[right].astype(np.float64) * frac
        ).astype(np.int16)

        self._phase = positions[-1] + step - max_pos
        self._prev_sample = int(samples[-1])
        return output.tobytes()


@dataclass
class OpusStreamEncoder:
    sample_rate: int = OPUS_SAMPLE_RATE
    channels: int = OPUS_CHANNELS
    frame_ms: int = OPUS_FRAME_MS
    _carry: bytes = b""
    _frames: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            import opuslib_next
        except Exception as exc:  # pragma: no cover - depends on system libopus
            raise RuntimeError("opuslib_next is required to encode Fish Audio PCM") from exc

        application = getattr(opuslib_next, "APPLICATION_AUDIO", "audio")
        self._frame_samples = self.sample_rate * self.frame_ms // 1000
        self._frame_bytes = self._frame_samples * self.channels * PCM_SAMPLE_WIDTH_BYTES
        self._encoder = opuslib_next.Encoder(self.sample_rate, self.channels, application)

    def feed(self, pcm: bytes) -> list[bytes]:
        data = self._carry + pcm
        complete_len = len(data) - (len(data) % self._frame_bytes)
        complete = data[:complete_len]
        self._carry = data[complete_len:]

        produced: list[bytes] = []
        for offset in range(0, len(complete), self._frame_bytes):
            frame = complete[offset : offset + self._frame_bytes]
            packet = self._encoder.encode(frame, self._frame_samples)
            produced.append(packet)
        self._frames.extend(produced)
        return produced

    def finish(self) -> list[bytes]:
        if not self._carry:
            return []
        padded = self._carry + b"\x00" * (self._frame_bytes - len(self._carry))
        self._carry = b""
        packet = self._encoder.encode(padded, self._frame_samples)
        self._frames.append(packet)
        return [packet]

