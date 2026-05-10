from __future__ import annotations

import asyncio
import concurrent.futures
import pathlib
import queue
import sys
import threading
import types
from collections import deque
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class _ImportLogger:
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


_import_logger = _ImportLogger()
logger_stub = types.ModuleType("config.logger")
logger_stub.setup_logging = lambda: _import_logger
logger_stub.build_module_string = lambda _selected: "test"
logger_stub.create_connection_logger = lambda *_args, **_kwargs: _import_logger
sys.modules.setdefault("config.logger", logger_stub)
sys.modules.setdefault("opuslib_next", types.ModuleType("opuslib_next"))

modules_initialize_stub = types.ModuleType("core.utils.modules_initialize")
modules_initialize_stub.initialize_modules = lambda *_args, **_kwargs: {}
modules_initialize_stub.initialize_tts = lambda *_args, **_kwargs: None
modules_initialize_stub.initialize_asr = lambda *_args, **_kwargs: None
sys.modules.setdefault("core.utils.modules_initialize", modules_initialize_stub)

report_stub = types.ModuleType("core.handle.reportHandle")
report_stub.report = lambda *_args, **_kwargs: None
report_stub.enqueue_asr_report = lambda *_args, **_kwargs: None
report_stub.enqueue_tts_report = lambda *_args, **_kwargs: None
sys.modules.setdefault("core.handle.reportHandle", report_stub)

default_tts_stub = types.ModuleType("core.providers.tts.default")


class _ImportDefaultTTS:
    async def open_audio_channels(self, _conn):
        return None

    async def close(self):
        return None


default_tts_stub.DefaultTTS = _ImportDefaultTTS
sys.modules.setdefault("core.providers.tts.default", default_tts_stub)

text_handle_stub = types.ModuleType("core.handle.textHandle")


async def _import_handle_text_message(*_args, **_kwargs):
    return None


text_handle_stub.handleTextMessage = _import_handle_text_message
sys.modules.setdefault("core.handle.textHandle", text_handle_stub)

tool_handler_stub = types.ModuleType("core.providers.tools.unified_tool_handler")
tool_handler_stub.UnifiedToolHandler = type("UnifiedToolHandler", (), {})
sys.modules.setdefault("core.providers.tools.unified_tool_handler", tool_handler_stub)

prompt_manager_stub = types.ModuleType("core.utils.prompt_manager")


class _ImportPromptManager:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_quick_prompt(self, prompt, device_id=None):
        return prompt

    def update_context_info(self, *_args, **_kwargs):
        pass

    def build_enhanced_prompt(self, *_args, **_kwargs):
        return None


prompt_manager_stub.PromptManager = _ImportPromptManager
sys.modules.setdefault("core.utils.prompt_manager", prompt_manager_stub)

voiceprint_stub = types.ModuleType("core.utils.voiceprint_provider")
voiceprint_stub.VoiceprintProvider = lambda *_args, **_kwargs: None
sys.modules.setdefault("core.utils.voiceprint_provider", voiceprint_stub)

from core import connection as conn_mod
from core.connection import ConnectionHandler


def _base_config() -> dict:
    return {
        "server": {"auth": {"enabled": False}},
        "selected_module": {},
        "exit_commands": [],
        "xiaozhi": {},
        "prompt": "base prompt",
        "close_connection_no_voice_time": 120,
        "read_config_from_api": False,
        "delete_audio": True,
        "Memory": {},
        "LLM": {},
    }


class _NullLogger:
    def __init__(self):
        self.records = []

    def bind(self, **_kwargs):
        return self

    def debug(self, *args, **_kwargs):
        self.records.append(("debug", " ".join(map(str, args))))

    def info(self, *args, **_kwargs):
        self.records.append(("info", " ".join(map(str, args))))

    def warning(self, *args, **_kwargs):
        self.records.append(("warning", " ".join(map(str, args))))

    def error(self, *args, **_kwargs):
        self.records.append(("error", " ".join(map(str, args))))


