from __future__ import annotations

import asyncio
import pathlib
import sys
import threading
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _NullLogger:
    def bind(self, **_kwargs):
        return self

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _FakeEncoder:
    def __init__(self, *_args, **_kwargs):
        self.reset_count = 0

    def reset_state(self):
        self.reset_count += 1

    def encode_pcm_to_opus_stream(self, pcm_data, end_of_stream, callback):
        if pcm_data and not end_of_stream:
            callback(b"opus")

    def close(self):
        pass


try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientConnectionError(Exception):
        pass

    class ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    aiohttp_stub.ClientConnectionError = ClientConnectionError
    aiohttp_stub.ClientTimeout = ClientTimeout
    aiohttp_stub.ClientSession = None
    sys.modules["aiohttp"] = aiohttp_stub

logger_stub = types.ModuleType("config.logger")
logger_stub.setup_logging = lambda: _NullLogger()
sys.modules.setdefault("config.logger", logger_stub)

report_stub = types.ModuleType("core.handle.reportHandle")
report_stub.enqueue_tts_report = lambda *_args, **_kwargs: None
sys.modules.setdefault("core.handle.reportHandle", report_stub)

send_audio_stub = types.ModuleType("core.handle.sendAudioHandle")


async def _send_audio_message(*_args, **_kwargs):
    return None


send_audio_stub.sendAudioMessage = _send_audio_message
sys.modules.setdefault("core.handle.sendAudioHandle", send_audio_stub)

opus_encoder_stub = types.ModuleType("core.utils.opus_encoder_utils")
opus_encoder_stub.OpusEncoderUtils = _FakeEncoder
sys.modules.setdefault("core.utils.opus_encoder_utils", opus_encoder_stub)

p3_stub = types.ModuleType("core.utils.p3")
p3_stub.decode_opus_from_file_stream = lambda *_args, **_kwargs: None
sys.modules.setdefault("core.utils.p3", p3_stub)

util_stub = types.ModuleType("core.utils.util")
util_stub.audio_bytes_to_data_stream = lambda *_args, **_kwargs: None
util_stub.audio_to_data_stream = lambda *_args, **_kwargs: None
util_stub.check_model_key = lambda *_args, **_kwargs: None
util_stub.parse_string_to_list = lambda value: [] if value in (None, "") else [value]
sys.modules.setdefault("core.utils.util", util_stub)

from core.providers.tts import fish_audio


class _FakeResampler:
    def __init__(self, *_args, **_kwargs):
        pass

    def reset(self):
        pass

    def process(self, pcm_bytes):
        return b"pcm" if pcm_bytes else b""


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status=200, body="", chunks=None):
        self.status = status
        self.body = body
        self.content = _FakeContent(chunks or [b"\x00\x00" * 16])
        self.text_calls = 0
        self.closed = False
        self.number = None
        self.factory = None

    async def __aenter__(self):
        self.factory.response_enter_count += 1
        self.factory.events.append(("response_enter", self.number))
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        self.factory.response_exit_count += 1
        self.factory.events.append(("response_exit", self.number))
        return False

    async def text(self):
        self.text_calls += 1
        self.factory.events.append(("response_text", self.number))
        return self.body


class _FakeSession:
    def __init__(self, factory):
        self.factory = factory
        self.closed = False

    async def __aenter__(self):
        self.factory.session_enter_count += 1
        self.factory.events.append(("session_enter", len(self.factory.sessions)))
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        self.factory.session_exit_count += 1
        self.factory.events.append(("session_exit", len(self.factory.sessions)))
        return False

    def post(self, url, data=None, headers=None):
        self.factory.post_count += 1
        self.factory.post_payloads.append(
            {"url": url, "data": data, "headers": headers}
        )
        item = self.factory.responses.pop(0)
        if isinstance(item, Exception):
            raise item

        item.factory = self.factory
        item.number = self.factory.post_count
        self.factory.events.append(("post", item.number))
        return item


class _FakeSessionFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sessions = []
        self.session_kwargs = []
        self.post_payloads = []
        self.events = []
        self.post_count = 0
        self.session_enter_count = 0
        self.session_exit_count = 0
        self.response_enter_count = 0
        self.response_exit_count = 0

    def __call__(self, *args, **kwargs):
        self.session_kwargs.append(kwargs)
        session = _FakeSession(self)
        self.sessions.append(session)
        return session


@pytest.fixture(autouse=True)
def _reset_fish_audio_state(monkeypatch):
    with fish_audio._INVALID_REFERENCES_LOCK:
        fish_audio._INVALID_REFERENCES.clear()
    monkeypatch.setattr(fish_audio, "_REQUEST_LIMITER", None)
    monkeypatch.setattr(fish_audio, "_REQUEST_LIMITER_CAPACITY", None)
    monkeypatch.setattr(fish_audio, "logger", _NullLogger())
    monkeypatch.setattr(fish_audio.opus_encoder_utils, "OpusEncoderUtils", _FakeEncoder)
    monkeypatch.setattr(fish_audio, "_StreamingPcmResampler", _FakeResampler)


