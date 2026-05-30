import copy
import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "ip": "0.0.0.0",
        "port": 8000,
        "http_port": 8003,
        "tts_frame_interval_ms": 60,
        "tts_prebuffer_frames": 4,
    },
    "profile": {
        "default_device_id": "",
        "supabase_url": "",
        "supabase_service_role_key": "",
        "timeout_seconds": 10,
        "users_table": "users",
        "memory_read_model_table": "memory_read_model",
    },
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
            "sample_rate": 44100,
            "debug_audio_dir": "generated_audio",
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
    fish_sample_rate = os.getenv("FISH_AUDIO_SAMPLE_RATE")
    if fish_sample_rate:
        config.setdefault("TTS", {}).setdefault("FishAudio", {})["sample_rate"] = int(fish_sample_rate)
    tts_debug_audio_dir = os.getenv("ECHOEAR_TTS_DEBUG_AUDIO_DIR")
    if tts_debug_audio_dir:
        config.setdefault("TTS", {}).setdefault("FishAudio", {})["debug_audio_dir"] = tts_debug_audio_dir

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
    if os.getenv("ECHOEAR_TTS_FRAME_INTERVAL_MS"):
        config.setdefault("server", {})["tts_frame_interval_ms"] = int(os.environ["ECHOEAR_TTS_FRAME_INTERVAL_MS"])
    if os.getenv("ECHOEAR_TTS_PREBUFFER_FRAMES"):
        config.setdefault("server", {})["tts_prebuffer_frames"] = int(os.environ["ECHOEAR_TTS_PREBUFFER_FRAMES"])

    profile = config.setdefault("profile", {})
    if os.getenv("ECHOEAR_DEFAULT_DEVICE_ID"):
        profile["default_device_id"] = os.environ["ECHOEAR_DEFAULT_DEVICE_ID"]
    if os.getenv("SUPABASE_URL"):
        profile["supabase_url"] = os.environ["SUPABASE_URL"]
    if os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        profile["supabase_service_role_key"] = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    if os.getenv("SUPABASE_TIMEOUT_SECONDS"):
        profile["timeout_seconds"] = int(os.environ["SUPABASE_TIMEOUT_SECONDS"])
    if os.getenv("SUPABASE_USERS_TABLE"):
        profile["users_table"] = os.environ["SUPABASE_USERS_TABLE"]
    if os.getenv("SUPABASE_MEMORY_READ_MODEL_TABLE"):
        profile["memory_read_model_table"] = os.environ["SUPABASE_MEMORY_READ_MODEL_TABLE"]


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
            "tts_frame_interval_ms": int(server.get("tts_frame_interval_ms", 60)),
            "tts_prebuffer_frames": int(server.get("tts_prebuffer_frames", 4)),
        },
        "providers": {
            "asr": summarize("ASR"),
            "llm": summarize("LLM"),
            "tts": summarize("TTS"),
        },
        "profile": {
            "supabase": bool((config.get("profile") or {}).get("supabase_url")),
            "default_device_id": bool((config.get("profile") or {}).get("default_device_id")),
        },
        "queued_audio_only": True,
    }