class _PromptManager:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_quick_prompt(self, prompt, device_id=None):
        return prompt

    def update_context_info(self, *_args, **_kwargs):
        pass

    def build_enhanced_prompt(self, *_args, **_kwargs):
        return None


class _ChatStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_or_create_user(self, *_args, **_kwargs):
        pass

    def ensure_character_memory_record(self, *_args, **_kwargs):
        pass

    def create_session(self, *_args, **_kwargs):
        pass

    def update_session_conversation_id(self, *_args, **_kwargs):
        pass

    def delete_session(self, *_args, **_kwargs):
        pass

    def end_session(self, *_args, **_kwargs):
        pass

    def get_system_memory_block(self, *_args, **_kwargs):
        return ""


class _Auth:
    def __init__(self, *_args, **_kwargs):
        pass

    async def authenticate(self, _headers):
        return True


class _TemplateASR:
    interface_type = object()


@pytest.fixture
def connection_fakes(monkeypatch):
    logger = _NullLogger()

    monkeypatch.setattr(conn_mod, "setup_logging", lambda: logger)
    monkeypatch.setattr(conn_mod, "create_connection_logger", lambda *a, **kw: logger)
    monkeypatch.setattr(conn_mod, "build_module_string", lambda _selected: "test")
    monkeypatch.setattr(conn_mod, "PromptManager", _PromptManager)
    monkeypatch.setattr(conn_mod, "ChatStore", _ChatStore)
    monkeypatch.setattr(conn_mod, "AuthMiddleware", _Auth)

    monkeypatch.setattr(conn_mod, "get_active_character_for_device", lambda _device_id: None)
    monkeypatch.setattr(
        conn_mod, "get_most_recent_character_via_user_for_device", lambda _device_id: None
    )
    monkeypatch.setattr(conn_mod, "get_character_profile", lambda _character_id: {})
    monkeypatch.setattr(conn_mod, "extract_character_profile_fields", lambda doc: doc or {})
    monkeypatch.setattr(conn_mod, "get_owner_phone_for_device", lambda _device_id: None)
    monkeypatch.setattr(conn_mod, "get_user_profile_by_phone", lambda _phone: {})
    monkeypatch.setattr(conn_mod, "extract_user_profile_fields", lambda doc: doc or {})
    monkeypatch.setattr(conn_mod, "query_task", lambda *a, **kw: "")
    monkeypatch.setattr(conn_mod, "get_ready_next_starter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        conn_mod.session_context_store, "get_session", lambda _device_id: None
    )

    return logger


def _make_handler(executors=None) -> ConnectionHandler:
    return ConnectionHandler(
        _base_config(),
        _vad=object(),
        _asr=_TemplateASR(),
        _llm=None,
        _memory=None,
        _task=None,
        _intent=None,
        server=None,
        executors=executors,
    )


def _executor_queue_limit(executor) -> int:
    for attr in ("max_queue_size", "_max_queue_size", "queue_maxsize"):
        value = getattr(executor, attr, None)
        if isinstance(value, int):
            return value

    work_queue = getattr(executor, "_work_queue", None)
    maxsize = getattr(work_queue, "maxsize", None)
    return maxsize if isinstance(maxsize, int) else 0


def _find_audio_queue(handler):
    for name in ("early_audio_queue", "_early_audio_queue", "asr_audio_queue"):
        if hasattr(handler, name):
            return name, getattr(handler, name)
    raise AssertionError("connection handler has no audio ingress queue")


def _queue_limit(audio_queue) -> int:
    maxsize = getattr(audio_queue, "maxsize", None)
    return maxsize if isinstance(maxsize, int) else 0


def test_connection_handlers_use_one_shared_bounded_executor(connection_fakes):
    executors = conn_mod.ServerExecutors.from_config(_base_config())
    handler_one = _make_handler(executors=executors)
    handler_two = _make_handler(executors=executors)

    assert handler_one.executors is handler_two.executors is executors
    assert handler_one.executor is handler_two.executor is executors.provider
    assert _executor_queue_limit(handler_one.executor) > 0


