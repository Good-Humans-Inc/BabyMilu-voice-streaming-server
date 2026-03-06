from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.providers.llm.openai.openai import LLMProvider


class _FakeResponses:
    def __init__(self):
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        idx = len(self.calls)
        return _FakeStream(idx)


class _FakeStream:
    def __init__(self, idx: int):
        self.idx = idx

    def __enter__(self):
        if self.idx == 1:
            raise Exception(
                "Error code: 404 - {'error': {'message': \"Conversation with id 'conv-old' not found.\"}}"
            )
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="response.completed")


def _build_provider():
    provider = LLMProvider.__new__(LLMProvider)
    provider.model_name = "gpt-4o-mini"
    provider.stateless_default = False
    provider._conversations = {}
    provider.client = SimpleNamespace(responses=_FakeResponses())
    return provider


def test_response_resets_stale_conversation_and_retries():
    provider = _build_provider()
    session_id = "s1"
    provider._conversations[session_id] = {"id": "conv-old"}

    def _ensure(session):
        state = provider._conversations.get(session)
        if state and state.get("id"):
            return state["id"]
        provider._conversations[session] = {"id": "conv-new"}
        return "conv-new"

    provider.ensure_conversation = _ensure  # type: ignore[method-assign]

    callbacks = []
    output = list(
        provider.response(
            session_id,
            [{"role": "user", "content": "hello"}],
            on_conversation_not_found=lambda: callbacks.append("reset"),
        )
    )

    assert output == []
    assert callbacks == ["reset"]
    assert provider.client.responses.calls[0]["conversation"] == "conv-old"
    assert provider.client.responses.calls[1]["conversation"] == "conv-new"


def test_response_with_functions_resets_stale_conversation_and_retries():
    provider = _build_provider()
    session_id = "s2"
    provider._conversations[session_id] = {"id": "conv-old"}

    def _ensure(session):
        state = provider._conversations.get(session)
        if state and state.get("id"):
            return state["id"]
        provider._conversations[session] = {"id": "conv-new"}
        return "conv-new"

    provider.ensure_conversation = _ensure  # type: ignore[method-assign]

    callbacks = []
    output = list(
        provider.response_with_functions(
            session_id,
            [{"role": "user", "content": "set reminder"}],
            functions=[],
            on_conversation_not_found=lambda: callbacks.append("reset"),
        )
    )

    assert output == []
    assert callbacks == ["reset"]
    assert provider.client.responses.calls[0]["conversation"] == "conv-old"
    assert provider.client.responses.calls[1]["conversation"] == "conv-new"
