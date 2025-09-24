import os
import yaml
from collections.abc import Mapping
from config.manage_api_client import init_service, get_server_config, get_agent_models
from typing import Any, Dict
import time

_firestore_client = None
_profile_cache: Dict[str, Dict[str, Any]] = {}
_profile_cache_ttl_seconds = 60
_profile_cache_time: Dict[str, float] = {}


def get_project_dir():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def read_config(config_path):
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config


def load_config():
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_config_path = get_project_dir() + "data/.config.yaml"

    # 加载默认配置
    default_config = read_config(default_config_path)
    custom_config = read_config(custom_config_path)

    if custom_config.get("manager-api", {}).get("url"):
        config = get_config_from_api(custom_config)
    else:
        # 合并配置
        config = merge_configs(default_config, custom_config)
    # 初始化目录
    ensure_directories(config)

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


def get_config_from_api(config):
    """从Java API获取配置"""
    # 初始化API客户端
    init_service(config)

    # 获取服务器配置
    config_data = get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    # server的配置以本地为准
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    return config_data


def get_private_config_from_api(config, device_id, client_id):
    """获取私有配置：优先调用Java API；如果未配置，则尝试Firestore。"""
    # 1) Java API path (manager-api)
    if config.get("manager-api", {}).get("url"):
        return get_agent_models(device_id, client_id, config["selected_module"])

    # 2) Firestore path (if configured)
    fs_conf = config.get("firestore", {})
    if not fs_conf:
        return {}

    profile = _get_profile_from_firestore(device_id, fs_conf)
    if not profile:
        return {}

    # Build private_config in the same shape expected by connection.py
    private_config: Dict[str, Any] = {"selected_module": {}}

    # Prompt override
    if profile.get("prompt"):
        private_config["prompt"] = profile["prompt"]

    # TTS override (CustomTTS default for stability)
    eleven_key = profile.get("elevenlabs_api_key") or fs_conf.get("elevenlabs_api_key", "")
    voice_id = profile.get("voice_id")
    tts_mode = profile.get("tts_mode", "custom").lower()
    if voice_id and eleven_key:
        if tts_mode == "stream":
            # 仅声明我们实际要变更的模块，避免其它模块被误判需要重新初始化
            private_config["selected_module"]["TTS"] = "ElevenLabsStream"
            private_config.setdefault("TTS", {})["ElevenLabsStream"] = {
                "type": "elevenlabs_stream",
                "api_key": eleven_key,
                "voice_id": voice_id,
                "model_id": profile.get("model_id", "eleven_multilingual_v2"),
                "output_format": profile.get("output_format", "pcm_16000"),
                "optimize_streaming_latency": int(profile.get("optimize_streaming_latency", 4)),
                "output_dir": "tmp/",
            }
        else:
            private_config["selected_module"]["TTS"] = "CustomTTS"
            private_config.setdefault("TTS", {})["CustomTTS"] = {
                "type": "custom",
                "method": "POST",
                "url": f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                "headers": {
                    "xi-api-key": eleven_key,
                    "accept": "audio/mpeg",
                    "Content-Type": "application/json",
                },
                "params": {
                    "text": "{prompt_text}",
                    "model_id": profile.get("model_id", "eleven_multilingual_v2"),
                    "optimize_streaming_latency": int(profile.get("optimize_streaming_latency", 4)),
                },
                "format": "mp3",
                "output_dir": "tmp/",
            }

    # 注意：不要在这里补齐其它模块的 selected_module，
    # 否则会触发连接侧对 VAD/ASR 等模块的“需要更新”判断，
    # 但 private_config 未提供对应模块的配置块，从而导致 KeyError。

    return private_config


def _get_profile_from_firestore(device_id: str, fs_conf: Dict[str, Any]) -> Dict[str, Any]:
    # Simple in-memory TTL cache
    now = time.time()
    if device_id in _profile_cache and now - _profile_cache_time.get(device_id, 0) < _profile_cache_ttl_seconds:
        return _profile_cache[device_id]

    global _firestore_client
    if _firestore_client is None:
        try:
            from google.cloud import firestore
        except Exception:
            return {}
        project_id = fs_conf.get("project_id")
        # ADC on GCE; credentials picked automatically
        _firestore_client = firestore.Client(project=project_id) if project_id else firestore.Client()

    try:
        collection = fs_conf.get("devices_collection", "devices")
        doc = _firestore_client.collection(collection).document(str(device_id)).get()
        if not doc.exists:
            return {}
        profile = doc.to_dict() or {}
        _profile_cache[device_id] = profile
        _profile_cache_time[device_id] = now
        return profile
    except Exception:
        return {}


def ensure_directories(config):
    """确保所有配置路径存在"""
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # 根据selected_module创建模型目录
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module) is None:
            continue
        if config.get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged
