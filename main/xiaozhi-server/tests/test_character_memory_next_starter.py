import threading
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_ensure_character_memory_record_posts_create_when_row_missing(monkeypatch):
    from core.utils import next_starter_client as client

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("SUPABASE_CHARACTER_MEMORY_TABLE", "character_memory_model")

    calls = {}

    class Response:
        def __init__(self, payload=None):
            self._payload = payload or []

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        calls["get_url"] = url
        calls["get_headers"] = headers
        calls["get_timeout"] = timeout
        return Response([])

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["json"] = json
        calls["timeout"] = timeout
        return Response()

    monkeypatch.setattr(client.requests, "get", fake_get)
    monkeypatch.setattr(client.requests, "post", fake_post)

    ok = client.ensure_character_memory_record(
        "char_123",
        owner_user_id="+15551234567",
        last_device_id="90:e5:b1:00:00:01",
    )

    assert ok is True
    assert calls["url"].startswith(
        "https://example.supabase.co/rest/v1/character_memory_model"
    )
    assert "on_conflict=character_id" in calls["url"]
    assert calls["json"]["character_id"] == "char_123"
    assert calls["json"]["owner_user_id"] == "+15551234567"
    assert calls["json"]["last_device_id"] == "90:e5:b1:00:00:01"
    assert calls["json"]["next_starter"] is None
    assert calls["json"]["starter_fallback"] is None


def test_ensure_character_memory_record_patches_existing_row_without_clearing_starter(
    monkeypatch,
):
    from core.utils import next_starter_client as client

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("SUPABASE_CHARACTER_MEMORY_TABLE", "character_memory_model")

    calls = {}

    class Response:
        def __init__(self, payload=None):
            self._payload = payload or []

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        calls["get_url"] = url
        return Response([{"character_id": "char_123"}])

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls["patch_url"] = url
        calls["patch_headers"] = headers
        calls["patch_json"] = json
        calls["patch_timeout"] = timeout
        return Response()

    monkeypatch.setattr(client.requests, "get", fake_get)
    monkeypatch.setattr(client.requests, "patch", fake_patch)

    ok = client.ensure_character_memory_record(
        "char_123",
        owner_user_id="+15551234567",
        last_device_id="90:e5:b1:00:00:01",
    )

    assert ok is True
    assert calls["patch_url"] == (
        "https://example.supabase.co/rest/v1/character_memory_model"
        "?character_id=eq.char_123"
    )
    assert calls["patch_json"]["owner_user_id"] == "+15551234567"
    assert calls["patch_json"]["last_device_id"] == "90:e5:b1:00:00:01"
    assert "next_starter" not in calls["patch_json"]