def _make_provider(config=None, voice_id="voice-ref"):
    provider_config = {
        "api_key": "test-key",
        "api_url": "https://fish.test/v1/tts",
        "reference_id": "default-ref",
        "max_concurrent_requests": 1,
        "retry_429_attempts": 1,
        "retry_429_backoff_ms": 0,
        "connect_timeout_seconds": 1,
        "total_timeout_seconds": 1,
        "invalid_reference_ttl_seconds": 60,
    }
    if config:
        provider_config.update(config)

    provider = fish_audio.TTSProvider(provider_config, delete_audio_file=True)
    provider.conn = SimpleNamespace(
        stop_event=threading.Event(),
        client_abort=False,
        voice_id=voice_id,
    )
    return provider


def _assert_owned_sessions(factory):
    assert factory.session_enter_count == factory.session_exit_count
    assert factory.response_enter_count == factory.response_exit_count
    assert all(session.closed for session in factory.sessions)
    assert all(kwargs.get("connector_owner") is not False for kwargs in factory.session_kwargs)
    assert all("connector" not in kwargs for kwargs in factory.session_kwargs)


def test_successful_fish_request_closes_session_and_response(monkeypatch):
    response = _FakeResponse(status=200)
    factory = _FakeSessionFactory([response])
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)

    asyncio.run(_make_provider().text_to_speak("hello"))

    assert factory.post_count == 1
    assert response.closed is True
    _assert_owned_sessions(factory)


def test_400_reference_not_found_closes_response_and_is_not_retried(monkeypatch):
    response = _FakeResponse(status=400, body="Reference not found")
    factory = _FakeSessionFactory([response])
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)

    with pytest.raises(Exception, match="400"):
        asyncio.run(
            _make_provider({"retry_429_attempts": 3}).text_to_speak("bad reference")
        )

    assert factory.post_count == 1
    assert response.text_calls == 1
    assert response.closed is True
    assert fish_audio._get_invalid_reference_ttl_remaining("voice-ref") is not None
    _assert_owned_sessions(factory)


def test_429_retry_closes_each_attempt(monkeypatch):
    responses = [
        _FakeResponse(status=429, body="rate limited"),
        _FakeResponse(status=429, body="still rate limited"),
        _FakeResponse(status=200),
    ]
    factory = _FakeSessionFactory(responses)
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)

    asyncio.run(
        _make_provider({"retry_429_attempts": 2, "retry_429_backoff_ms": 0}).text_to_speak(
            "retry me"
        )
    )

    assert factory.post_count == 3
    assert responses[0].text_calls == 1
    assert responses[1].text_calls == 1
    assert all(response.closed for response in responses)
    assert factory.events.index(("response_exit", 1)) < factory.events.index(
        ("response_enter", 2)
    )
    assert factory.events.index(("response_exit", 2)) < factory.events.index(
        ("response_enter", 3)
    )
    _assert_owned_sessions(factory)


def test_client_connection_error_retries_without_leaking_session(monkeypatch):
    response = _FakeResponse(status=200)
    factory = _FakeSessionFactory(
        [fish_audio.aiohttp.ClientConnectionError("connect failed"), response]
    )
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)

    asyncio.run(
        _make_provider({"retry_429_attempts": 1, "retry_429_backoff_ms": 0}).text_to_speak(
            "connection retry"
        )
    )

    assert factory.post_count == 2
    assert factory.session_enter_count == 2
    assert factory.session_exit_count == 2
    assert factory.response_enter_count == 1
    assert factory.response_exit_count == 1
    assert response.closed is True
    _assert_owned_sessions(factory)


def test_cached_invalid_conn_voice_id_falls_back_to_default_reference(monkeypatch):
    captured_reference_ids = []

    def fake_packb(request_data, option=None):
        captured_reference_ids.append(request_data.reference_id)
        return b"payload"

    responses = [
        _FakeResponse(status=400, body="Reference not found"),
        _FakeResponse(status=200),
    ]
    factory = _FakeSessionFactory(responses)
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(fish_audio.ormsgpack, "packb", fake_packb)

    provider = _make_provider(voice_id="bad-ref")
    with pytest.raises(Exception, match="400"):
        asyncio.run(provider.text_to_speak("bad ref"))

    asyncio.run(provider.text_to_speak("uses fallback"))

    assert captured_reference_ids == ["bad-ref", "default-ref"]
    assert factory.post_count == 2
    assert responses[0].closed is True
    assert responses[1].closed is True
    _assert_owned_sessions(factory)


def test_cached_invalid_conn_voice_id_without_fallback_skips_fish(monkeypatch):
    responses = [_FakeResponse(status=400, body="Reference not found")]
    factory = _FakeSessionFactory(responses)
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", factory)

    provider = _make_provider({"reference_id": None}, voice_id="bad-ref")
    with pytest.raises(Exception, match="400"):
        asyncio.run(provider.text_to_speak("bad ref"))

    with pytest.raises(Exception, match="cached invalid"):
        asyncio.run(provider.text_to_speak("skipped before network"))

    assert factory.post_count == 1
    assert responses[0].closed is True
    _assert_owned_sessions(factory)
