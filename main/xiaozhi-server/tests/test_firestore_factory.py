import os


def test_build_firestore_client_uses_database_id(monkeypatch):
    from core.utils import firestore_factory

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(firestore_factory.firestore, "Client", FakeClient)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-1")
    monkeypatch.setenv("FIRESTORE_DATABASE_ID", "development")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    firestore_factory.build_firestore_client()

    assert captured == {"project": "project-1", "database": "development"}


def test_build_firestore_client_preserves_default_database(monkeypatch):
    from core.utils import firestore_factory

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(firestore_factory.firestore, "Client", FakeClient)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("FIRESTORE_DATABASE_ID", "(default)")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    firestore_factory.build_firestore_client()

    assert "database" not in captured


def test_build_firestore_client_clears_directory_credentials(monkeypatch, tmp_path):
    from core.utils import firestore_factory

    class FakeClient:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(firestore_factory.firestore, "Client", FakeClient)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path))

    firestore_factory.build_firestore_client()

    assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") != str(tmp_path)
