import copy
import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"ip": "0.0.0.0", "port": 8000, "http_port": 8003},
    "selected_module": {"ASR": "OpenaiASR", "LLM": "OpenAILLM", "TTS": "FishAudio"},
    "ASR": {
        "OpenaiASR": {
            "type": "openai",
            "api_key": "",
            "base_url": "https://api.openai.com/v1/audio/transcriptions",
            "model_name": "gpt-4o-mini-transcribe",
            "language": "en",
            "output_dir": "tmp/",
        }
    },
    "LLM": {
        "OpenAILLM": {
            "type": "openai",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4o",
            "api_key": "",
            "max_tokens": 160,
            "temperature": 0.6,
            "top_p": 1,
        }
    },
    "TTS": {
        "FishAudio": {
            "type": "fish_audio",
            "api_url": "https://api.fish.audio/v1/tts",
            "api_key": "",
            "reference_id": "",
            "latency": "normal",
            "normalize": True,
            "chunk_length": 100,
            "top_p": 0.7,
            "temperature": 0.7,
            "repetition_penalty": 1.2,
        }
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def is_usable_secret(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    lowered = value.lower()
    blocked = ["your-", "your_", "你的", "api key", "api_key", "xxxx", "bearer;"]
    return not any(token in lowered for token in blocked)


def provider_config(config: dict[str, Any], module: str) -> tuple[str, dict[str, Any]]:
    selected = (config.get("selected_module") or {}).get(module)
    providers = config.get(module) or {}
    if selected in providers:
        return selected, providers[selected] or {}

    if module == "LLM" and "OpenAILLM" in providers:
        return "OpenAILLM", providers["OpenAILLM"] or {}
    if module == "ASR" and "OpenaiASR" in providers:
        return "OpenaiASR", providers["OpenaiASR"] or {}
    if module == "TTS" and "FishAudio" in providers:
        return "FishAudio", providers["FishAudio"] or {}
    raise KeyError(f"No provider configured for {module}")


def _apply_env_overrides(config: dict[str, Any]) -> None:
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        config.setdefault("ASR", {}).setdefault("OpenaiASR", {})["api_key"] = openai_key
        config.setdefault("LLM", {}).setdefault("OpenAILLM", {})["api_key"] = openai_key

    fish_key = os.getenv("FISH_AUDIO_API_KEY")
    if fish_key:
        config.setdefault("TTS", {}).setdefault("FishAudio", {})["api_key"] = fish_key

    fish_reference = os.getenv("FISH_AUDIO_REFERENCE_ID")
    if fish_reference:
        config.setdefault("TTS", {}).setdefault("FishAudio", {})["reference_id"] = fish_reference

    llm_model = os.getenv("LLM_MODEL")
    if llm_model:
        config.setdefault("LLM", {}).setdefault("OpenAILLM", {})["model_name"] = llm_model

    asr_model = os.getenv("ASR_MODEL")
    if asr_model:
        config.setdefault("ASR", {}).setdefault("OpenaiASR", {})["model_name"] = asr_model

    if os.getenv("ECHOEAR_WS_PORT"):
        config.setdefault("server", {})["port"] = int(os.environ["ECHOEAR_WS_PORT"])
    if os.getenv("ECHOEAR_HTTP_PORT"):
        config.setdefault("server", {})["http_port"] = int(os.environ["ECHOEAR_HTTP_PORT"])


def load_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(config_dir or os.getenv("ECHOEAR_CONFIG_DIR") or ".").resolve()
    config = copy.deepcopy(DEFAULT_CONFIG)

    config = deep_merge(config, read_yaml(root / "config.yaml"))
    config = deep_merge(config, read_yaml(root / "data" / ".config.yaml"))
    _apply_env_overrides(config)

    mock_env = os.getenv("ECHOEAR_MOCK_PROVIDERS", "").strip().lower()
    if mock_env in {"1", "true", "yes", "on"}:
        config["mock_providers"] = True

    try:
        _, asr = provider_config(config, "ASR")
        _, llm = provider_config(config, "LLM")
        _, tts = provider_config(config, "TTS")
    except KeyError:
        config["mock_providers"] = True
    else:
        if not (
            is_usable_secret(asr.get("api_key"))
            and is_usable_secret(llm.get("api_key"))
            and is_usable_secret(tts.get("api_key"))
            and is_usable_secret(tts.get("reference_id"))
        ):
            config.setdefault("mock_providers", True)

    return config


def public_summary(config: dict[str, Any]) -> dict[str, Any]:
    def summarize(module: str) -> dict[str, Any]:
        name, cfg = provider_config(config, module)
        return {
            "name": name,
            "type": cfg.get("type"),
            "model": cfg.get("model_name") or cfg.get("reference_id"),
            "mock": bool(config.get("mock_providers")),
        }

    server = config.get("server") or {}
    return {
        "server": {
            "ip": server.get("ip", "0.0.0.0"),
            "port": int(server.get("port", 8000)),
            "http_port": int(server.get("http_port", 8003)),
        },
        "providers": {
            "asr": summarize("ASR"),
            "llm": summarize("LLM"),
            "tts": summarize("TTS"),
        },
        "queued_audio_only": True,
    }
