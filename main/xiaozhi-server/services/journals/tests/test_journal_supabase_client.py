from services.journals.supabase_client import JournalSupabaseClient


def test_memory_event_default_prefers_plural_camel_table(monkeypatch):
    monkeypatch.delenv("SUPABASE_CHARACTER_MEMORY_EVENT_TABLE", raising=False)
    monkeypatch.delenv("SUPABASE_MEMORY_EVENT_TABLE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    client = JournalSupabaseClient()

    assert client._memory_event_table_candidates()[0] == (
        "character_memory_events",
        "camel",
    )


def test_legacy_memory_event_env_is_fallback_not_first(monkeypatch):
    monkeypatch.delenv("SUPABASE_CHARACTER_MEMORY_EVENT_TABLE", raising=False)
    monkeypatch.setenv("SUPABASE_MEMORY_EVENT_TABLE", "character_memory_event")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    client = JournalSupabaseClient()

    assert client._memory_event_table_candidates()[:2] == [
        ("character_memory_events", "camel"),
        ("character_memory_event", "snake"),
    ]


def test_specific_memory_event_env_is_respected(monkeypatch):
    monkeypatch.setenv("SUPABASE_CHARACTER_MEMORY_EVENT_TABLE", "custom_events")
    monkeypatch.setenv("SUPABASE_MEMORY_EVENT_TABLE", "character_memory_event")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    client = JournalSupabaseClient()

    assert client._memory_event_table_candidates()[0] == ("custom_events", "camel")


def test_get_journal_memory_events_reads_plural_camel_table(monkeypatch):
    monkeypatch.delenv("SUPABASE_CHARACTER_MEMORY_EVENT_TABLE", raising=False)
    monkeypatch.delenv("SUPABASE_MEMORY_EVENT_TABLE", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")

    calls = []

    class FakeClient(JournalSupabaseClient):
        def _request(self, method, table, query="", **kwargs):
            calls.append((method, table, query))
            return [
                {
                    "eventId": "event-1",
                    "eventType": "journal_written",
                    "userId": "u1",
                    "characterId": "c1",
                    "content": {"journalEntryId": "entry-1"},
                }
            ]

    rows = FakeClient().get_journal_memory_events("u1", character_id="c1", limit=5)

    assert calls == [
        (
            "GET",
            "character_memory_events",
            "?userId=eq.u1&characterId=eq.c1&eventType=eq.journal_written&select=*&order=created_at.desc&limit=5",
        )
    ]
    assert rows[0]["event_id"] == "event-1"
    assert rows[0]["event_type"] == "journal_written"
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["character_id"] == "c1"
