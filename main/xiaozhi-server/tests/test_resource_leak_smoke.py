from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _Logger:
    def bind(self, **kwargs):
        return self

    def debug(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def _install_logger_fake(monkeypatch):
    logger_module = types.ModuleType("config.logger")
    logger_module.setup_logging = lambda: _Logger()
    logger_module.build_module_string = lambda selected_module: "00000000000000"
    logger_module.create_connection_logger = lambda *args, **kwargs: _Logger()
    monkeypatch.setitem(sys.modules, "config.logger", logger_module)
    return logger_module


def _fresh_import(module_name):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class _ServeTTSRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeOpusEncoder:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self.reset_count = 0
        self.encoded_chunks = 0

    def reset_state(self):
        self.reset_count += 1

    def encode_pcm_to_opus_stream(self, pcm_data, end_of_stream, callback):
        self.encoded_chunks += 1
        if pcm_data or end_of_stream:
            callback(b"opus")

    def close(self):
        self.closed = True


class _FakeResampler:
    def __init__(self, *args, **kwargs):
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1

    def process(self, pcm_bytes):
        return pcm_bytes


class _FakeFishResponse:
    def __init__(self, status=200, chunks=None, body="ok"):
        self.status = status
        self._chunks = list(chunks or [])
        self._body = body
        self.closed = False
        self.content = self

    async def text(self):
        return self._body

    async def iter_chunked(self, chunk_size):
        for chunk in self._chunks:
            yield chunk


class _FakeFishPostContext:
    def __init__(self, response, metrics):
        self.response = response
        self.metrics = metrics

    async def __aenter__(self):
        self.metrics["open_responses"] += 1
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.response.closed = True
        self.metrics["closed_responses"] += 1
        return False


class _FakeFishConnector:
    instances = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        type(self).instances.append(self)

    async def close(self):
        self.closed = True


class _FakeFishClientSession:
    instances = []
    responses = []
    metrics = {"open_sessions": 0, "closed_sessions": 0, "open_responses": 0, "closed_responses": 0}

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.connector = kwargs.get("connector")
        self.connector_owner = kwargs.get("connector_owner", True)
        type(self).instances.append(self)

    async def __aenter__(self):
        self.metrics["open_sessions"] += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        self.metrics["closed_sessions"] += 1
        if self.connector is not None and self.connector_owner:
            close_result = self.connector.close()
            if asyncio.iscoroutine(close_result):
                await close_result
        return False

    def post(self, *args, **kwargs):
        response = type(self).responses.pop(0)
        return _FakeFishPostContext(response, self.metrics)


def _reset_fish_session_fakes(responses):
    _FakeFishClientSession.instances = []
    _FakeFishClientSession.responses = list(responses)
    _FakeFishClientSession.metrics = {
        "open_sessions": 0,
        "closed_sessions": 0,
        "open_responses": 0,
        "closed_responses": 0,
    }
    _FakeFishConnector.instances = []


def _install_fish_audio_fakes(monkeypatch, responses):
    _install_logger_fake(monkeypatch)

    tts_module = types.ModuleType("core.utils.tts")
    tts_module.MarkdownCleaner = SimpleNamespace(clean_markdown=lambda text: text)
    text_utils_module = types.ModuleType("core.utils.textUtils")
    text_utils_module.get_string_no_punctuation_or_emoji = lambda text: text
    util_module = types.ModuleType("core.utils.util")
    util_module.check_model_key = lambda *args, **kwargs: None
    util_module.audio_bytes_to_data_stream = lambda *args, **kwargs: None
    util_module.audio_to_data_stream = lambda *args, **kwargs: None
    opus_module = types.ModuleType("core.utils.opus_encoder_utils")
    opus_module.OpusEncoderUtils = _FakeOpusEncoder
    fishspeech_module = types.ModuleType("core.providers.tts.fishspeech")
    fishspeech_module.ServeTTSRequest = _ServeTTSRequest
    output_counter_module = types.ModuleType("core.utils.output_counter")
    output_counter_module.add_device_output = lambda *args, **kwargs: None
    report_module = types.ModuleType("core.handle.reportHandle")
    report_module.enqueue_tts_report = lambda *args, **kwargs: None
    report_module.report = lambda *args, **kwargs: None
    send_audio_module = types.ModuleType("core.handle.sendAudioHandle")

    async def _send_audio_message(*args, **kwargs):
        return None

    send_audio_module.sendAudioMessage = _send_audio_message

    monkeypatch.setitem(sys.modules, "core.utils.tts", tts_module)
    monkeypatch.setitem(sys.modules, "core.utils.textUtils", text_utils_module)
    monkeypatch.setitem(sys.modules, "core.utils.util", util_module)
    monkeypatch.setitem(sys.modules, "core.utils.opus_encoder_utils", opus_module)
    monkeypatch.setitem(
        sys.modules,
        "core.providers.tts.fishspeech",
        fishspeech_module,
    )
    monkeypatch.setitem(sys.modules, "core.utils.output_counter", output_counter_module)
    monkeypatch.setitem(sys.modules, "core.handle.reportHandle", report_module)
    monkeypatch.setitem(sys.modules, "core.handle.sendAudioHandle", send_audio_module)

    sys.modules.pop("core.providers.tts.base", None)
    fish_audio = _fresh_import("core.providers.tts.fish_audio")

    _reset_fish_session_fakes(responses)
    monkeypatch.setattr(fish_audio, "ServeTTSRequest", _ServeTTSRequest)
    monkeypatch.setattr(fish_audio, "check_model_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(fish_audio, "_StreamingPcmResampler", _FakeResampler)
    monkeypatch.setattr(
        fish_audio.opus_encoder_utils,
        "OpusEncoderUtils",
        _FakeOpusEncoder,
    )
    monkeypatch.setattr(fish_audio.ormsgpack, "packb", lambda *args, **kwargs: b"packed")
    monkeypatch.setattr(fish_audio.aiohttp, "ClientSession", _FakeFishClientSession)
    monkeypatch.setattr(fish_audio.aiohttp, "TCPConnector", _FakeFishConnector, raising=False)
    monkeypatch.setattr(fish_audio, "_REQUEST_LIMITER", None)
    monkeypatch.setattr(fish_audio, "_REQUEST_LIMITER_CAPACITY", None)
    return fish_audio


def _fish_config(**overrides):
    config = {
        "api_key": "test-key",
        "reference_id": "voice-ref",
        "retry_429_attempts": 0,
        "retry_429_backoff_ms": 0,
        "connect_timeout_seconds": 1,
        "total_timeout_seconds": 1,
        "max_concurrent_requests": 2,
    }
    config.update(overrides)
    return config


def test_fish_audio_owns_and_closes_http_session_per_stream(monkeypatch):
    fish_audio = _install_fish_audio_fakes(
        monkeypatch,
        [_FakeFishResponse(chunks=[b"\x00\x00" * 960])],
    )

    provider = fish_audio.TTSProvider(_fish_config(), delete_audio_file=True)
    provider.conn = SimpleNamespace(client_abort=False, voice_id=None)

    asyncio.run(provider.text_to_speak("hello"))

    assert _FakeFishClientSession.metrics["open_sessions"] == 1
    assert _FakeFishClientSession.metrics["closed_sessions"] == 1
    assert _FakeFishClientSession.metrics["open_responses"] == 1
    assert _FakeFishClientSession.metrics["closed_responses"] == 1
    assert all(session.closed for session in _FakeFishClientSession.instances)
    assert all(connector.closed for connector in _FakeFishConnector.instances)
    assert all(
        session.connector_owner is not False
        for session in _FakeFishClientSession.instances
    )
    assert provider.request_limiter._in_flight == 0


def test_fish_audio_closes_each_attempt_after_429_retry(monkeypatch):
    fish_audio = _install_fish_audio_fakes(
        monkeypatch,
        [
            _FakeFishResponse(status=429, body="rate limited"),
            _FakeFishResponse(chunks=[b"\x00\x00" * 960]),
        ],
    )

    provider = fish_audio.TTSProvider(
        _fish_config(retry_429_attempts=1),
        delete_audio_file=True,
    )
    provider.conn = SimpleNamespace(client_abort=False, voice_id=None)

    asyncio.run(provider.text_to_speak("hello after retry"))

    assert _FakeFishClientSession.metrics["open_sessions"] == 2
    assert _FakeFishClientSession.metrics["closed_sessions"] == 2
    assert _FakeFishClientSession.metrics["open_responses"] == 2
    assert _FakeFishClientSession.metrics["closed_responses"] == 2
    assert all(session.closed for session in _FakeFishClientSession.instances)
    assert provider.request_limiter._in_flight == 0


def test_simple_http_server_cleans_runner_when_start_task_is_cancelled(monkeypatch):
    _install_logger_fake(monkeypatch)

    ota_module = types.ModuleType("core.api.ota_handler")
    ota_module.OTAHandler = lambda config: SimpleNamespace(
        handle_get=lambda request: None,
        handle_post=lambda request: None,
    )
    vision_module = types.ModuleType("core.api.vision_handler")
    vision_module.VisionHandler = lambda config: SimpleNamespace(
        handle_get=lambda request: None,
        handle_post=lambda request: None,
    )
    mqtt_module = types.ModuleType("services.messaging.mqtt")
    mqtt_module.publish_ws_start = lambda *args, **kwargs: True
    mqtt_module.publish_auto_update = lambda *args, **kwargs: True
    mac_module = types.ModuleType("core.utils.mac")
    mac_module.normalize_mac = lambda device_id: device_id
    monkeypatch.setitem(sys.modules, "core.api.ota_handler", ota_module)
    monkeypatch.setitem(sys.modules, "core.api.vision_handler", vision_module)
    monkeypatch.setitem(sys.modules, "services.messaging.mqtt", mqtt_module)
    monkeypatch.setitem(sys.modules, "core.utils.mac", mac_module)

    http_server = _fresh_import("core.http_server")

    class _FakeRouteApp:
        def __init__(self):
            self.routes = []
            self.router = SimpleNamespace(add_static=lambda *args, **kwargs: None)

        def add_routes(self, routes):
            self.routes.extend(routes)

    class _FakeRunner:
        instances = []

        def __init__(self, app):
            self.app = app
            self.setup_called = False
            self.cleanup_called = False
            type(self).instances.append(self)

        async def setup(self):
            self.setup_called = True

        async def cleanup(self):
            self.cleanup_called = True

    class _FakeSite:
        instances = []

        def __init__(self, runner, host, port):
            self.runner = runner
            self.host = host
            self.port = port
            self.started = False
            type(self).instances.append(self)

        async def start(self):
            self.started = True

    async def _cancel_immediately(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(http_server.web, "Application", _FakeRouteApp)
    monkeypatch.setattr(http_server.web, "AppRunner", _FakeRunner)
    monkeypatch.setattr(http_server.web, "TCPSite", _FakeSite)
    monkeypatch.setattr(http_server.web, "get", lambda *args, **kwargs: ("GET", args, kwargs))
    monkeypatch.setattr(http_server.web, "post", lambda *args, **kwargs: ("POST", args, kwargs))
    monkeypatch.setattr(http_server.web, "options", lambda *args, **kwargs: ("OPTIONS", args, kwargs))
    monkeypatch.setattr(http_server.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(http_server.asyncio, "sleep", _cancel_immediately)

    server = http_server.SimpleHttpServer.__new__(http_server.SimpleHttpServer)
    server.config = {
        "read_config_from_api": False,
        "server": {
            "ip": "127.0.0.1",
            "http_port": 8003,
            "port": 8000,
            "auth_key": "test-auth-key",
        },
    }
    server.logger = _Logger()
    server.ota_handler = SimpleNamespace(handle_get=lambda request: None, handle_post=lambda request: None)
    server.vision_handler = SimpleNamespace(handle_get=lambda request: None, handle_post=lambda request: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.start())

    assert _FakeRunner.instances
    assert _FakeRunner.instances[0].setup_called is True
    assert _FakeSite.instances[0].started is True
    assert _FakeRunner.instances[0].cleanup_called is True


def _install_vision_handler_fakes(monkeypatch, fake_vllm):
    _install_logger_fake(monkeypatch)

    util_module = types.ModuleType("core.utils.util")
    util_module.get_vision_url = lambda config: "http://127.0.0.1/vision"
    util_module.is_valid_image_file = lambda image_data: True
    vllm_module = types.ModuleType("core.utils.vllm")
    vllm_module.create_instance = lambda *args, **kwargs: fake_vllm
    config_loader_module = types.ModuleType("config.config_loader")
    config_loader_module.get_private_config_from_api = lambda config, device_id, client_id: config
    auth_module = types.ModuleType("core.utils.auth")
    auth_module.AuthToken = lambda auth_key: SimpleNamespace(
        verify_token=lambda token: (True, "device-1")
    )
    register_module = types.ModuleType("plugins_func.register")
    register_module.Action = SimpleNamespace(RESPONSE=SimpleNamespace(name="RESPONSE"))

    monkeypatch.setitem(sys.modules, "core.utils.util", util_module)
    monkeypatch.setitem(sys.modules, "core.utils.vllm", vllm_module)
    monkeypatch.setitem(sys.modules, "config.config_loader", config_loader_module)
    monkeypatch.setitem(sys.modules, "core.utils.auth", auth_module)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register_module)
    return _fresh_import("core.api.vision_handler")


class _MultipartField:
    def __init__(self, *, text=None, data=None):
        self._text = text
        self._data = data

    async def text(self):
        return self._text

    async def read(self):
        return self._data


class _MultipartReader:
    def __init__(self):
        self._fields = [
            _MultipartField(text="what is this"),
            _MultipartField(data=b"fake-image"),
        ]

    async def next(self):
        if not self._fields:
            return None
        return self._fields.pop(0)


class _VisionRequest:
    headers = {
        "Authorization": "Bearer token",
        "Device-Id": "device-1",
        "Client-Id": "client-1",
    }

    async def multipart(self):
        return _MultipartReader()


def test_vision_handler_closes_vllm_provider_after_success(monkeypatch):
    fake_vllm = SimpleNamespace(
        closed=False,
        response=lambda question, image_base64: "vision-result",
    )
    fake_vllm.close = lambda: setattr(fake_vllm, "closed", True)
    vision_handler = _install_vision_handler_fakes(monkeypatch, fake_vllm)

    handler = vision_handler.VisionHandler(
        {
            "server": {"auth_key": "secret"},
            "selected_module": {"VLLM": "OpenAI"},
            "VLLM": {"OpenAI": {"type": "openai"}},
        }
    )

    response = asyncio.run(handler.handle_post(_VisionRequest()))

    assert response.status == 200
    assert fake_vllm.closed is True


def test_vision_handler_closes_vllm_provider_after_response_error(monkeypatch):
    def _raise_response(question, image_base64):
        raise RuntimeError("simulated vision failure")

    fake_vllm = SimpleNamespace(closed=False, response=_raise_response)
    fake_vllm.close = lambda: setattr(fake_vllm, "closed", True)
    vision_handler = _install_vision_handler_fakes(monkeypatch, fake_vllm)

    handler = vision_handler.VisionHandler(
        {
            "server": {"auth_key": "secret"},
            "selected_module": {"VLLM": "OpenAI"},
            "VLLM": {"OpenAI": {"type": "openai"}},
        }
    )

    response = asyncio.run(handler.handle_post(_VisionRequest()))

    assert response.status == 200
    assert fake_vllm.closed is True


def test_vllm_openai_provider_closes_underlying_client(monkeypatch):
    _install_logger_fake(monkeypatch)
    util_module = types.ModuleType("core.utils.util")
    util_module.check_model_key = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "core.utils.util", util_module)
    vllm_openai = _fresh_import("core.providers.vllm.openai")

    fake_client = SimpleNamespace(close_count=0)
    fake_client.close = lambda: setattr(fake_client, "close_count", fake_client.close_count + 1)
    monkeypatch.setattr(vllm_openai.openai, "OpenAI", lambda *args, **kwargs: fake_client)

    provider = vllm_openai.VLLMProvider(
        {"model_name": "vision", "api_key": "key", "base_url": "https://example.test"}
    )
    provider.close()

    assert fake_client.close_count == 1


def test_websocket_handler_closes_fake_sockets_under_10k_and_100k_session_model(monkeypatch):
    class _FakeConnectionHandler:
        created = 0
        handled = 0

        def __init__(self, *args, **kwargs):
            type(self).created += 1

        async def handle_connection(self, websocket):
            type(self).handled += 1
            if websocket.index % 997 == 0:
                raise RuntimeError("simulated handler failure")

    _install_logger_fake(monkeypatch)
    connection_module = types.ModuleType("core.connection")
    connection_module.ConnectionHandler = _FakeConnectionHandler
    modules_initialize_module = types.ModuleType("core.utils.modules_initialize")
    modules_initialize_module.initialize_modules = lambda *args, **kwargs: {}
    util_module = types.ModuleType("core.utils.util")
    util_module.check_vad_update = lambda *args, **kwargs: False
    util_module.check_asr_update = lambda *args, **kwargs: False
    monkeypatch.setitem(sys.modules, "core.connection", connection_module)
    monkeypatch.setitem(
        sys.modules,
        "core.utils.modules_initialize",
        modules_initialize_module,
    )
    monkeypatch.setitem(sys.modules, "core.utils.util", util_module)

    websocket_server = _fresh_import("core.websocket_server")

    class _FakeSocket:
        def __init__(self, index):
            self.index = index
            self.closed = False
            self.close_count = 0

        async def close(self):
            self.close_count += 1
            self.closed = True

    monkeypatch.setattr(websocket_server, "ConnectionHandler", _FakeConnectionHandler)

    server = websocket_server.WebSocketServer.__new__(websocket_server.WebSocketServer)
    server.config = {"selected_module": {}, "server": {}}
    server._vad = None
    server._asr = None
    server._llm = None
    server._memory = None
    server._task = None
    server._intent = None
    server.active_connections = set()
    server.logger = _Logger()

    async def _exercise(total_sessions):
        for index in range(total_sessions):
            websocket = _FakeSocket(index)
            await server._handle_connection(websocket)
            assert websocket.closed is True
            assert websocket.close_count == 1
            if index % 2048 == 0:
                assert server.active_connections == set()
        assert server.active_connections == set()

    asyncio.run(_exercise(10_000))
    asyncio.run(_exercise(100_000))

    assert _FakeConnectionHandler.created == 110_000
    assert _FakeConnectionHandler.handled == 110_000
