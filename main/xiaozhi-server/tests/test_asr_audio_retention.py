import json
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_openai_asr_archives_audio_when_delete_disabled(tmp_path):
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    archive_dir = tmp_path / "archive"
    output_dir.mkdir()

    provider = ASRProvider(
        {
            "api_key": "test",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-mini-transcribe",
            "output_dir": str(output_dir),
            "audio_archive_dir": str(archive_dir),
        },
        delete_audio_file=False,
    )

    file_path = output_dir / "sample.wav"
    file_path.write_bytes(b"fake wav")

    archived_path = provider.finalize_audio_file(str(file_path), "session-123")

    assert archived_path is not None
    assert archived_path.startswith(str(archive_dir))
    assert not file_path.exists()
    assert Path(archived_path).exists()
    manifest = archive_dir / "manifest.jsonl"
    assert manifest.exists()
    assert "session-123" in manifest.read_text(encoding="utf-8")


def test_openai_asr_deletes_audio_when_delete_enabled(tmp_path):
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    output_dir.mkdir()

    provider = ASRProvider(
        {
            "api_key": "test",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-mini-transcribe",
            "output_dir": str(output_dir),
        },
        delete_audio_file=True,
    )

    file_path = output_dir / "sample.wav"
    file_path.write_bytes(b"fake wav")
    result = provider.finalize_audio_file(str(file_path), "session-456")

    assert result is None
    assert not file_path.exists()


def test_openai_asr_uploads_audio_to_gcs_and_deletes_local(tmp_path, monkeypatch):
    from core.providers.asr import base as base_module
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    archive_dir = tmp_path / "archive"
    output_dir.mkdir()
    uploads = []

    class FakeBlob:
        def __init__(self, object_name):
            self.object_name = object_name

        def upload_from_filename(self, filename, content_type=None):
            uploads.append(
                {
                    "objectName": self.object_name,
                    "filename": filename,
                    "contentType": content_type,
                }
            )

    class FakeBucket:
        def __init__(self, name):
            self.name = name

        def blob(self, object_name):
            return FakeBlob(object_name)

    class FakeClient:
        def bucket(self, name):
            return FakeBucket(name)

    monkeypatch.setattr(
        base_module,
        "storage",
        SimpleNamespace(Client=lambda: FakeClient()),
    )

    provider = ASRProvider(
        {
            "api_key": "test",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-mini-transcribe",
            "output_dir": str(output_dir),
            "audio_retention_mode": "gcs",
            "audio_gcs_bucket": "milu-user",
            "audio_gcs_prefix": "asr-audio/staging",
            "audio_archive_dir": str(archive_dir),
        },
        delete_audio_file=True,
    )

    file_path = output_dir / "sample.wav"
    file_path.write_bytes(b"fake wav")
    result = provider.finalize_audio_file(str(file_path), "session-789")

    assert result is not None
    assert result.startswith("gs://milu-user/asr-audio/staging/")
    assert result.endswith("/session-789/sample.wav")
    assert not file_path.exists()
    assert uploads == [
        {
            "objectName": result.removeprefix("gs://milu-user/"),
            "filename": str(file_path),
            "contentType": "audio/wav",
        }
    ]

    manifest = archive_dir / "manifest.jsonl"
    record = json.loads(manifest.read_text(encoding="utf-8").strip())
    assert record["sessionId"] == "session-789"
    assert record["gcsUri"] == result
    assert record["localDeleted"] is True


def test_openai_asr_gcs_mode_keeps_local_file_without_bucket(tmp_path):
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    output_dir.mkdir()

    provider = ASRProvider(
        {
            "api_key": "test",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-mini-transcribe",
            "output_dir": str(output_dir),
            "audio_retention_mode": "gcs",
        },
        delete_audio_file=True,
    )

    file_path = output_dir / "sample.wav"
    file_path.write_bytes(b"fake wav")
    result = provider.finalize_audio_file(str(file_path), "session-999")

    assert result == str(file_path)
    assert file_path.exists()
