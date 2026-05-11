from datetime import datetime, timezone

from services.journals import jobs


class FakeRef:
    def __init__(self, path="users/u1/characters/c1/journal_session_state/s1"):
        self.path = path
        self.set_calls = []
        self.parent = None

    def set(self, payload, merge=False):
        self.set_calls.append((payload, merge))


class FakeMarker:
    id = "s1"

    def __init__(self, payload):
        self.payload = payload
        self.reference = FakeRef()

    def to_dict(self):
        return dict(self.payload)


class FakeSupabase:
    def __init__(self, session=None, turns=None):
        self.session = session or {
            "session_id": "s1",
            "memory_status": "done",
            "start_time": "2026-05-10T12:00:00+00:00",
            "end_time": "2026-05-10T12:30:00+00:00",
        }
        self.turns = turns or []

    def get_session(self, session_id):
        return self.session

    def get_turns(self, session_id):
        return self.turns

    def get_journal_memory_events(self, user_id, limit=3):
        return []

    def get_memory_events_since(self, *args, **kwargs):
        return []

    def get_recent_memory_events(self, user_id, limit=5):
        return []


def _enable_processing(monkeypatch):
    monkeypatch.setattr(jobs.config, "processing_enabled", lambda: True)
    monkeypatch.setattr(jobs.config, "max_ready_sessions", lambda: 10)


def test_process_skips_short_session(monkeypatch):
    _enable_processing(monkeypatch)
    marker = FakeMarker(
        {
            "userId": "u1",
            "characterId": "c1",
            "sessionId": "s1",
            "userTurnCount": 2,
            "status": "waiting_memory",
        }
    )
    monkeypatch.setattr(jobs.store, "fetch_waiting_session_markers", lambda client=None, limit=50: [marker])

    result = jobs.process_journal_ready_sessions(
        execute=True,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
        client=object(),
        supabase=FakeSupabase(),
    )

    assert result["results"][0]["reason"] == "too_few_turns"
    assert marker.reference.set_calls[0][0]["status"] == "skipped"


def test_process_dry_run_does_not_increment_turn_counter(monkeypatch):
    _enable_processing(monkeypatch)
    marker = FakeMarker(
        {
            "userId": "u1",
            "characterId": "c1",
            "sessionId": "s1",
            "userTurnCount": 10,
            "status": "waiting_memory",
        }
    )
    monkeypatch.setattr(jobs.store, "fetch_waiting_session_markers", lambda client=None, limit=50: [marker])
    monkeypatch.setattr(jobs.store, "get_turn_counter", lambda client, user_id, character_id: 0)
    monkeypatch.setattr(
        jobs.store,
        "add_turns_to_counter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write in dry run")),
    )

    result = jobs.process_journal_ready_sessions(
        execute=False,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
        client=object(),
        supabase=FakeSupabase(),
    )

    assert result["results"][0]["reason"] == "below_turn_threshold"


def test_first_journal_queues_without_classification(monkeypatch):
    _enable_processing(monkeypatch)
    marker = FakeMarker(
        {
            "userId": "u1",
            "characterId": "c1",
            "sessionId": "s1",
            "userTurnCount": 20,
            "status": "waiting_memory",
        }
    )
    queued = {}
    monkeypatch.setattr(jobs.store, "fetch_waiting_session_markers", lambda client=None, limit=50: [marker])
    monkeypatch.setattr(jobs.store, "add_turns_to_counter", lambda client, user_id, character_id, user_turn_count: 20)
    monkeypatch.setattr(jobs.store, "get_user_timezone", lambda client, user_id: "UTC")
    monkeypatch.setattr(jobs.store, "has_prior_visible_journal", lambda client, user_id, character_id: False)
    monkeypatch.setattr(jobs.store, "queue_session", lambda **kwargs: queued.setdefault("path", "queue/path"))
    monkeypatch.setattr(
        jobs.generator,
        "classify_session",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("first journal skips classification")),
    )

    result = jobs.process_journal_ready_sessions(
        execute=True,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
        client=object(),
        supabase=FakeSupabase(turns=[{"speaker": "user", "text": "hello"}]),
    )

    assert result["results"][0]["journalType"] == "first"
    assert queued["path"] == "queue/path"


def test_generation_writes_memory_event_payload(monkeypatch):
    written = {}
    generated = {
        "text": "The way she laughed at the end stayed with me.",
        "thread_reference": True,
        "topicSummary": ["work stress"],
    }

    class FakeQueue:
        id = "2026-05-11"

        def __init__(self):
            self.reference = FakeRef("users/u1/characters/c1/journal_queue/2026-05-11")
            self.reference.parent = type("Parent", (), {"parent": type("Character", (), {"id": "c1", "parent": type("Characters", (), {"parent": type("User", (), {"id": "u1"})()})()})()})()

        def to_dict(self):
            return {
                "date": "2026-05-11",
                "status": "pending",
                "journal_type": "regular",
                "sessions": [{"sessionId": "s1", "sessionEndTime": "2026-05-11T12:00:00+00:00"}],
            }

    class FakeSb(FakeSupabase):
        def get_system_memory_block(self, user_id):
            return "memory"

        def write_journal_memory_event(self, **kwargs):
            written.update(kwargs)
            return {"id": 123}

    monkeypatch.setattr(jobs.config, "generation_enabled", lambda: True)
    monkeypatch.setattr(jobs.config, "max_generation_queues", lambda: 10)
    monkeypatch.setattr(jobs.store, "fetch_pending_queues", lambda client=None, limit=50: [FakeQueue()])
    monkeypatch.setattr(jobs, "_local_clock_matches", lambda **kwargs: True)
    monkeypatch.setattr(jobs.store, "get_user_data", lambda client, user_id: {})
    monkeypatch.setattr(jobs.store, "get_character_data", lambda client, user_id, character_id: {})
    monkeypatch.setattr(jobs.store, "list_journal_entries", lambda *args, **kwargs: [])
    monkeypatch.setattr(jobs.generator, "generate_journal_text", lambda **kwargs: generated)
    monkeypatch.setattr(jobs.store, "create_journal_entry", lambda **kwargs: "entry-1")
    monkeypatch.setattr(jobs, "_run_lure_back_generation", lambda *args, **kwargs: [])

    result = jobs.run_journal_generation_job(
        execute=True,
        now=datetime(2026, 5, 11, 6, 30, tzinfo=timezone.utc),
        client=object(),
        supabase=FakeSb(),
    )

    assert result["results"][0]["entryId"] == "entry-1"
    assert written["content"]["journalEntryId"] == "entry-1"
    assert written["content"]["thread_reference"] is True
