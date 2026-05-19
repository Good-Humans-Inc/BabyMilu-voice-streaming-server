from services.journals import store


class FakeSnapshot:
    def __init__(self, exists=True, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class FakeDocument:
    def __init__(self, doc_id):
        self.id = doc_id
        self.set_calls = []
        self._exists = True

    def collection(self, name):
        return FakeCollection(name)

    def get(self):
        return FakeSnapshot(self._exists)

    def set(self, payload, merge=False):
        self.set_calls.append((payload, merge))


class FakeCollection:
    docs = {}

    def __init__(self, name):
        self.name = name

    def document(self, doc_id):
        key = (self.name, doc_id)
        if key not in self.docs:
            self.docs[key] = FakeDocument(doc_id)
        return self.docs[key]


class FakeClient:
    def collection(self, name):
        return FakeCollection(name)


def test_write_session_marker_sets_waiting_memory():
    FakeCollection.docs = {}
    client = FakeClient()

    ok = store.write_session_marker(
        user_id="u1",
        device_id="d1",
        character_id="c1",
        session_id="s1",
        user_turn_count=4,
        client=client,
    )

    doc = FakeCollection.docs[("journal_session_state", "s1")]
    payload, merge = doc.set_calls[0]
    assert ok is True
    assert merge is True
    assert payload["status"] == "waiting_memory"
    assert payload["userTurnCount"] == 4


def test_soft_delete_marks_entry_without_deleting():
    FakeCollection.docs = {}
    client = FakeClient()
    FakeCollection.docs[("moments", "entry-1")] = FakeDocument("entry-1")

    deleted = store.soft_delete_journal_entry(
        user_id="u1",
        character_id="c1",
        entry_id="entry-1",
        client=client,
    )

    doc = FakeCollection.docs[("moments", "entry-1")]
    payload, merge = doc.set_calls[0]
    assert deleted is True
    assert merge is True
    assert payload["status"] == "deleted"
    assert payload["deletedAt"]


def test_create_journal_entry_writes_app_moment_shape():
    FakeCollection.docs = {}
    client = FakeClient()

    entry_id = store.create_journal_entry(
        client=client,
        user_id="u1",
        character_id="c1",
        text="I wrote this down.",
        journal_type="regular",
        display_at="2026-05-18T07:00:00+00:00",
        entry_id="entry-1",
    )

    doc = FakeCollection.docs[("moments", "entry-1")]
    payload, merge = doc.set_calls[0]
    assert entry_id == "entry-1"
    assert merge is True
    assert payload == {
        "id": "entry-1",
        "type": "journal",
        "characterId": "c1",
        "displayAt": "2026-05-18T07:00:00+00:00",
        "text": "I wrote this down.",
        "status": "ready",
        "journalType": "regular",
        "memoryEventId": None,
        "deletedAt": None,
    }


def test_create_journal_entry_uses_app_lure_back_spelling():
    FakeCollection.docs = {}
    client = FakeClient()

    store.create_journal_entry(
        client=client,
        user_id="u1",
        character_id="c1",
        text="I missed her.",
        journal_type="lure_back",
        display_at="2026-05-18T07:00:00+00:00",
        entry_id="entry-1",
    )

    doc = FakeCollection.docs[("moments", "entry-1")]
    payload, _ = doc.set_calls[0]
    assert payload["journalType"] == "lure-back"
