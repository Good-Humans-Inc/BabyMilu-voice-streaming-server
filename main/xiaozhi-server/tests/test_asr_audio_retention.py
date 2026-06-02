import sys
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
