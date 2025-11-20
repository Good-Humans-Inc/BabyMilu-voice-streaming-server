import time
import json
import random
import asyncio
from core.utils.dialogue import Message
from core.utils.util import audio_to_data
from core.providers.tts.dto.dto import SentenceType
from core.utils.wakeup_word import WakeupWordsConfig
from core.handle.sendAudioHandle import sendAudioMessage, send_stt_message
from core.utils.util import remove_punctuation_and_length, opus_datas_to_wav_bytes
from core.providers.tools.device_mcp import (
    MCPClient,
    send_mcp_initialize_message,
    send_mcp_tools_list_request,
)

TAG = __name__

async def _trigger_server_greeting(conn):
    """Wait for all components initialization, then trigger server-initiated greeting"""
    try:
        # Wait for components_initialized event (with 60s timeout)
        await asyncio.wait_for(conn.components_initialized.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        conn.logger.bind(tag=TAG).warning("Components not initialized after 60s, cannot trigger greeting")
        return
    
    # Check llm_finish_task to avoid overlapping conversations
    if conn.llm_finish_task:
        conn.logger.bind(tag=TAG).info("Triggering server-initiated greeting")
        # Server-initiated greeting; not fresh user input
        conn.executor.submit(conn.chat, "", 0, None, False)
    else:
        conn.logger.bind(tag=TAG).warning("Cannot trigger greeting: llm_finish_task is False")

WAKEUP_CONFIG = {
    "refresh_time": 5,
    "words": ["你好", "你好啊", "嘿，你好", "嗨"],
}

# 创建全局的唤醒词配置管理器
wakeup_words_config = WakeupWordsConfig()

# 用于防止并发调用wakeupWordsResponse的锁
_wakeup_response_lock = asyncio.Lock()


async def handleHelloMessage(conn, msg_json):
    """处理hello消息"""
    conn.logger.bind(tag=TAG).info(f"👋 Received hello message: {msg_json}")
    audio_params = msg_json.get("audio_params")
    if audio_params:
        format = audio_params.get("format")
        conn.logger.bind(tag=TAG).info(f"客户端音频格式: {format}")
        conn.audio_format = format
        conn.welcome_msg["audio_params"] = audio_params
    features = msg_json.get("features")
    if features:
        conn.logger.bind(tag=TAG).info(f"客户端特性features: {features}")
        conn.features = features
        # Mode功能（如morning_alarm闹钟）
        # HACK: Force morning_alarm mode for demo (remove this line when device sends mode)
        # if not features.get("mode"):
        #     features["mode"] = "morning_alarm"
        #     conn.logger.bind(tag=TAG).warning("🚨 DEMO MODE: Forcing morning_alarm mode 🚨")
        if features.get("mode"):
            mode = features.get("mode").lower()
            mode_config = conn.config.get("mode_config", {}).get(mode, {})
            # Load instructions from file if specified
            instructions_file = mode_config.get("instructions_file")
            if instructions_file:
                try:
                    with open(instructions_file, 'r', encoding='utf-8') as f:
                        conn.mode_specific_instructions = f.read().strip()
                    conn.logger.bind(tag=TAG).info(f"Loaded mode specific instructions from {instructions_file}")
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"Failed to load mode specific instructions from {instructions_file}: {e}")
                    conn.mode_specific_instructions = mode_config.get("instructions", "")
            else:
                conn.mode_specific_instructions = mode_config.get("instructions", "")
            if conn.mode_specific_instructions:
                conn.logger.bind(tag=TAG).info(f"Read mode specific instructions from mode config: {conn.mode_specific_instructions}")
            else:
                conn.logger.bind(tag=TAG).warning(f"No mode specific instructions found for mode: {conn.mode}")
            # whether to initiate chat from server for this mode
            conn.server_initiate_chat = mode_config.get("server_initiate_chat", False)
            # Generic follow-up config for modes that may need proactive re-engagement
            conn.followup_enabled = mode_config.get("followup_enabled", False)
            conn.followup_delay = mode_config.get("followup_delay", 10)
            conn.followup_max = mode_config.get("followup_max", 5)
            if conn.server_initiate_chat:
                # Trigger server-initiated greeting after TTS is ready
                asyncio.create_task(_trigger_server_greeting(conn))
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).info("客户端支持MCP")
            conn.mcp_client = MCPClient()
            # 发送初始化
            asyncio.create_task(send_mcp_initialize_message(conn))
            # 发送mcp消息，获取tools列表
            asyncio.create_task(send_mcp_tools_list_request(conn))

    await conn.websocket.send(json.dumps(conn.welcome_msg))


