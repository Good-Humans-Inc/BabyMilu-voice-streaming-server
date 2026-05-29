from pathlib import Path

from echoear_server.config import load_config, provider_config, public_summary


def test_config_loads_data_config_override(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".config.yaml").write_text(
        """
selected_module:
  ASR: OpenaiASR
  LLM: OpenAILLM
  TTS: FishAudio
ASR:
  OpenaiASR:
    api_key: asr-key
LLM:
  OpenAILLM:
    api_key: llm-key
TTS:
  FishAudio:
    api_key: fish-key
    reference_id: fish-ref
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("ECHOEAR_MOCK_PROVIDERS", raising=False)
    cfg = load_config(tmp_path)
    assert provider_config(cfg, "ASR")[1]["api_key"] == "asr-key"
    assert cfg.get("mock_providers") is not True
    assert public_summary(cfg)["queued_audio_only"] is True


def test_missing_secrets_falls_back_to_mock(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg["mock_providers"] is True