def test_websocket_receive_loop_starts_before_slow_hydration(monkeypatch, connection_fakes):
    hydration_started = threading.Event()
    release_hydration = threading.Event()
    receive_started = threading.Event()
    release_receive = threading.Event()
    handler_ready = threading.Event()
    thread_errors = []
    holder = {}

    def slow_character_lookup(_device_id):
        hydration_started.set()
        release_hydration.wait(timeout=2)
        return None

    monkeypatch.setattr(conn_mod, "get_active_character_for_device", slow_character_lookup)

    class _FakeWebSocket:
        def __init__(self):
            self.request = SimpleNamespace(
                headers={"device-id": "AA:BB:CC:DD:EE:FF"},
                path="/xiaozhi",
            )
            self.remote_address = ("127.0.0.1", 12345)
            self.closed = False
            self._sent_audio = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            receive_started.set()
            if not self._sent_audio:
                self._sent_audio = True
                return b"early-audio"
            while not release_receive.is_set():
                await asyncio.sleep(0.005)
            raise StopAsyncIteration

        async def send(self, _payload):
            pass

        async def close(self):
            self.closed = True

    ws = _FakeWebSocket()

    async def _run_connection():
        handler = _make_handler()
        handler.vad = object()
        handler.asr = object()
        handler._initialize_components = lambda: None

        def _slow_profile_bootstrap():
            hydration_started.set()
            release_hydration.wait(timeout=2)

        handler._run_profile_bootstrap = _slow_profile_bootstrap

        async def _save_and_close(_ws):
            pass

        handler._save_and_close = _save_and_close
        holder["handler"] = handler
        handler_ready.set()
        await handler.handle_connection(ws)

    def _thread_main():
        try:
            asyncio.run(_run_connection())
        except BaseException as exc:
            thread_errors.append(exc)

    thread = threading.Thread(target=_thread_main, daemon=True)
    thread.start()

    try:
        assert handler_ready.wait(timeout=1)
        assert receive_started.wait(timeout=0.2)

        _queue_name, audio_queue = _find_audio_queue(holder["handler"])
        queued = audio_queue.get_nowait()
        assert queued == b"early-audio"
        assert not release_hydration.is_set()
    finally:
        release_hydration.set()
        release_receive.set()
        thread.join(timeout=1)

    assert thread_errors == []


class _TinyNonBlockingQueue:
    maxsize = 2

    def __init__(self):
        self.items = []

    def put(self, item, block=True, timeout=None):
        if len(self.items) >= self.maxsize:
            self.items.pop(0)
        self.items.append(item)

    def put_nowait(self, item):
        self.put(item, block=False)

    def get_nowait(self):
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)

    def qsize(self):
        return len(self.items)


def test_early_audio_queue_is_bounded_and_keeps_newest_audio(connection_fakes):
    async def _scenario():
        handler = _make_handler()
        handler.vad = None
        handler.asr = None
        handler.conn_from_mqtt_gateway = False

        queue_name, initial_queue = _find_audio_queue(handler)
        assert _queue_limit(initial_queue) > 0

        tiny_queue = _TinyNonBlockingQueue()
        setattr(handler, queue_name, tiny_queue)

        for frame in (b"frame-0", b"frame-1", b"frame-2"):
            await handler._route_message(frame)

        assert tiny_queue.qsize() == tiny_queue.maxsize
        assert b"frame-2" in tiny_queue.items

    asyncio.run(_scenario())


class _ChannelProvider:
    async def open_audio_channels(self, _conn):
        return None


class _BoundaryFuture:
    def __init__(self, exc):
        self.exc = exc
        self.result_timeouts = []
        self.cancelled = False

    def result(self, timeout=None):
        self.result_timeouts.append(timeout)
        raise self.exc

    def cancel(self):
        self.cancelled = True
        return True