async def checkWakeupWords(conn, text):
    enable_wakeup_words_response_cache = conn.config[
        "enable_wakeup_words_response_cache"
    ]

    # 等待tts初始化，最多等待3秒
    start_time = time.time()
    while time.time() - start_time < 3:
        if conn.tts:
            break
        await asyncio.sleep(0.1)
    else:
        return False

    if not enable_wakeup_words_response_cache:
        return False

    _, filtered_text = remove_punctuation_and_length(text)
    if filtered_text not in conn.config.get("wakeup_words"):
        return False

    conn.just_woken_up = True
    await send_stt_message(conn, text)

    # 获取当前音色
    voice = getattr(conn.tts, "voice", "default")
    if not voice:
        voice = "default"

    # 获取唤醒词回复配置
    response = wakeup_words_config.get_wakeup_response(voice)
    if not response or not response.get("file_path"):
        response = {
            "voice": "default",
            "file_path": "config/assets/wakeup_words.wav",
            "time": 0,
            "text": "哈啰啊，我是小智啦，声音好听的台湾女孩一枚，超开心认识你耶，最近在忙啥，别忘了给我来点有趣的料哦，我超爱听八卦的啦",
        }

    # 获取音频数据
    opus_packets = audio_to_data(response.get("file_path"))
    # 播放唤醒词回复
    conn.client_abort = False

    conn.logger.bind(tag=TAG).info(f"播放唤醒词回复: {response.get('text')}")
    await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
    await sendAudioMessage(conn, SentenceType.LAST, [], None)

    # 补充对话
    conn.dialogue.put(Message(role="assistant", content=response.get("text")))

    # 检查是否需要更新唤醒词回复
    if time.time() - response.get("time", 0) > WAKEUP_CONFIG["refresh_time"]:
        if not _wakeup_response_lock.locked():
            asyncio.create_task(wakeupWordsResponse(conn))
    return True


async def wakeupWordsResponse(conn):
    if not conn.tts or not conn.llm or not conn.llm.response_no_stream:
        return

    try:
        # 尝试获取锁，如果获取不到就返回
        if not await _wakeup_response_lock.acquire():
            return

        # 生成唤醒词回复
        wakeup_word = random.choice(WAKEUP_CONFIG["words"])
        question = (
            "此刻用户正在和你说```"
            + wakeup_word
            + "```。\n请你根据以上用户的内容进行20-30字回复。要符合系统设置的角色情感和态度，不要像机器人一样说话。\n"
            + "请勿对这条内容本身进行任何解释和回应，请勿返回表情符号，仅返回对用户的内容的回复。"
        )

        result = conn.llm.response_no_stream(conn.config["prompt"], question)
        if not result or len(result) == 0:
            return

        # 生成TTS音频
        tts_result = await asyncio.to_thread(conn.tts.to_tts, result)
        if not tts_result:
            return

        # 获取当前音色
        voice = getattr(conn.tts, "voice", "default")

        wav_bytes = opus_datas_to_wav_bytes(tts_result, sample_rate=16000)
        file_path = wakeup_words_config.generate_file_path(voice)
        with open(file_path, "wb") as f:
            f.write(wav_bytes)
        # 更新配置
        wakeup_words_config.update_wakeup_response(voice, file_path, result)
    finally:
        # 确保在任何情况下都释放锁
        if _wakeup_response_lock.locked():
            _wakeup_response_lock.release()