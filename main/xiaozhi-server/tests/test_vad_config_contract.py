from __future__ import annotations

import pathlib


def test_default_silero_vad_allows_normal_human_pauses():
    config_path = pathlib.Path(__file__).resolve().parents[1] / "config.yaml"

    config_text = config_path.read_text(encoding="utf-8")

    assert "min_silence_duration_ms: 1500" in config_text