@pytest.mark.parametrize(
    ("future_exc", "expected_log", "expect_cancel"),
    [
        (concurrent.futures.TimeoutError(), "timeout", True),
        (RuntimeError("open failed"), "open failed", False),
    ],
)
def test_component_channel_boundary_timeout_and_errors_are_observed(
    monkeypatch,
    connection_fakes,
    future_exc,
    expected_log,
    expect_cancel,
):
    async def _scenario():
        futures = []

        def fake_run_coroutine_threadsafe(coro, _loop):
            coro.close()
            future = _BoundaryFuture(future_exc)
            futures.append(future)
            return future

        monkeypatch.setattr(
            conn_mod.asyncio,
            "run_coroutine_threadsafe",
            fake_run_coroutine_threadsafe,
        )

        handler = _make_handler()
        handler.device_id = "aa:bb:cc:dd:ee:ff"
        handler.client_ip = "127.0.0.1"
        handler.asr = _ChannelProvider()
        handler.tts = _ChannelProvider()
        handler._initialize_voiceprint = lambda: None
        handler._ensure_per_connection_providers = lambda: None
        handler._initialize_memory = lambda: None
        handler._initialize_intent = lambda: None
        handler._init_report_threads = lambda: None
        handler._init_prompt_enhancement = lambda: None

        handler._initialize_components()
        await asyncio.sleep(0)

        assert futures
        assert all(future.result_timeouts for future in futures)
        if expect_cancel:
            assert all(future.cancelled for future in futures)

        logs = "\n".join(message for _level, message in connection_fakes.records).lower()
        if isinstance(future_exc, concurrent.futures.TimeoutError):
            assert "timeout" in logs or "timed out" in logs
        else:
            assert expected_log in logs
        assert handler.components_initialized.is_set()

    asyncio.run(_scenario())


def test_shared_silero_vad_uses_per_connection_opus_decoders(monkeypatch):
    class FakeScore:
        def item(self):
            return 0.9

    class FakeModel:
        def __call__(self, _audio_tensor, _sample_rate):
            return FakeScore()

    class FakeNoGrad:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    torch_stub = types.ModuleType("torch")
    torch_stub.hub = SimpleNamespace(load=lambda **_kwargs: (FakeModel(), None))
    torch_stub.from_numpy = lambda audio: audio
    torch_stub.no_grad = lambda: FakeNoGrad()
    monkeypatch.setitem(sys.modules, "torch", torch_stub)

    from core.providers.vad import silero

    created_decoders = []

    class FakeDecoder:
        def __init__(self, sample_rate, channels):
            self.sample_rate = sample_rate
            self.channels = channels
            self.decode_calls = []
            created_decoders.append(self)

        def decode(self, packet, frame_size):
            self.decode_calls.append((packet, frame_size))
            return b"\x01\x00" * 512

    monkeypatch.setattr(silero.opuslib_next, "Decoder", FakeDecoder, raising=False)
    monkeypatch.setattr(silero.opuslib_next, "OpusError", Exception, raising=False)

    provider = silero.VADProvider({"model_dir": "unused"})
    provider.frame_window_threshold = 1

    def make_conn():
        return SimpleNamespace(
            client_audio_buffer=bytearray(),
            client_have_voice=False,
            client_voice_window=deque(maxlen=5),
            last_activity_time=0,
            client_voice_stop=False,
            last_is_voice=False,
        )

    conn_a = make_conn()
    conn_b = make_conn()

    assert provider.is_vad(conn_a, b"frame-a") is True
    assert provider.is_vad(conn_b, b"frame-b") is True
    assert provider.is_vad(conn_a, b"frame-a2") is True

    assert len(created_decoders) == 2
    assert conn_a._vad_opus_decoder is created_decoders[0]
    assert conn_b._vad_opus_decoder is created_decoders[1]
    assert conn_a._vad_opus_decoder is not conn_b._vad_opus_decoder
    assert not hasattr(provider, "decoder")
