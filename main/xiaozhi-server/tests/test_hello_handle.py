from __future__ import annotations

from core.handle.helloHandle import (
    _build_server_initiated_query,
    _get_precomputed_reminder_message,
)


class _Conn:
    def __init__(self, session_config):
        self.mode_session = type(
            "ModeSessionStub",
            (),
            {"session_config": session_config},
        )()


def test_build_server_initiated_query_uses_context_for_scheduled_conversations():
    query = _build_server_initiated_query(
        _Conn({"mode": "scheduled_conversation", "context": "take vitamins"})
    )
    assert "take vitamins" in query
    assert "first spoken sentence must already contain that reminder reason" in query


def test_build_server_initiated_query_falls_back_to_title_then_label():
    title_query = _build_server_initiated_query(
        _Conn({"mode": "scheduled_conversation", "context": None, "title": "scheduler test later"})
    )
    assert "scheduler test later" in title_query

    label_query = _build_server_initiated_query(
        _Conn({"mode": "scheduled_conversation", "context": None, "title": None, "label": "drink water"})
    )
    assert "drink water" in label_query


def test_build_server_initiated_query_noops_for_non_scheduled_conversation_mode():
    assert _build_server_initiated_query(_Conn({"mode": "morning_alarm"})) == ""


def test_get_precomputed_reminder_message_ignores_scheduled_conversations():
    assert (
        _get_precomputed_reminder_message(
            _Conn({"mode": "scheduled_conversation", "firstMessage": "Don't forget your test later."})
        )
        == ""
    )
