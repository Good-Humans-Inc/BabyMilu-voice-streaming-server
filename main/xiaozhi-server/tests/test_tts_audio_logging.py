from __future__ import annotations

from types import SimpleNamespace

from core.providers.tts import base as tts_base


class _CaptureLogger:
    def __init__(self):
        self.records = []

    def bind(self, **kwargs):
        return _BoundLogger(self, kwargs)


class _BoundLogger:
    def __init__(self, parent, bindings):
        self._parent = parent
        self._bindings = bindings

    def debug(self, message, *_args, **_kwargs):
        self._parent.records.append(("debug", self._bindings, message))

    def warning(self, message, *_args, **_kwargs):
        self._parent.records.append(("warning", self._bindings, message))


class _StopEvent:
    def __init__(self, is_set=False):
        self._is_set = is_set

    def is_set(self):
        return self._is_set


class _TestTTSProvider(tts_base.TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return b""


def _provider(conn):
    provider = object.__new__(_TestTTSProvider)
    provider.conn = conn
    provider._audio_play_disconnect_logged = False
    return provider


def test_audio_play_close_error_is_debug_during_shutdown(monkeypatch):
    capture = _CaptureLogger()
    monkeypatch.setattr(tts_base, "logger", capture)
    conn = SimpleNamespace(
        device_id="device-a",
        session_id="session-a",
        headers={},
        _closing=True,
        stop_event=_StopEvent(False),
    )

    _provider(conn)._log_audio_play_exception(
        "hello", RuntimeError("no close frame received or sent")
    )

    assert len(capture.records) == 1
    level, bindings, message = capture.records[0]
    assert level == "debug"
    assert bindings["device_id"] == "device-a"
    assert bindings["session_id"] == "session-a"
    assert "device_id=device-a" in message
    assert "session_id=session-a" in message
    assert "send stopped during shutdown" in message


def test_audio_play_abrupt_disconnect_warns_once_with_header_device_id(monkeypatch):
    capture = _CaptureLogger()
    monkeypatch.setattr(tts_base, "logger", capture)
    conn = SimpleNamespace(
        device_id=None,
        session_id="session-b",
        headers={"device-id": "header-device"},
        _closing=False,
        stop_event=_StopEvent(False),
    )
    provider = _provider(conn)

    provider._log_audio_play_exception(
        "hello", RuntimeError("no close frame received or sent")
    )
    provider._log_audio_play_exception(
        "hello again", RuntimeError("no close frame received or sent")
    )

    assert len(capture.records) == 2
    first_level, first_bindings, first_message = capture.records[0]
    second_level, second_bindings, second_message = capture.records[1]
    assert first_level == "warning"
    assert first_bindings["device_id"] == "header-device"
    assert first_bindings["session_id"] == "session-b"
    assert "device disconnected abruptly" in first_message
    assert "device_id=header-device" in first_message
    assert second_level == "debug"
    assert second_bindings["device_id"] == "header-device"
    assert "device disconnected abruptly" in second_message


def test_audio_play_unexpected_send_failure_stays_warning(monkeypatch):
    capture = _CaptureLogger()
    monkeypatch.setattr(tts_base, "logger", capture)
    conn = SimpleNamespace(
        device_id="device-c",
        session_id="session-c",
        headers={},
        _closing=False,
        stop_event=_StopEvent(False),
    )

    _provider(conn)._log_audio_play_exception("hello", ValueError("boom"))

    assert len(capture.records) == 1
    level, bindings, message = capture.records[0]
    assert level == "warning"
    assert bindings["device_id"] == "device-c"
    assert "send failed" in message
    assert "ValueError: boom" in message
