import asyncio
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


def test_asr_gate_rejects_noise_fragments_and_accepts_short_english(tmp_path):
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

    assert provider._should_forward_asr_text(".")[0] is False
    assert provider._should_forward_asr_text(",")[0] is False
    assert provider._should_forward_asr_text("۵۔")[0] is False
    assert provider._should_forward_asr_text("общими")[0] is False
    assert provider._should_forward_asr_text("谢谢。")[0] is False
    assert provider._should_forward_asr_text(
        "Hmm",
        audio_duration_seconds=0.4,
    )[0] is False
    assert provider._should_forward_asr_text(
        "You",
        audio_duration_seconds=0.5,
    )[0] is False
    assert provider._should_forward_asr_text("I want you to be more emotional.")[0]
    assert provider._should_forward_asr_text("Hi!")[0]
    assert provider._should_forward_asr_text("Ok.")[0]
    assert provider._should_forward_asr_text(
        "You got milk?",
        audio_duration_seconds=0.8,
    )[0]
    assert provider._should_forward_asr_text(
        "Hmm",
        audio_duration_seconds=2.5,
    )[0]


def test_asr_unclear_prompt_speaks_without_forwarding_to_dialogue(tmp_path):
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    output_dir.mkdir()
    provider = ASRProvider(
        {
            "api_key": "test",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-mini-transcribe",
            "output_dir": str(output_dir),
            "unclear_asr_prompt": "I didn't hear that clearly.",
        },
        delete_audio_file=True,
    )
    spoken = []

    conn = SimpleNamespace(
        sentence_id="sentence-1",
        tts_MessageText="",
        tts=SimpleNamespace(
            tts_one_sentence=lambda _conn, content_type, content_detail, sentence_id=None: spoken.append(
                (content_type.name, content_detail, sentence_id)
            ),
        ),
    )

    asyncio.run(provider._maybe_speak_unclear_asr_prompt(conn, "low_signal_fragment"))

    assert spoken == [("TEXT", "I didn't hear that clearly.", "sentence-1")]
    assert conn.tts_MessageText == "I didn't hear that clearly."


def test_openai_asr_sends_language_and_prompt(tmp_path, monkeypatch):
    from core.providers.asr import openai as openai_module
    from core.providers.asr.openai import ASRProvider

    output_dir = tmp_path / "tmp"
    output_dir.mkdir()
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"text":"hello"}'

        def json(self):
            return {"text": "hello"}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data or {})
        captured["headers"] = dict(headers or {})
        captured["timeout"] = timeout
        assert "file" in files
        return FakeResponse()

    monkeypatch.setattr(openai_module.requests, "post", fake_post)

    provider = ASRProvider(
        {
            "api_key": "test-key",
            "base_url": "https://example.com/asr",
            "model_name": "gpt-4o-transcribe",
            "language": "en",
            "prompt": "Only transcribe intentional nearby English speech.",
            "output_dir": str(output_dir),
            "timeout_seconds": 7,
        },
        delete_audio_file=True,
    )

    text, file_path = asyncio.run(
        provider.speech_to_text([b"\x00\x00" * 960], "session-abc", "pcm")
    )

    assert text == "hello"
    assert file_path is not None
    assert not Path(file_path).exists()
    assert captured["url"] == "https://example.com/asr"
    assert captured["data"] == {
        "model": "gpt-4o-transcribe",
        "language": "en",
        "prompt": "Only transcribe intentional nearby English speech.",
    }
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["timeout"] == 7
