import os
import sys
import copy
import json
import uuid
import time
import queue
import asyncio
import threading
import traceback
import subprocess
import websockets

from dataclasses import dataclass
from typing import Dict, Any, Optional
from collections import deque
from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from core.handle.reportHandle import report
from core.providers.tts.default import DefaultTTS
from concurrent.futures import ThreadPoolExecutor
from core.utils.dialogue import Message, Dialogue
from core.providers.asr.dto.dto import InterfaceType
from core.handle.textHandle import handleTextMessage
from core.providers.tools.unified_tool_handler import UnifiedToolHandler
from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import Action, ActionResponse
from core.auth import AuthMiddleware, AuthenticationError
from config.config_loader import get_private_config_from_api
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from config.logger import setup_logging, build_module_string, create_connection_logger
from config.manage_api_client import DeviceNotFoundException, DeviceBindException
from core.utils.prompt_manager import PromptManager
from core.utils.voiceprint_provider import VoiceprintProvider
from core.utils import textUtils
from core.utils.firestore_client import (
    get_active_character_for_device,
    get_character_profile,
    extract_character_profile_fields,
    get_owner_phone_for_device,
    get_user_profile_by_phone,
    extract_user_profile_fields,
    get_conversation_id_for_device,
    set_conversation_id_for_device,
    get_most_recent_character_via_user_for_device,
)
from services.session_context import store as session_context_store
from services.alarms.config import MODE_CONFIG

TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


@dataclass
class ModeRuntimeState:
    active_mode: Optional[str] = None
    instructions: str = ""
    server_initiate_chat: bool = False
    greeting_scheduled: bool = False

    def reset(self) -> None:
        self.active_mode = None
        self.instructions = ""
        self.server_initiate_chat = False
        self.greeting_scheduled = False


