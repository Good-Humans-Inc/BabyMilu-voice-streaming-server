from __future__ import annotations

import ast
import importlib
import os
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONNECTION_PATH = ROOT / "core" / "connection.py"


class _NullLogger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class _FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocumentRef:
    def __init__(self, client, collection_name, doc_id):
        self.client = client
        self.collection_name = collection_name
        self.doc_id = doc_id

    def get(self, timeout=None):
        self.client.get_calls.append(
            (self.collection_name, self.doc_id, timeout)
        )
        return _FakeDoc(
            self.client.docs.get(self.collection_name, {}).get(self.doc_id)
        )

    def set(self, payload, merge=False, timeout=None):
        self.client.set_calls.append(
            {
                "collection": self.collection_name,
                "doc_id": self.doc_id,
                "payload": payload,
                "merge": merge,
                "timeout": timeout,
            }
        )


class _FakeCollection:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def document(self, doc_id):
        return _FakeDocumentRef(self.client, self.name, doc_id)


class _FakeClient:
    project = "fake-project"

    def __init__(self, docs):
        self.docs = docs
        self.get_calls = []
        self.set_calls = []

    def collection(self, name):
        return _FakeCollection(self, name)


@pytest.fixture(autouse=True)
def _clear_reloaded_firestore_client():
    yield
    sys.modules.pop("core.utils.firestore_client", None)
    sys.modules.pop("core.utils.memory", None)


def _load_firestore_client_with_fake_firestore(monkeypatch):
    firestore_stub = types.ModuleType("google.cloud.firestore")
    firestore_stub.DELETE_FIELD = object()

    class Client:
        pass

    firestore_stub.Client = Client

    cloud_stub = types.ModuleType("google.cloud")
    cloud_stub.firestore = firestore_stub
    google_stub = types.ModuleType("google")
    google_stub.cloud = cloud_stub

    settings_stub = types.ModuleType("config.settings")
    settings_stub.get_gcp_credentials_path = lambda: None

    logger_stub = types.ModuleType("config.logger")
    logger_stub.setup_logging = lambda: _NullLogger()

    monkeypatch.setitem(sys.modules, "google", google_stub)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_stub)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", firestore_stub)
    monkeypatch.setitem(sys.modules, "config.settings", settings_stub)
    monkeypatch.setitem(sys.modules, "config.logger", logger_stub)
    sys.modules.pop("core.utils.firestore_client", None)

    return importlib.import_module("core.utils.firestore_client")


def test_firestore_device_id_case_fallback_reads_and_writes_resolved_doc(
    monkeypatch,
):
    firestore_client = _load_firestore_client_with_fake_firestore(monkeypatch)
    client = _FakeClient(
        {
            "devices": {
                "ABC123": {
                    "activeCharacterId": "char_miffy",
                    "conversation": {"id": "conv_existing"},
                }
            }
        }
    )
    monkeypatch.setattr(firestore_client, "_build_client", lambda: client)

    active_character = firestore_client.get_active_character_for_device(
        "abc123", timeout=1.5
    )
    conversation = firestore_client.get_conversation_state_for_device(
        "abc123", timeout=2.0
    )
    ok = firestore_client.update_conversation_state_for_device(
        "abc123",
        conversation_id="conv_new",
        last_used="2026-05-01T12:00:00+00:00",
        timeout=2.5,
    )

    assert active_character == "char_miffy"
    assert conversation == {"id": "conv_existing"}
    assert ok is True
    assert ("devices", "abc123", 1.5) in client.get_calls
    assert ("devices", "ABC123", 1.5) in client.get_calls
    assert client.set_calls[-1]["collection"] == "devices"
    assert client.set_calls[-1]["doc_id"] == "ABC123"
    assert client.set_calls[-1]["merge"] is True
    assert client.set_calls[-1]["payload"]["conversation"] == {
        "id": "conv_new",
        "last_used": "2026-05-01T12:00:00+00:00",
    }
    assert (
        client.set_calls[-1]["payload"]["conversationId"]
        is firestore_client.firestore.DELETE_FIELD
    )


def test_missing_memory_provider_falls_back_to_nomem(monkeypatch):
    logger_stub = types.ModuleType("config.logger")
    logger_stub.setup_logging = lambda: _NullLogger()
    monkeypatch.setitem(sys.modules, "config.logger", logger_stub)
    sys.modules.pop("core.utils.memory", None)

    memory = importlib.import_module("core.utils.memory")

    imported = []
    nomem_lib_name = "core.providers.memory.nomem.nomem"

    class FakeMemoryProvider:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_nomem_module = types.SimpleNamespace(MemoryProvider=FakeMemoryProvider)

    def fake_exists(path):
        return path == os.path.join("core", "providers", "memory", "nomem", "nomem.py")

    def fake_import_module(name):
        imported.append(name)
        assert name == nomem_lib_name
        return fake_nomem_module

    monkeypatch.setattr(memory.os.path, "exists", fake_exists)
    monkeypatch.setattr(memory.importlib, "import_module", fake_import_module)
    monkeypatch.delitem(sys.modules, nomem_lib_name, raising=False)

    provider = memory.create_instance("missing_provider", {"enabled": True}, key="v")

    assert isinstance(provider, FakeMemoryProvider)
    assert provider.args == ({"enabled": True},)
    assert provider.kwargs == {"key": "v"}
    assert imported == [nomem_lib_name]


def _connection_tree():
    return ast.parse(CONNECTION_PATH.read_text(encoding="utf-8"))


def _call_name(call_node):
    func = call_node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_cached_prompt_and_per_turn_refresh_paths_are_not_executable():
    calls = {
        _call_name(node)
        for node in ast.walk(_connection_tree())
        if isinstance(node, ast.Call)
    }

    assert "get_cached_enhanced_prompt" not in calls
    assert "invalidate_device_prompt_cache" not in calls
    assert "_refresh_character_binding_if_needed" not in calls


def test_device_scoped_conversation_binding_is_not_short_circuited():
    tree = _connection_tree()
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ConnectionHandler"
    )
    method = next(
        node
        for node in klass.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_ensure_device_scoped_conversation"
    )

    first_statement = method.body[0]
    assert not isinstance(first_statement, ast.Return)

    first_statement_calls = {
        _call_name(node)
        for node in ast.walk(first_statement)
        if isinstance(node, ast.Call)
    }
    assert "get_conversation_state_for_device" in first_statement_calls
