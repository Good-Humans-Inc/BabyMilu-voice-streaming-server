from __future__ import annotations

from datetime import datetime, timezone

from core.connection import ConnectionHandler, ModeRuntimeState, FollowupState
from services.session_context import models as session_models


class _DummyLogger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


def _build_conn(mode_config: dict, monkeypatch) -> ConnectionHandler:
    monkeypatch.setattr("core.connection.MODE_CONFIG", {"morning_alarm": mode_config})
    conn = ConnectionHandler.__new__(ConnectionHandler)
    conn.config = {"mode_config": {"morning_alarm": mode_config}}
    conn.logger = _DummyLogger()
    conn._mode_state = ModeRuntimeState()
    conn._followup_state = FollowupState()
    conn.mode_session = session_models.ModeSession(
        device_id="DEV1",
        session_type="alarm",
        triggered_at=datetime.now(timezone.utc),
        ttl_seconds=300,
        session_config={
            "mode": "morning_alarm",
        },
    )
    return conn


def test_apply_mode_session_settings_loads_config(monkeypatch):
    conn = _build_conn(
        {
            "instructions": "WAKE UP",
            "server_initiate_chat": True,
            "followup_enabled": True,
            "followup_delay": 5,
            "followup_max": 2,
        },
        monkeypatch,
    )

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode == "morning_alarm"
    assert conn.mode_specific_instructions == "WAKE UP"
    assert conn.server_initiate_chat is True
    assert conn.followup_enabled is True
    assert conn.followup_delay == 5
    assert conn.followup_max == 2


def test_apply_mode_session_settings_without_mode_noops(monkeypatch):
    conn = _build_conn({"instructions": "unused"}, monkeypatch)
    conn.mode_session.session_config = {}

    ConnectionHandler._apply_mode_session_settings(conn)

    assert conn.active_mode is None
    assert conn.mode_specific_instructions == ""