@dataclass
class FollowupState:
    task: Optional[asyncio.Future] = None
    count: int = 0
    user_has_responded: bool = False
    enabled: bool = False
    delay: int = 10
    max: int = 5
    default_delay: int = 10
    default_max: int = 5

    def reset(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        self.count = 0
        self.user_has_responded = False
        self.enabled = False
        self.delay = self.default_delay
        self.max = self.default_max


class ConnectionHandler:
    def __init__(
        self,
        config: Dict[str, Any],
        _vad,
        _asr,
        _llm,
        _memory,
        _intent,
        server=None,
    ):
        self.common_config = config
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        self.logger = setup_logging()
        self.server = server  # 保存server实例的引用

        self.auth = AuthMiddleware(config)
        self.need_bind = False
        self.bind_code = None
        self.read_config_from_api = self.config.get("read_config_from_api", False)

        self.websocket = None
        self.headers = None
        self.device_id = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.max_output_size = 0
        self.chat_history_conf = 0
        self.audio_format = "opus"

        # 客户端状态相关
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"

        # Mode + follow-up runtime state
        self._mode_state = ModeRuntimeState()
        self._followup_state = FollowupState()

        # 线程任务相关
        self.loop = asyncio.get_event_loop()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=5)

        # 添加上报线程池
        self.report_queue = queue.Queue()
        self.report_thread = None
        # 未来可以通过修改此处，调节asr的上报和tts的上报，目前默认都开启
        self.report_asr_enable = self.read_config_from_api
        self.report_tts_enable = self.read_config_from_api

        # 依赖的组件
        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        # 为每个连接单独管理声纹识别
        self.voiceprint_provider = None

        # vad相关变量
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.last_activity_time = 0.0  # 统一的活动时间戳（毫秒）
        self.client_voice_stop = False
        self.last_is_voice = False

        # asr相关变量
        # 因为实际部署时可能会用到公共的本地ASR，不能把变量暴露给公共ASR
        # 所以涉及到ASR的变量，需要在这里定义，属于connection的私有变量
        self.asr_audio = []
        self.asr_audio_queue = queue.Queue()

        # llm相关变量
        self.llm_finish_task = True
        self.dialogue = Dialogue()

        # 组件初始化完成事件
        self.components_initialized = asyncio.Event()

        # tts相关变量
        self.sentence_id = None
        self.voice_id = None
        # 处理TTS响应没有文本返回
        self.tts_MessageText = ""

        # iot相关变量
        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]

        # 是否在聊天结束后关闭连接
        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
            int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )  # 在原来第一道关闭的基础上加60秒，进行二道关闭
        self.timeout_task = None

        # {"mcp":true} 表示启用MCP功能
        self.features = None
        self.mode_session = None

        # 标记连接是否来自MQTT
        self.conn_from_mqtt_gateway = False

        # 初始化提示词管理器
        self.prompt_manager = PromptManager(config, self.logger)
        # 当新建会话时，首轮需要下发instructions
        self._seed_instructions_once = False

    # ------------------------------------------------------------------
    # Mode runtime accessors
    # ------------------------------------------------------------------
    @property
    def mode_specific_instructions(self) -> str:
        return self._mode_state.instructions

    @mode_specific_instructions.setter
    def mode_specific_instructions(self, value: str) -> None:
        self._mode_state.instructions = value or ""

    @property
    def server_initiate_chat(self) -> bool:
        return self._mode_state.server_initiate_chat

    @server_initiate_chat.setter
    def server_initiate_chat(self, value: bool) -> None:
        self._mode_state.server_initiate_chat = bool(value)

    @property
    def active_mode(self) -> Optional[str]:
        return self._mode_state.active_mode

    @active_mode.setter
    def active_mode(self, value: Optional[str]) -> None:
        self._mode_state.active_mode = value

    @property
    def _server_greeting_scheduled(self) -> bool:
        return self._mode_state.greeting_scheduled

    @_server_greeting_scheduled.setter
    def _server_greeting_scheduled(self, value: bool) -> None:
        self._mode_state.greeting_scheduled = bool(value)

    # ------------------------------------------------------------------
    # Follow-up runtime accessors
    # ------------------------------------------------------------------
    @property
    def followup_task(self):
        return self._followup_state.task

    @followup_task.setter
    def followup_task(self, value):
        self._followup_state.task = value

    @property
    def followup_count(self) -> int:
        return self._followup_state.count

    @followup_count.setter
    def followup_count(self, value: int) -> None:
        self._followup_state.count = value

    @property
    def followup_user_has_responded(self) -> bool:
        return self._followup_state.user_has_responded

    @followup_user_has_responded.setter
    def followup_user_has_responded(self, value: bool) -> None:
        self._followup_state.user_has_responded = bool(value)

    @property
    def followup_enabled(self) -> bool:
        return self._followup_state.enabled

    @followup_enabled.setter
    def followup_enabled(self, value: bool) -> None:
        self._followup_state.enabled = bool(value)

    @property
    def followup_delay(self) -> int:
        return self._followup_state.delay

    @followup_delay.setter
    def followup_delay(self, value: int) -> None:
        if value is not None:
            self._followup_state.delay = int(value)

    @property
    def followup_max(self) -> int:
        return self._followup_state.max

    @followup_max.setter
    def followup_max(self, value: int) -> None:
        if value is not None:
            self._followup_state.max = int(value)

    async def handle_connection(self, ws):
        try:
            # 获取并验证headers
            self.headers = dict(ws.request.headers)

            if self.headers.get("device-id", None) is None:
                # 尝试从 URL 的查询参数中获取 device-id
                from urllib.parse import parse_qs, urlparse

                # 从 WebSocket 请求中获取路径
                request_path = ws.request.path
                if not request_path:
                    self.logger.bind(tag=TAG).error("无法获取请求路径")
                    return
                parsed_url = urlparse(request_path)
                query_params = parse_qs(parsed_url.query)
                if "device-id" in query_params:
                    self.headers["device-id"] = query_params["device-id"][0]
                    self.headers["client-id"] = query_params["client-id"][0]
                else:
                    await ws.send("端口正常，如需测试连接，请使用test_page.html")
                    await self.close(ws)
                    return
            real_ip = self.headers.get("x-real-ip") or self.headers.get(
                "x-forwarded-for"
            )
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()
            else:
                self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            await self.auth.authenticate(self.headers)

            # 认证通过,继续处理
            self.websocket = ws
            # Normalize device-id casing: uppercase for legacy device lookups,
            # lowercase copy for session context interactions
            raw_device_id = self.headers.get("device-id", None)
            self.device_id = (
                raw_device_id.upper() if isinstance(raw_device_id, str) else raw_device_id
            )
            session_device_id = (
                raw_device_id.lower() if isinstance(raw_device_id, str) else raw_device_id
            )
            self.logger.bind(tag=TAG).info(f"device_id: {self.device_id}")
            self._hydrate_mode_session(session_device_id=session_device_id)

            # Send server hello to satisfy firmware handshake
            try:
                hello = {
                    "type": "hello",
                    "transport": "websocket",
                    "session_id": self.session_id,
                    "audio_params": {
                        "sample_rate": 16000,
                        "frame_duration": 60
                    }
                }
                await self.websocket.send(json.dumps(hello, ensure_ascii=False))
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"Failed to send server hello: {e}")

            # 检查是否来自MQTT连接
            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("连接来自:MQTT网关")

            # 初始化活动时间戳
            self.last_activity_time = time.time() * 1000

            # 从云端获取角色配置（voice, bio 等），并应用到本次会话
            try:
                char_id = None
                if self.device_id:
                    char_id = get_active_character_for_device(self.device_id)
                    if not char_id:
                        fallback_id = get_most_recent_character_via_user_for_device(self.device_id)
                        if fallback_id:
                            self.logger.bind(tag=TAG, device_id=self.device_id).warning(
                                f"activeCharacterId missing; falling back to most recent user character: {fallback_id}"
                            )
                            char_id = fallback_id
                if char_id:
                    self.logger.info(f"char_id={char_id!r}")
                    char_doc = get_character_profile(char_id)
                    self.logger.info(f"char_doc_keys={list((char_doc or {}).keys())}")
                    fields = extract_character_profile_fields(char_doc or {})
                    self.logger.info(f"resolved voice={fields.get('voice')}, bio_present={bool(fields.get('bio'))}")

                    # 优先保留客户端传入的 voice-id；否则使用云端的
                    if not self.voice_id and fields.get("voice"):
                        self.voice_id = str(fields.get("voice"))

                    # 组装角色提示并更新系统提示词
                    profile_parts = []
                    for label, key in (
                        ("Your Name", "name"),
                        ("Your Age", "age"),
                        ("Your Pronouns", "pronouns"),
                        ("Your Relationship with the user", "relationship"),
                        ("You like calling the user", "callMe"),
                    ):
                        val = fields.get(key)
                        if val:
                            profile_parts.append(f"{label}: {val}")
                    profile_line = "\n- ".join(profile_parts)
                    bio_text = fields.get("bio")

                    new_prompt = self.config.get("prompt", "")
                    if profile_line:
                        new_prompt = new_prompt + f"\n# About you:\n{profile_line}"
                    if bio_text:
                        new_prompt = new_prompt + f"\nUser's description of you: {bio_text}"

                    # Append user profile from Firestore users/{ownerPhone}
                    owner_phone = get_owner_phone_for_device(self.device_id)
                    if owner_phone:
                        user_doc = get_user_profile_by_phone(owner_phone)
                        user_fields = extract_user_profile_fields(user_doc or {})
                        user_parts = []
                        for label, key in (("User's name", "name"), ("User's Birthday", "birthday"), ("User's Pronouns", "pronouns")):
                            val = user_fields.get(key)
                            if val:
                                user_parts.append(f"{label}: {val}")
                        if user_parts:
                            user_profile = "\n- ".join(user_parts)
                            new_prompt = new_prompt + f"\nUser profile:\n {user_profile}"
                    if new_prompt != self.config.get("prompt", ""):
                        self.config["prompt"] = new_prompt
                        self.change_system_prompt(new_prompt)
                        self.logger.bind(tag=TAG).info(f"Applied character profile from Firestore, prompt={self.config.get('prompt')}")
                else:
                    # Prominent error to surface missing character configuration
                    self.logger.bind(tag=TAG, device_id=self.device_id).error(
                        "🚨 MISSING activeCharacterId for device; using defaults 🚨"
                    )
                    # No character info – still ensure a default voice is applied if missing
                    if not self.voice_id:
                        default_voice = (
                            self.config.get("TTS", {})
                            .get("CustomTTS", {})
                            .get("default_voice_id")
                            or self.config.get("TTS", {}).get("CustomTTS", {}).get("voice_id")
                        )
                        if default_voice:
                            self.logger.bind(tag=TAG).warning(
                                "No character voice_id; using default voice from config"
                            )
                            self.voice_id = str(default_voice)
                        else:
                            self.logger.bind(tag=TAG).error(
                                "No character voice_id and no default voice configured"
                            )
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"Failed to fetch/apply character profile: {e}")

            # 启动超时检查任务
            self.timeout_task = asyncio.create_task(self._check_timeout())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            # 获取差异化配置
            self._initialize_private_config()
            # 同步构建首轮系统提示词（包含增强），用于会话首条system消息
            try:
                base_prompt = self.config.get("prompt")
                if base_prompt is not None:
                    quick = self.prompt_manager.get_quick_prompt(base_prompt)
                    self.change_system_prompt(quick)
                    # 根据当前连接信息构建增强prompt
                    self.prompt_manager.update_context_info(self, self.client_ip)
                    enhanced = self.prompt_manager.build_enhanced_prompt(
                        self.config["prompt"], self.device_id, self.client_ip
                    )
                    if enhanced:
                        self.change_system_prompt(enhanced)
                        self.logger.bind(tag=TAG).info(
                            f"同步构建增强系统提示词完成"
                        )
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"同步构建系统提示词失败: {e}")
            # 拉取并注入持久会话ID
            try:
                if self.llm and self.device_id:
                    conv_id = get_conversation_id_for_device(self.device_id)
                    if conv_id:
                        if hasattr(self.llm, "adopt_conversation_id_for_session"):
                            self.llm.adopt_conversation_id_for_session(self.session_id, conv_id)
                            self.logger.bind(tag=TAG).info(f"Loaded conversationId for device: {conv_id}")
                    else:
                        # 未找到则创建新会话，并回写到Firestore
                        try:
                            if hasattr(self.llm, "ensure_conversation_with_system"):
                                # 在创建会话时，直接以系统消息作为首条item
                                new_conv_id = self.llm.ensure_conversation_with_system(self.session_id, self.prompt)
                            elif hasattr(self.llm, "ensure_conversation"):
                                new_conv_id = self.llm.ensure_conversation(self.session_id)
                            else:
                                # 兜底：调用一次对话以触发创建
                                new_conv_id = None
                            if new_conv_id:
                                ok = set_conversation_id_for_device(self.device_id, new_conv_id)
                                if ok:
                                    self.logger.bind(tag=TAG).info(f"Created and saved conversationId for device: {new_conv_id}")
                                    # 标记需要在首轮发送instructions
                                    self._seed_instructions_once = True
                                else:
                                    self.logger.bind(tag=TAG).warning("Failed to save conversationId to Firestore")
                        except Exception as create_err:
                            self.logger.bind(tag=TAG).warning(f"Create conversationId failed: {create_err}")
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"Failed to load conversationId: {e}")
            # 异步初始化
            self.executor.submit(self._initialize_components)

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("客户端断开连接")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"最终清理时出错: {final_error}")
                # 确保即使保存记忆失败，也要关闭连接
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"强制关闭连接时出错: {close_error}"
                    )

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            if self.memory:
                # 使用线程池异步保存记忆
                def save_memory_task():
                    try:
                        # 创建新事件循环（避免与主循环冲突）
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            self.memory.save_memory(self.dialogue.dialogue)
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass

                # 启动线程保存记忆，不等待完成
                threading.Thread(target=save_memory_task, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
            # 立即关闭连接，不等待记忆保存完成
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"保存记忆后关闭连接失败: {close_error}"
                )

    async def _route_message(self, message):
        """消息路由"""
        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            if self.vad is None or self.asr is None:
                return

            # 处理来自MQTT网关的音频包
            if self.conn_from_mqtt_gateway and len(message) >= 16:
                handled = await self._process_mqtt_audio_message(message)
                if handled:
                    return

            # 不需要头部处理或没有头部时，直接处理原始消息
            self.asr_audio_queue.put(message)

    async def _process_mqtt_audio_message(self, message):
        """
        处理来自MQTT网关的音频消息，解析16字节头部并提取音频数据

        Args:
            message: 包含头部的音频消息

        Returns:
            bool: 是否成功处理了消息
        """
        try:
            # 提取头部信息
            timestamp = int.from_bytes(message[8:12], "big")
            audio_length = int.from_bytes(message[12:16], "big")

            # 提取音频数据
            if audio_length > 0 and len(message) >= 16 + audio_length:
                # 有指定长度，提取精确的音频数据
                audio_data = message[16 : 16 + audio_length]
                # 基于时间戳进行排序处理
                self._process_websocket_audio(audio_data, timestamp)
                return True
            elif len(message) > 16:
                # 没有指定长度或长度无效，去掉头部后处理剩余数据
                audio_data = message[16:]
                self.asr_audio_queue.put(audio_data)
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"解析WebSocket音频包失败: {e}")

        # 处理失败，返回False表示需要继续处理
        return False

    def _process_websocket_audio(self, audio_data, timestamp):
        """处理WebSocket格式的音频包"""
        # 初始化时间戳序列管理
        if not hasattr(self, "audio_timestamp_buffer"):
            self.audio_timestamp_buffer = {}
            self.last_processed_timestamp = 0
            self.max_timestamp_buffer_size = 20

        # 如果时间戳是递增的，直接处理
        if timestamp >= self.last_processed_timestamp:
            self.asr_audio_queue.put(audio_data)
            self.last_processed_timestamp = timestamp

            # 处理缓冲区中的后续包
            processed_any = True
            while processed_any:
                processed_any = False
                for ts in sorted(self.audio_timestamp_buffer.keys()):
                    if ts > self.last_processed_timestamp:
                        buffered_audio = self.audio_timestamp_buffer.pop(ts)
                        self.asr_audio_queue.put(buffered_audio)
                        self.last_processed_timestamp = ts
                        processed_any = True
                        break
        else:
            # 乱序包，暂存
            if len(self.audio_timestamp_buffer) < self.max_timestamp_buffer_size:
                self.audio_timestamp_buffer[timestamp] = audio_data
            else:
                self.asr_audio_queue.put(audio_data)

    async def handle_restart(self, message):
        """处理服务器重启请求"""
        try:

            self.logger.bind(tag=TAG).info("收到服务器重启指令，准备执行...")

            # 发送确认响应
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "success",
                        "message": "服务器重启中...",
                        "content": {"action": "restart"},
                    }
                )
            )

            # 异步执行重启操作
            def restart_server():
                """实际执行重启的方法"""
                time.sleep(1)
                self.logger.bind(tag=TAG).info("执行服务器重启...")
                subprocess.Popen(
                    [sys.executable, "app.py"],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    start_new_session=True,
                )
                os._exit(0)

            # 使用线程执行重启避免阻塞事件循环
            threading.Thread(target=restart_server, daemon=True).start()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重启失败: {str(e)}")
            await self.websocket.send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": f"Restart failed: {str(e)}",
                        "content": {"action": "restart"},
                    }
                )
            )

    def _initialize_components(self):
        import traceback
        try:
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str)

            """初始化组件"""
            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                # 使用快速提示词进行初始化
                prompt = self.prompt_manager.get_quick_prompt(user_prompt)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"快速初始化组件: prompt成功: {prompt}..."
                )

            """初始化本地组件"""
            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()

            # 初始化声纹识别
            self._initialize_voiceprint()

            # 打开语音识别通道
            asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )
            if self.tts is None:
                self.tts = self._initialize_tts()
            # 打开语音合成通道
            asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )

            """加载记忆"""
            self._initialize_memory()
            """加载意图识别"""
            self._initialize_intent()
            """初始化上报线程"""
            self._init_report_threads()
            """更新系统提示词"""
            self._init_prompt_enhancement()

            self.logger.bind(tag=TAG).info("所有组件初始化完成")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"实例化组件失败: {e}")
            self.logger.bind(tag=TAG).error(f"Traceback:\n{traceback.format_exc()}")
        finally:
            # Always signal completion, even if there was an error
            self.loop.call_soon_threadsafe(self.components_initialized.set)

    def _init_prompt_enhancement(self):
        # 更新上下文信息
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"], self.device_id, self.client_ip
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).info(f"系统提示词已增强更新: {enhanced_prompt}")

    def _init_report_threads(self):
        """初始化ASR和TTS上报线程"""
        if not self.read_config_from_api or self.need_bind:
            return
        if self.chat_history_conf == 0:
            return
        if self.report_thread is None or not self.report_thread.is_alive():
            self.report_thread = threading.Thread(
                target=self._report_worker, daemon=True
            )
            self.report_thread.start()
            self.logger.bind(tag=TAG).info("TTS上报线程已启动")

    def _initialize_tts(self):
        """初始化TTS"""
        tts = None
        if not self.need_bind:
            tts = initialize_tts(self.config)

        if tts is None:
            tts = DefaultTTS(self.config, delete_audio_file=True)

        return tts

    def _initialize_asr(self):
        """初始化ASR"""
        if self._asr.interface_type == InterfaceType.LOCAL:
            # 如果公共ASR是本地服务，则直接返回
            # 因为本地一个实例ASR，可以被多个连接共享
            asr = self._asr
        else:
            # 如果公共ASR是远程服务，则初始化一个新实例
            # 因为远程ASR，涉及到websocket连接和接收线程，需要每个连接一个实例
            asr = initialize_asr(self.config)

        return asr

    def _initialize_voiceprint(self):
        """为当前连接初始化声纹识别"""
        try:
            voiceprint_config = self.config.get("voiceprint", {})
            if voiceprint_config:
                voiceprint_provider = VoiceprintProvider(voiceprint_config)
                if voiceprint_provider is not None and voiceprint_provider.enabled:
                    self.voiceprint_provider = voiceprint_provider
                    self.logger.bind(tag=TAG).info("声纹识别功能已在连接时动态启用")
                else:
                    self.logger.bind(tag=TAG).warning("声纹识别功能启用但配置不完整")
            else:
                self.logger.bind(tag=TAG).info("声纹识别功能未启用")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"声纹识别初始化失败: {str(e)}")

    def _initialize_private_config(self):
        """如果是从配置文件获取，则进行二次实例化"""
        if not self.read_config_from_api:
            return
        """从接口获取差异化的配置进行二次实例化，非全量重新实例化"""
        try:
            begin_time = time.time()
            private_config = get_private_config_from_api(
                self.config,
                self.headers.get("device-id"),
                self.headers.get("client-id", self.headers.get("device-id")),
            )
            private_config["delete_audio"] = bool(self.config.get("delete_audio", True))
            self.logger.bind(tag=TAG).info(
                f"{time.time() - begin_time} 秒，获取差异化配置成功: {json.dumps(filter_sensitive_info(private_config), ensure_ascii=False)}"
            )
        except DeviceNotFoundException as e:
            self.need_bind = True
            private_config = {}
        except DeviceBindException as e:
            self.need_bind = True
            self.bind_code = e.bind_code
            private_config = {}
        except Exception as e:
            self.need_bind = True
            self.logger.bind(tag=TAG).error(f"获取差异化配置失败: {e}")
            private_config = {}

        init_llm, init_tts, init_memory, init_intent = (
            False,
            False,
            False,
            False,
        )

        init_vad = check_vad_update(self.common_config, private_config)
        init_asr = check_asr_update(self.common_config, private_config)

        if init_vad:
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"][
                "VAD"
            ]
        if init_asr:
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"][
                "ASR"
            ]
        if private_config.get("TTS", None) is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"][
                "TTS"
            ]
        if private_config.get("LLM", None) is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"][
                "LLM"
            ]
        if private_config.get("VLLM", None) is not None:
            self.config["VLLM"] = private_config["VLLM"]
            self.config["selected_module"]["VLLM"] = private_config["selected_module"][
                "VLLM"
            ]
        if private_config.get("Memory", None) is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config[
                "selected_module"
            ]["Memory"]
        if private_config.get("Intent", None) is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            model_intent = private_config.get("selected_module", {}).get("Intent", {})
            self.config["selected_module"]["Intent"] = model_intent
            # 加载插件配置
            if model_intent != "Intent_nointent":
                plugin_from_server = private_config.get("plugins", {})
                for plugin, config_str in plugin_from_server.items():
                    plugin_from_server[plugin] = json.loads(config_str)
                self.config["plugins"] = plugin_from_server
                self.config["Intent"][self.config["selected_module"]["Intent"]][
                    "functions"
                ] = plugin_from_server.keys()
        if private_config.get("prompt", None) is not None:
            self.config["prompt"] = private_config["prompt"]
        # 获取声纹信息
        if private_config.get("voiceprint", None) is not None:
            self.config["voiceprint"] = private_config["voiceprint"]
        if private_config.get("summaryMemory", None) is not None:
            self.config["summaryMemory"] = private_config["summaryMemory"]
        if private_config.get("device_max_output_size", None) is not None:
            self.max_output_size = int(private_config["device_max_output_size"])
        if private_config.get("chat_history_conf", None) is not None:
            self.chat_history_conf = int(private_config["chat_history_conf"])
        if private_config.get("mcp_endpoint", None) is not None:
            self.config["mcp_endpoint"] = private_config["mcp_endpoint"]
        try:
            modules = initialize_modules(
                self.logger,
                private_config,
                init_vad,
                init_asr,
                init_llm,
                init_tts,
                init_memory,
                init_intent,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"初始化组件失败: {e}")
            modules = {}
        if modules.get("tts", None) is not None:
            self.tts = modules["tts"]
        if modules.get("vad", None) is not None:
            self.vad = modules["vad"]
        if modules.get("asr", None) is not None:
            self.asr = modules["asr"]
        if modules.get("llm", None) is not None:
            self.llm = modules["llm"]
        if modules.get("intent", None) is not None:
            self.intent = modules["intent"]
        if modules.get("memory", None) is not None:
            self.memory = modules["memory"]

    def _initialize_memory(self):
        if self.memory is None:
            return
        """初始化记忆模块"""
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
            save_to_file=not self.read_config_from_api,
        )

        # 获取记忆总结配置
        memory_config = self.config["Memory"]
        memory_type = self.config["Memory"][self.config["selected_module"]["Memory"]][
            "type"
        ]
        # 如果使用 nomen，直接返回
        if memory_type == "nomem":
            return
        # 使用 mem_local_short 模式
        elif memory_type == "mem_local_short":
            memory_llm_name = memory_config[self.config["selected_module"]["Memory"]][
                "llm"
            ]
            if memory_llm_name and memory_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                memory_llm_config = self.config["LLM"][memory_llm_name]
                memory_llm_type = memory_llm_config.get("type", memory_llm_name)
                memory_llm = llm_utils.create_instance(
                    memory_llm_type, memory_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为记忆总结创建了专用LLM: {memory_llm_name}, 类型: {memory_llm_type}"
                )
                self.memory.set_llm(memory_llm)
            else:
                # 否则使用主LLM
                self.memory.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

    def _initialize_intent(self):
        if self.intent is None:
            return
        self.intent_type = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ]["type"]
        if self.intent_type == "function_call" or self.intent_type == "intent_llm":
            self.load_function_plugin = True
        """初始化意图识别模块"""
        # 获取意图识别配置
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]][
            "type"
        ]

        # 如果使用 nointent，直接返回
        if intent_type == "nointent":
            return
        # 使用 intent_llm 模式
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为意图识别创建了专用LLM: {intent_llm_name}, 类型: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                # 否则使用主LLM
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

        """加载统一工具处理器"""
        self.func_handler = UnifiedToolHandler(self)

        # 异步初始化工具处理器
        if hasattr(self, "loop") and self.loop:
            asyncio.run_coroutine_threadsafe(self.func_handler._initialize(), self.loop)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        self.dialogue.update_system_message(self.prompt)
        self.logger.bind(tag=TAG).info(f"Ran change_system_prompt (new prompt length {len(prompt)}） with prompt:\n\n{prompt}\n")

    def _hydrate_mode_session(self, session_device_id: Optional[str] = None):
        session = None
        if not (session_device_id or self.device_id):
            self.mode_session = None
            self._apply_mode_session_settings()
            return
        lookup_device_id = session_device_id
        if not lookup_device_id and isinstance(self.device_id, str):
            lookup_device_id = self.device_id.lower()
        try:
            session = (
                session_context_store.get_session(lookup_device_id)
                if lookup_device_id
                else None
            )
            if session:
                session_mode = (session.session_config or {}).get("mode")
                self.logger.bind(tag=TAG).info(
                    f"Mode session detected for device {self.device_id}: type={session.session_type}, mode={session_mode}"
                )
        except Exception as e:
            self.logger.bind(tag=TAG).warning(
                f"Failed to hydrate mode session for {self.device_id}: {e}"
            )
        finally:
            self.mode_session = session
            self._apply_mode_session_settings()

    def _apply_mode_session_settings(self):
        self._mode_state.reset()
        self._followup_state.reset()

        session = self.mode_session
        session_config = (session.session_config if session else {}) or {}
        mode = session_config.get("mode")
        if not mode:
            return

        self.active_mode = mode
        if session and session.session_type == "alarm":
            self.logger.bind(tag=TAG).info(
                f"Alarm session activated for device {self.device_id}: "
                f"mode={mode}, alarmId={session_config.get('alarmId')}, "
                f"label={session_config.get('label')}"
            )
        config = dict(MODE_CONFIG.get(mode, {}))
        config.update(session_config.get("mode_config") or {})

        instructions = config.get("instructions", "")
        instructions_file = config.get("instructions_file")
        if instructions_file:
            try:
                with open(instructions_file, "r", encoding="utf-8") as fp:
                    instructions = fp.read().strip()
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(
                    f"Failed to load mode instructions from {instructions_file}: {exc}"
            )

        self.mode_specific_instructions = instructions
        self.server_initiate_chat = config.get("server_initiate_chat", False)
        self.followup_enabled = config.get("followup_enabled", False)
        if "followup_delay" in config:
            self.followup_delay = config["followup_delay"]
        if "followup_max" in config:
            self.followup_max = config["followup_max"]

    def _schedule_followup(self):
        """Schedule a mode follow-up chat after a delay if no user response"""
        # Cancel any existing follow-up task
        if self.followup_task and not self.followup_task.done():
            self.followup_task.cancel()

        # Schedule new follow-up
        delay = getattr(self, "followup_delay", 10)
        self.followup_task = asyncio.run_coroutine_threadsafe(
            self._followup_trigger(delay), self.loop
        )
        self.logger.bind(tag=TAG).info(f"Scheduled follow-up #{self.followup_count + 1} in {delay}s")
    
    async def _followup_trigger(self, delay):
        """Wait and trigger mode follow-up if not cancelled

        This uses the internal `is_user_input` flag when calling `chat()` so that:
        - Server-initiated follow-up turns are clearly distinguished from real user input.
        - Only genuine user messages (ASR/text paths that call `chat(..., is_user_input=True)`)
          will flip `followup_user_has_responded` and permanently stop future follow-ups.
        """
        try:
            await asyncio.sleep(delay)
            # Only trigger if user still hasn't responded and LLM is idle
            if not getattr(self, "followup_user_has_responded", False) and self.llm_finish_task:
                self.followup_count += 1
                self.logger.bind(tag=TAG).info(f"Triggering follow-up #{self.followup_count}")
                # Include context in the query to ensure it's seen even with conversation persistence
                followup_query = f"[No response from user - follow-up #{self.followup_count}]"
                self.executor.submit(
                    self.chat,
                    followup_query,
                    depth=0,
                    extra_inputs=None,
                    is_user_input=False,
                )
        except asyncio.CancelledError:
            self.logger.bind(tag=TAG).info("Follow-up cancelled (user responded)")

    def chat(self, query, depth=0, extra_inputs=None, is_user_input=True):
        self.logger.bind(tag=TAG).info(f"大模型收到用户消息: {query}")
        self.llm_finish_task = False
        
        # If we got genuine user input, mark that the user responded and cancel any pending follow-up timer
        if query and is_user_input:
            self.followup_user_has_responded = True
            if self.followup_task and not self.followup_task.done():
                self.followup_task.cancel()
                self.logger.bind(tag=TAG).info("User responded - cancelling follow-up")

        # 为最顶层时新建会话ID和发送FIRST请求
        if depth == 0:
            self.sentence_id = str(uuid.uuid4().hex)
            self.dialogue.put(Message(role="user", content=query))
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=self.sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )

        # Define intent functions
        functions = None
        if self.intent_type == "function_call" and hasattr(self, "func_handler"):
            functions = self.func_handler.get_functions()
        response_message = []

        try:
            # 使用带记忆的对话
            memory_str = None
            if self.memory is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.memory.query_memory(query), self.loop
                    )
                    memory_str = future.result(timeout=5.0)  # 5 second timeout
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"记忆查询失败或超时: {e}")

            # 根据是否有持久会话决定是否传递全历史
            use_full_history = True
            try:
                if hasattr(self.llm, "has_conversation") and self.llm.has_conversation(self.session_id):
                    use_full_history = False
            except Exception:
                pass

            current_input = None
            if use_full_history:
                current_input = self.dialogue.get_llm_dialogue_with_memory(
                    memory_str, self.config.get("voiceprint", {})
                )
            else:
                # 仅传入最新用户消息；系统prompt/instructions由会话持久化
                current_input = [{"role": "user", "content": query}]

            # 构建instructions：合并memory和可选的mode-specific instructions
            instructions = ""
            if self.mode_specific_instructions:
                instructions += self.mode_specific_instructions
                # Clear after use 
                self.mode_specific_instructions = ""
            
            if memory_str:
                instructions += f"\n\n<memory>\n{memory_str}\n</memory>"

            kwargs = {}
            if instructions:
                kwargs["instructions"] = instructions
                self.logger.bind(tag=TAG).debug(f"Passing instructions to LLM (length: {len(instructions)})")
            if extra_inputs:
                kwargs["extra_inputs"] = extra_inputs

            if self.intent_type == "function_call" and functions is not None:
                # 使用支持functions的streaming接口
                llm_responses = self.llm.response_with_functions(
                    self.session_id,
                    current_input,
                    functions=functions,
                    **kwargs
                )
            else:
                llm_responses = self.llm.response(
                    self.session_id,
                    current_input,
                    **kwargs
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        # 处理流式响应
        tool_call_flag = False
        function_name = None
        function_id = None
        function_arguments = ""
        content_arguments = ""
        self.client_abort = False
        emotion_flag = True
        for response in llm_responses:
            if self.client_abort:
                self.logger.bind(tag=TAG).info("LLM response generation interrupted by client.")
                break
            if self.intent_type == "function_call" and functions is not None:
                content, tools_call = response
                if tools_call is not None and len(tools_call) > 0:
                    try:
                        arg_sample = ""
                        try:
                            arg_sample = str(getattr(getattr(tools_call[0], 'function', None), 'arguments', ""))[:80]
                        except Exception:
                            arg_sample = ""
                        self.logger.bind(tag=TAG).info(f"tool_call delta: id={getattr(tools_call[0], 'id', None)}, name={getattr(getattr(tools_call[0], 'function', None), 'name', None)}, args_len={len(str(getattr(getattr(tools_call[0], 'function', None), 'arguments', '') or ''))}, args_prefix={arg_sample}")
                    except Exception:
                        pass
                if "content" in response:
                    content = response["content"]
                    tools_call = None
                if content is not None and len(content) > 0:
                    content_arguments += content

                if not tool_call_flag and content_arguments.startswith("<tool_call>"):
                    # print("content_arguments", content_arguments)
                    tool_call_flag = True

                if tools_call is not None and len(tools_call) > 0:
                    tool_call_flag = True
                    if tools_call[0].id is not None:
                        function_id = tools_call[0].id
                    if tools_call[0].function.name is not None:
                        function_name = tools_call[0].function.name
                    if tools_call[0].function.arguments is not None:
                        function_arguments += tools_call[0].function.arguments
            else:
                content = response

            # 在llm回复中获取情绪表情，一轮对话只在开头获取一次
            if emotion_flag and content is not None and content.strip():
                asyncio.run_coroutine_threadsafe(
                    textUtils.get_emotion(self, content),
                    self.loop,
                )
                emotion_flag = False

            if content is not None and len(content) > 0:
                if not tool_call_flag:
                    response_message.append(content)
                    self.tts.tts_text_queue.put(
                        TTSMessageDTO(
                            sentence_id=self.sentence_id,
                            sentence_type=SentenceType.MIDDLE,
                            content_type=ContentType.TEXT,
                            content_detail=content,
                        )
                    )
        # 处理function call
        if tool_call_flag:
            bHasError = False
            if function_id is None:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        function_name = content_arguments_json["name"]
                        function_arguments = json.dumps(
                            content_arguments_json["arguments"], ensure_ascii=False
                        )
                        function_id = str(uuid.uuid4().hex)
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )
            if not bHasError:
                # 如需要大模型先处理一轮，添加相关处理后的日志情况
                if len(response_message) > 0:
                    text_buff = "".join(response_message)
                    self.tts_MessageText = text_buff
                    self.dialogue.put(Message(role="assistant", content=text_buff))
                response_message.clear()
                try:
                    self.logger.bind(tag=TAG).info(
                        f"Consolidated function_call: id={function_id}, name={function_name}, args_len={len(function_arguments) if function_arguments else 0}, args_prefix={(function_arguments or '')[:120]}"
                    )
                except Exception:
                    pass
                function_call_data = {
                    "name": function_name,
                    "id": function_id,
                    "arguments": function_arguments,
                }

                # 使用统一工具处理器处理所有工具调用
                result = asyncio.run_coroutine_threadsafe(
                    self.func_handler.handle_llm_function_call(
                        self, function_call_data
                    ),
                    self.loop,
                ).result()
                self._handle_function_result(result, function_call_data, depth=depth)

        # 存储对话内容
        if len(response_message) > 0:
            text_buff = "".join(response_message)
            self.tts_MessageText = text_buff
            self.dialogue.put(Message(role="assistant", content=text_buff))
        if depth == 0:
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=self.sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
        self.llm_finish_task = True
        
        # Schedule follow-up if enabled and the user has not responded yet
        if (
            getattr(self, "followup_enabled", False)
            and not getattr(self, "followup_user_has_responded", False)
            and self.followup_count < getattr(self, "followup_max", 5)
        ):
            self._schedule_followup()
        
        # 使用lambda延迟计算，只有在DEBUG级别时才执行get_llm_dialogue()
        self.logger.bind(tag=TAG).debug(
            lambda: json.dumps(
                self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
            )
        )

        return True

    def _handle_function_result(self, result, function_call_data, depth):
        # Conversations: 将工具输出作为下一轮responses输入传递（function_call_output），不做本地播报/拼接
        using_conversation = False
        if hasattr(self.llm, "has_conversation"):
            try:
                using_conversation = bool(self.llm.has_conversation(self.session_id))
            except Exception:
                using_conversation = False
        function_id = function_call_data.get("id")
        if using_conversation and function_id:
            tool_output = None
            if result.action == Action.REQLLM:
                tool_output = result.result
            else:
                tool_output = result.response if result.response else result.result
            # Ensure output is a string per Responses API; JSON-encode if needed
            if tool_output is not None and not isinstance(tool_output, str):
                try:
                    tool_output = json.dumps(tool_output, ensure_ascii=False)
                except Exception:
                    tool_output = str(tool_output)
            try:
                self.logger.bind(tag=TAG).debug(f"Preparing function_call_output: id={function_id}, output_len={len(tool_output) if tool_output is not None else 0}")
            except Exception:
                pass
            extra_inputs = [
                {
                    "type": "function_call_output",
                    "call_id": function_id,
                    "output": "" if tool_output is None else tool_output,
                }
            ]
            self.logger.bind(tag=TAG).info(f"Passing function_call_output in next turn: id={function_id}, len={len(str(tool_output)) if tool_output is not None else 0}")
            # This is a tool-driven continuation, not fresh user input
            self.chat("", depth=depth + 1, extra_inputs=extra_inputs, is_user_input=False)
            return

        if result.action == Action.RESPONSE:  # 直接回复前端
            text = result.response
            self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
            self.dialogue.put(Message(role="assistant", content=text))
        elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
            text = result.result
            if text is not None and len(text) > 0:
                function_id = function_call_data["id"]
                function_name = function_call_data["name"]
                function_arguments = function_call_data["arguments"]
                self.dialogue.put(
                    Message(
                        role="assistant",
                        tool_calls=[
                            {
                                "id": function_id,
                                "function": {
                                    "arguments": (
                                        "{}"
                                        if function_arguments == ""
                                        else function_arguments
                                    ),
                                    "name": function_name,
                                },
                                "type": "function",
                                "index": 0,
                            }
                        ],
                    )
                )

                self.dialogue.put(
                    Message(
                        role="tool",
                        tool_call_id=(
                            str(uuid.uuid4()) if function_id is None else function_id
                        ),
                        content=text,
                    )
                )
                # This is a tool-driven continuation, not fresh user input
                self.chat(text, depth=depth + 1, is_user_input=False)
        elif result.action == Action.NOTFOUND or result.action == Action.ERROR:
            text = result.response if result.response else result.result
            # more user friendly error reporting
            # user_friendly_error_message = "Sorry, seems like something's up with a module I'm trying to use. Could you press the connect button to help me try again?"
            # self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=user_friendly_error_message)
            self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
            self.dialogue.put(Message(role="assistant", content=text))
        else:
            pass

    def _report_worker(self):
        """聊天记录上报工作线程"""
        while not self.stop_event.is_set():
            try:
                # 从队列获取数据，设置超时以便定期检查停止事件
                item = self.report_queue.get(timeout=1)
                if item is None:  # 检测毒丸对象
                    break
                try:
                    # 检查线程池状态
                    if self.executor is None:
                        continue
                    # 提交任务到线程池
                    self.executor.submit(self._process_report, *item)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"聊天记录上报线程异常: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"聊天记录上报工作线程异常: {e}")

        self.logger.bind(tag=TAG).info("聊天记录上报线程已退出")

    def _process_report(self, type, text, audio_data, report_time):
        """处理上报任务"""
        try:
            # 执行上报（传入二进制数据）
            report(self, type, text, audio_data, report_time)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"上报处理异常: {e}")
        finally:
            # 标记任务完成
            self.report_queue.task_done()

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug(f"清除服务端讲话状态")

    async def close(self, ws=None):
        """资源清理方法"""
        try:
            # 清理音频缓冲区
            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            # 取消超时任务
            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            # 清理工具处理器资源
            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"清理工具处理器时出错: {cleanup_error}"
                    )

            # 触发停止事件
            if self.stop_event:
                self.stop_event.set()

            # 清空任务队列
            self.clear_queues()

            # 关闭WebSocket连接
            try:
                if ws:
                    # 安全地检查WebSocket状态并关闭
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await ws.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
                elif self.websocket:
                    try:
                        if (
                            hasattr(self.websocket, "closed")
                            and not self.websocket.closed
                        ):
                            await self.websocket.close()
                        elif (
                            hasattr(self.websocket, "state")
                            and self.websocket.state.name != "CLOSED"
                        ):
                            await self.websocket.close()
                        else:
                            # 如果没有closed属性，直接尝试关闭
                            await self.websocket.close()
                    except Exception:
                        # 如果关闭失败，忽略错误
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"关闭WebSocket连接时出错: {ws_error}")

            if self.tts:
                await self.tts.close()

            # 最后关闭线程池（避免阻塞）
            if self.executor:
                try:
                    self.executor.shutdown(wait=False)
                except Exception as executor_error:
                    self.logger.bind(tag=TAG).error(
                        f"关闭线程池时出错: {executor_error}"
                    )
                self.executor = None

            self.logger.bind(tag=TAG).info("连接资源已释放")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"关闭连接时出错: {e}")
        finally:
            # 确保停止事件被设置
            if self.stop_event:
                self.stop_event.set()

    def clear_queues(self):
        """清空所有任务队列"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"开始清理: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

            # 使用非阻塞方式清空队列
            for q in [
                self.tts.tts_text_queue,
                self.tts.tts_audio_queue,
                self.report_queue,
            ]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            self.logger.bind(tag=TAG).debug(
                f"清理结束: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_vad_states(self):
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.logger.bind(tag=TAG).debug("VAD states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            # Internal/server-initiated close; not fresh user input
            self.chat(text, is_user_input=False)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def _check_timeout(self):
        """检查连接超时"""
        try:
            while not self.stop_event.is_set():
                # 检查是否超时（只有在时间戳已初始化的情况下）
                if self.last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if (
                        current_time - self.last_activity_time
                        > self.timeout_seconds * 1000
                    ):
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("连接超时，准备关闭")
                            # 设置停止事件，防止重复处理
                            self.stop_event.set()
                            # 使用 try-except 包装关闭操作，确保不会因为异常而阻塞
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(
                                    f"超时关闭连接时出错: {close_error}"
                                )
                        break
                # 每10秒检查一次，避免过于频繁
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"超时检查任务出错: {e}")
        finally:
            self.logger.bind(tag=TAG).info("超时检查任务已退出")