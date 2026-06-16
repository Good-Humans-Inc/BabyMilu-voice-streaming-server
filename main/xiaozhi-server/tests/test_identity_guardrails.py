from __future__ import annotations

from types import SimpleNamespace

from core import connection as connection_mod
from core.connection import ConnectionHandler
from core.providers.llm.openai.openai import LLMProvider
from core.utils.identity_guardrails import (
    identity_text_has_drift,
    is_forced_self_introduction_query,
)
from core.utils.dialogue import Dialogue, Message


class _Logger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def _connection_stub(**attrs):
    conn = object.__new__(ConnectionHandler)
    for key, value in attrs.items():
        setattr(conn, key, value)
    return conn


def test_identity_queries_are_forced_to_character_path():
    assert is_forced_self_introduction_query("What's your name?")
    assert is_forced_self_introduction_query(
        "Can you introduce yourself?"
    )
    assert is_forced_self_introduction_query("Who are you?")
    assert not is_forced_self_introduction_query("What's my name?")
    assert not is_forced_self_introduction_query(
        "Can you rename my reminder?"
    )


def test_forced_identity_response_uses_character_name(monkeypatch):
    monkeypatch.setattr(
        connection_mod,
        "get_character_profile",
        lambda _char_id: {"fake": "doc"},
    )
    monkeypatch.setattr(
        connection_mod,
        "extract_character_profile_fields",
        lambda _doc: {
            "name": "Bakugo",
            "bio": "personality: Aggressive, competitive, and direct. | likes: spicy food",
        },
    )
    conn = _connection_stub(
        active_character_id="CH-1",
        current_character_id=None,
        device_id="device-1",
        user_name="Lulu",
        logger=_Logger(),
        prompt="",
    )

    name_reply = ConnectionHandler._build_forced_self_introduction_text(
        conn,
        "What's your name?",
    )
    intro_reply = ConnectionHandler._build_forced_self_introduction_text(
        conn,
        "Can you introduce yourself?",
    )

    assert "Bakugo" in name_reply
    assert "Bakugo" in intro_reply
    assert "Aggressive, competitive, and direct." in intro_reply
    assert not identity_text_has_drift(name_reply)
    assert not identity_text_has_drift(intro_reply)


def test_forced_identity_response_can_fall_back_to_prompt_name(monkeypatch):
    monkeypatch.setattr(connection_mod, "get_character_profile", lambda _char_id: None)
    conn = _connection_stub(
        active_character_id="CH-1",
        current_character_id=None,
        device_id="device-1",
        user_name="Unknown User",
        logger=_Logger(),
        prompt="# About you:\n- Your Name: Miffy\n- Your Age: 4",
    )

    reply = ConnectionHandler._build_forced_self_introduction_text(
        conn,
        "What's your name?",
    )

    assert "Miffy" in reply
    assert "AI assistant" not in reply


def test_stored_conversation_turns_include_current_system_prompt_in_instructions():
    conn = SimpleNamespace(
        prompt="SYSTEM: Your Name: Bakugo",
        persistent_mode_specific_instructions="MODE",
        mode_specific_instructions="ONE-SHOT",
    )

    instructions = ConnectionHandler._compose_llm_instructions(
        conn,
        "memory fact",
        use_full_history=False,
    )

    assert "SYSTEM: Your Name: Bakugo" in instructions
    assert "MODE" in instructions
    assert "ONE-SHOT" in instructions
    assert "<memory>\nmemory fact\n</memory>" in instructions
    assert conn.mode_specific_instructions == ""


def test_full_history_turns_do_not_duplicate_system_prompt_in_instructions():
    conn = SimpleNamespace(
        prompt="SYSTEM: Your Name: Bakugo",
        persistent_mode_specific_instructions="",
        mode_specific_instructions="",
    )

    instructions = ConnectionHandler._compose_llm_instructions(
        conn,
        None,
        use_full_history=True,
    )

    assert instructions == ""


def test_contaminated_identity_summary_is_not_persisted():
    conn = _connection_stub(
        dialogue=Dialogue(),
        logger=_Logger(),
    )
    conn.dialogue.put(Message(role="user", content="What's your name?"))
    conn.dialogue.put(
        Message(
            role="assistant",
            content="I don't have a personal name, but you can call me your friendly AI assistant.",
        )
    )

    assert ConnectionHandler._build_last_interaction_summary(conn) == ""


def test_provider_reset_conversation_seeds_system_prompt():
    provider = object.__new__(LLMProvider)
    provider._conversations = {"session-1": {"id": "old-conv"}}
    calls = []

    def ensure_with_system(session_id, system_text):
        calls.append((session_id, system_text))
        provider._conversations[session_id] = {"id": "new-conv"}
        return "new-conv"

    provider.ensure_conversation_with_system = ensure_with_system
    provider.ensure_conversation = lambda session_id: "fallback-conv"

    new_id = provider.reset_conversation("session-1", "SYSTEM PROMPT")

    assert new_id == "new-conv"
    assert calls == [("session-1", "SYSTEM PROMPT")]
    assert provider._conversations["session-1"]["id"] == "new-conv"