def test_character_switch_refresh_reloads_next_starter(monkeypatch):
    import core.connection as conn_mod

    monkeypatch.setattr(
        conn_mod, "get_active_character_for_device", lambda did: "char_bob"
    )
    monkeypatch.setattr(
        conn_mod, "get_most_recent_character_via_user_for_device", lambda did: None
    )
    monkeypatch.setattr(
        conn_mod,
        "get_character_profile",
        lambda cid: {"voice": "voice_bob"} if cid == "char_bob" else {},
    )
    monkeypatch.setattr(
        conn_mod, "get_owner_phone_for_device", lambda did: "+15551234567"
    )
    monkeypatch.setattr(conn_mod, "get_user_profile_by_phone", lambda ph: {"name": "Lolo"})
    monkeypatch.setattr(
        conn_mod,
        "extract_user_profile_fields",
        lambda doc: {
            "name": doc.get("name"),
            "birthday": None,
            "pronouns": None,
            "phoneNumber": "+15551234567",
            "uid": "uid_1",
            "timezone": "America/Los_Angeles",
        },
    )
    monkeypatch.setattr(conn_mod, "extract_character_profile_fields", lambda doc: doc)
    monkeypatch.setattr(conn_mod, "query_task", lambda *a, **kw: "")
    monkeypatch.setattr(
        conn_mod,
        "get_ready_next_starter",
        lambda cid: {"status": "ready", "audioUrl": "https://cdn.test/starter.mp3", "text": "Hi"} if cid == "char_bob" else None,
    )

    class FakePromptManager:
        def invalidate_device_prompt_cache(self, device_id):
            return 1

        def get_quick_prompt(self, prompt, device_id=None):
            return prompt

        def build_enhanced_prompt(self, prompt, device_id, client_ip=None):
            return prompt + " [enhanced]"

    class FakeChatStore:
        def __init__(self):
            self.calls = []

        def ensure_character_memory_record(self, character_id, **kwargs):
            self.calls.append((character_id, kwargs))
            return True

    class FakeLogger:
        def bind(self, **kwargs):
            return self

        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

    conn = SimpleNamespace()
    conn.device_id = "device_aaa"
    conn.current_character_id = "char_alice"
    conn.active_character_id = "char_alice"
    conn.voice_id = "voice_alice"
    conn.client_ip = "127.0.0.1"
    conn.common_config = {"prompt": "base prompt"}
    conn.config = {"prompt": "base prompt"}
    conn.prompt_manager = FakePromptManager()
    conn.chat_store = FakeChatStore()
    conn.logger = FakeLogger()
    conn._profile_refresh_lock = threading.RLock()
    conn._last_profile_refresh_ms = 0
    conn._profile_refresh_interval_ms = 0
    conn.change_system_prompt = lambda prompt, prompt_label=None: None
    conn.user_id = "+15551234567"
    conn.next_starter_payload = {"status": "ready", "audioUrl": "stale"}
    conn.next_starter_scheduled = True

    conn_mod.ConnectionHandler._refresh_character_binding_if_needed(conn, force=True)

    assert conn.current_character_id == "char_bob"
    assert conn.active_character_id == "char_bob"
    assert conn.voice_id == "voice_bob"
    assert conn.next_starter_scheduled is False
    assert conn.next_starter_payload == {
        "status": "ready",
        "audioUrl": "https://cdn.test/starter.mp3",
        "text": "Hi",
    }
    assert conn.chat_store.calls == [
        (
            "char_bob",
            {
                "owner_user_id": "+15551234567",
                "device_id": "device_aaa",
            },
        )
    ]


def test_get_ready_next_starter_allows_text_only_payload(monkeypatch):
    from core.utils import next_starter_client as client

    monkeypatch.setenv("NEXT_STARTER_MAX_AGE_DAYS", "7")
    monkeypatch.setattr(
        client,
        "_fetch_next_starter_row",
        lambda cid: (
            {
                "next_starter": {
                    "status": "ready",
                    "characterId": cid,
                    "text": "Hey, tell me more.",
                    "generatedAt": "2026-05-01T07:00:00+00:00",
                    "sourceSessionId": "sess_1",
                }
            },
            "https://example.supabase.co",
            "service-role",
            2.0,
        ),
    )

    payload = client.get_ready_next_starter("char_123")

    assert payload is not None
    assert payload["text"] == "Hey, tell me more."


def test_get_ready_next_starter_falls_back_to_hi_audio(monkeypatch):
    from core.utils import next_starter_client as client

    monkeypatch.setenv("NEXT_STARTER_MAX_AGE_DAYS", "7")
    monkeypatch.setenv("STARTER_FALLBACK_MAX_AGE_DAYS", "3650")
    monkeypatch.setattr(
        client,
        "_fetch_next_starter_row",
        lambda cid: (
            {
                "next_starter": None,
                "starter_fallback": {
                    "status": "ready",
                    "characterId": cid,
                    "text": "Hi, how's everything going?",
                    "audioUrl": "https://example.supabase.co/storage/v1/object/public/next-starter-audio/char_123/fallback-hi.mp3",
                    "audioFormat": "mp3",
                    "generatedAt": "2026-05-01T07:00:00+00:00",
                    "sourceType": "fallback_hi",
                },
            },
            "https://example.supabase.co",
            "service-role",
            2.0,
        ),
    )

    payload = client.get_ready_next_starter("char_123")

    assert payload is not None
    assert payload["sourceType"] == "fallback_hi"
    assert payload["audioUrl"].endswith("fallback-hi.mp3")


def test_mark_next_starter_consumed_ignores_hi_fallback():
    from core.utils import next_starter_client as client

    ok = client.mark_next_starter_consumed(
        "char_123",
        {
            "status": "ready",
            "characterId": "char_123",
            "sourceType": "fallback_hi",
            "generatedAt": "2026-05-01T07:00:00+00:00",
        },
    )

    assert ok is False
