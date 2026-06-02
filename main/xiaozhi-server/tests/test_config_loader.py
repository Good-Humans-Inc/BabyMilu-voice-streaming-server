from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import config_loader


def test_private_config_keeps_fish_audio_selected_when_global_tts_is_fish(monkeypatch):
    monkeypatch.setattr(
        config_loader,
        "_get_profile_from_firestore",
        lambda device_id, fs_conf: {
            "prompt": "hello from firestore",
            "voice_id": "legacy-eleven-voice",
            "tts_mode": "stream",
            "elevenlabs_api_key": "legacy-eleven-key",
        },
    )

    config = {
        "selected_module": {"TTS": "FishAudio"},
        "TTS": {
            "FishAudio": {
                "type": "fish_audio",
                "api_key": "fish-key",
                "reference_id": "fish-reference",
            }
        },
        "firestore": {"elevenlabs_api_key": "shared-eleven-key"},
    }

    private_config = config_loader.get_private_config_from_api(
        config, "device-1", "client-1"
    )

    assert private_config["prompt"] == "hello from firestore"
    assert private_config["selected_module"]["TTS"] == "FishAudio"
    assert private_config["TTS"]["FishAudio"]["type"] == "fish_audio"
    assert private_config["TTS"]["FishAudio"]["api_key"] == "fish-key"
    assert "ElevenLabsStream" not in private_config.get("TTS", {})


def test_private_config_keeps_elevenlabs_override_when_global_tts_is_not_fish(monkeypatch):
    monkeypatch.setattr(
        config_loader,
        "_get_profile_from_firestore",
        lambda device_id, fs_conf: {
            "voice_id": "legacy-eleven-voice",
            "tts_mode": "stream",
            "elevenlabs_api_key": "legacy-eleven-key",
        },
    )

    config = {
        "selected_module": {"TTS": "EdgeTTS"},
        "TTS": {"EdgeTTS": {"type": "edge"}},
        "firestore": {"elevenlabs_api_key": "shared-eleven-key"},
    }

    private_config = config_loader.get_private_config_from_api(
        config, "device-1", "client-1"
    )

    assert private_config["selected_module"]["TTS"] == "ElevenLabsStream"
    assert private_config["TTS"]["ElevenLabsStream"]["type"] == "elevenlabs_stream"
    assert (
        private_config["TTS"]["ElevenLabsStream"]["voice_id"]
        == "legacy-eleven-voice"
    )
