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
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from collections import deque

from concurrent.futures import ThreadPoolExecutor

from core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from core.handle.reportHandle import report
from core.providers.tts.default import DefaultTTS
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
from core.utils.util import filter_sensitive_info, check_vad_update, check_asr_update
from core.utils.firestore_client import (
    get_active_character_for_device,
    get_character_profile,
    extract_character_profile_fields,
    get_owner_phone_for_device,
    get_user_profile_by_phone,
    extract_user_profile_fields,
    get_conversation_state_for_device,
    update_conversation_state_for_device,
    get_most_recent_character_via_user_for_device,
    get_assigned_tasks_for_user,
    process_user_action,
)

from services.session_context import store as session_context_store
from services.alarms.config import MODE_CONFIG


TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


@dataclass
class ModeRuntimeState:
    """In-memory state for a mode session (e.g. morning_alarm)."""
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
    """
    Tracks server-initiated follow-ups (e.g. alarm follow-up prompts).

    Only genuine user input should flip `user_has_responded` to True and
    cancel any pending follow-ups.
    """
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
        self.server = server  # 引用server实例

        self.auth = AuthMiddleware(config)
        self.need_bind = False
        self.bind_code = None
        self.read_config_from_api = self.config.get("read_config_from_api", False)

        self.websocket = None
        self.headers = None
        self.device_id: Optional[str] = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.max_output_size = 0
        self.chat_history_conf = 0
        self.audio_format = "opus"

        # 客户端状态
        self.client_abort = False
        self.client_is_speaking = False
        self.client_listen_mode = "auto"

        # Mode + follow-up runtime state
        self._mode_state = ModeRuntimeState()
        self._followup_state = FollowupState()
        self.followup_delays = None  # Optional[List[int]] escalating delays
        self.followup_exit_after = 120  # seconds since last speech before exit
        self.last_tts_stop_ms = 0
        self.last_stt_activity_ms = 0

        # 线程/任务
        self.loop = asyncio.get_event_loop()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=5)

        # 上报线程
        self.report_queue = queue.Queue()
        self.report_thread = None
        self.report_asr_enable = self.read_config_from_api
        self.report_tts_enable = self.read_config_from_api

        # 组件
        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        # 声纹识别
        self.voiceprint_provider = None

        # VAD
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.last_activity_time = 0.0
        self.client_voice_stop = False
        self.last_is_voice = False

        # ASR
        self.asr_audio = []
        self.asr_audio_queue = queue.Queue()

        # LLM / 对话
        self.llm_finish_task = True
        self.dialogue = Dialogue()
        self.device_conversation_ttl = self._derive_conversation_ttl()
        self.current_conversation_id: Optional[str] = None
        self.use_mode_conversation = False

        # 组件初始化事件
        self.components_initialized = asyncio.Event()

        # TTS
        self.sentence_id = None
        self.voice_id = None
        self.tts_MessageText = ""

        # IOT/MCP
        self.iot_descriptors = {}
        self.func_handler: Optional[UnifiedToolHandler] = None

        self.cmd_exit = self.config["exit_commands"]

        # 聊天结束后关闭连接
        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
            int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )
        self.timeout_task = None

        # {"mcp": true}
        self.features = None
        self.mode_session = None

        # MQTT 网关标记
        self.conn_from_mqtt_gateway = False

        # Prompt manager
        self.prompt_manager = PromptManager(config, self.logger)
        # 新建会话时，首轮下发mode instructions
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

    # ------------------------------------------------------------------

    async def handle_connection(self, ws):
        try:
            # 获取并验证headers
            self.headers = dict(ws.request.headers)

            if self.headers.get("device-id", None) is None:
                # 尝试从 URL 的查询参数中获取 device-id
                from urllib.parse import parse_qs, urlparse

                request_path = ws.request.path
                if not request_path:
                    self.logger.bind(tag=TAG).error("无法获取请求路径")
                    return
                parsed_url = urlparse(request_path)
                query_params = parse_qs(parsed_url.query)
                if "device-id" in query_params:
                    self.headers["device-id"] = query_params["device-id"][0]
                    self.headers["client-id"] = query_params.get(
                        "client-id", [query_params["device-id"][0]]
                    )[0]
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

            # Normalize device-id to lower-case so it matches sessionContexts doc IDs
            raw_device_id = self.headers.get("device-id")
            self.device_id = raw_device_id.lower() if isinstance(raw_device_id, str) else raw_device_id
            self.logger.bind(tag=TAG).info(f"device_id: {self.device_id}")

            # Hydrate any scheduled mode session (e.g. alarm)
            self._hydrate_mode_session()

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
                    self.logger.info(
                        f"resolved voice={fields.get('voice')}, bio_present={bool(fields.get('bio'))}"
                    )

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
                        for label, key in (
                            ("User's name", "name"),
                            ("User's Birthday", "birthday"),
                            ("User's Pronouns", "pronouns"),
                        ):
                            val = user_fields.get(key)
                            if val:
                                user_parts.append(f"{label}: {val}")
                        if user_parts:
                            user_profile = "\n- ".join(user_parts)
                            new_prompt = new_prompt + f"\nUser profile:\n {user_profile}"

                    if new_prompt != self.config.get("prompt", ""):
                        self.config["prompt"] = new_prompt
                        self.change_system_prompt(new_prompt)
                        self.logger.bind(tag=TAG).info(
                            f"Applied character profile from Firestore, prompt={self.config.get('prompt')}"
                        )
                else:
                    self.logger.bind(tag=TAG, device_id=self.device_id).error(
                        "🚨 MISSING activeCharacterId for device; using defaults 🚨"
                    )
                    if not self.voice_id:
                        default_voice = (
                            self.config.get("TTS", {})
                            .get("CustomTTS", {})
                            .get("default_voice_id")
                            or self.config.get("TTS", {})
                            .get("CustomTTS", {})
                            .get("voice_id")
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
                self.logger.bind(tag=TAG).warning(
                    f"Failed to fetch/apply character profile: {e}"
                )

            # 启动超时检查任务
            self.timeout_task = asyncio.create_task(self._check_timeout())

            self.welcome_msg = self.config["xiaozhi"]
            self.welcome_msg["session_id"] = self.session_id

            # 获取差异化配置
            self._initialize_private_config()

            # 同步构建首轮系统提示词（包含增强）
            try:
                base_prompt = self.config.get("prompt")
                if base_prompt is not None:
                    quick = self.prompt_manager.get_quick_prompt(base_prompt)
                    self.change_system_prompt(quick)
                    self.prompt_manager.update_context_info(self, self.client_ip)
                    enhanced = self.prompt_manager.build_enhanced_prompt(
                        self.config["prompt"], self.device_id, self.client_ip
                    )
                    if enhanced:
                        self.change_system_prompt(enhanced)
                        self.logger.bind(tag=TAG).info("同步构建增强系统提示词完成")
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"同步构建系统提示词失败: {e}")

            # 初始化会话绑定（mode-scoped 或 device-scoped）
            self._initialize_conversation_binding()

            # 异步初始化本地组件
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
            self.logger.bind(tag=TAG).error(
                f"Connection error: {str(e)}-{stack_trace}"
            )
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"最终清理时出错: {final_error}")
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"强制关闭连接时出错: {close_error}"
                    )

    def get_current_conversation(self):
        """
        获取当前websocket连接中的对话历史
        
        Returns:
            list: 对话历史列表，包含所有消息
                 返回格式: [{"role": "user/assistant/system", "content": "..."}, ...]
            None: 如果对话为空或出错
        """
        try:
            if self.dialogue:
                # 获取完整的对话历史
                conversation = self.dialogue.get_llm_dialogue()
                self.logger.bind(tag=TAG).debug(
                    f"获取当前对话历史，共 {len(conversation)} 条消息"
                )
                return conversation
            else:
                self.logger.bind(tag=TAG).warning("对话对象不存在")
                return None
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取对话历史失败: {e}")
            return None
    
    def generate_ai_conversation_summary(self):
        """
        使用LLM生成对话内容的AI摘要
        
        Returns:
            str: AI生成的对话摘要文本，如果失败则返回None
        """
        try:
            if not self.llm:
                self.logger.bind(tag=TAG).warning("LLM未初始化，无法生成AI摘要")
                return None
            
            conversation = self.get_current_conversation()
            if not conversation or len(conversation) == 0:
                self.logger.bind(tag=TAG).debug("对话为空，跳过AI摘要生成")
                return None
            
            # 过滤掉system消息，只保留用户和助手的对话
            filtered_conv = [msg for msg in conversation if msg.get("role") in ["user", "assistant"]]
            
            if len(filtered_conv) == 0:
                self.logger.bind(tag=TAG).debug("没有用户对话内容，跳过AI摘要生成")
                return None
            
            # 构建对话历史文本
            conv_text = ""
            for msg in filtered_conv:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "")
                conv_text += f"{role}: {content}\n"
            
            # 构建摘要请求
            summary_prompt = [
                {
                    "role": "user",
                    "content": f"Please provide a concise summary of the following conversation, focusing on key information and themes. Keep the summary under 100 words.\n\nConversation:\n{conv_text}\n\nProvide summary:"
                }
            ]
            
            # 调用LLM生成摘要
            summary_parts = []
            llm_responses = self.llm.response(
                f"{self.session_id}_summary",
                summary_prompt,
                stateless=True  # 使用无状态模式，不保存这次摘要对话
            )
            
            for response in llm_responses:
                if response:
                    summary_parts.append(response)
            
            summary = "".join(summary_parts).strip()
            
            if summary:
                self.logger.bind(tag=TAG).info(f"AI对话摘要生成成功: {summary[:50]}...")
                return summary
            else:
                self.logger.bind(tag=TAG).warning("AI摘要生成为空")
                return None
                
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"生成AI对话摘要失败: {e}")
            return None
    
    def check_conversation_against_tasks(self, user_id: str):
        """
        检查当前对话内容是否匹配用户的已分配任务
        
        Args:
            user_id: 用户ID，用于获取分配的任务列表
            
        Returns:
            list: 匹配的任务列表，每个任务包含任务信息和匹配原因
                 格式: [{"task_id": "...", "task_title": "...", "match_reason": "..."}, ...]
        """
        try:
            if not self.llm:
                self.logger.bind(tag=TAG).warning("LLM未初始化，无法检查任务匹配")
                return []
            
            # 获取用户分配的任务
            tasks = get_assigned_tasks_for_user(user_id)
            if not tasks or len(tasks) == 0:
                self.logger.bind(tag=TAG).debug(f"用户 {user_id} 没有分配的任务")
                return []
            
            # 获取当前对话
            conversation = self.get_current_conversation()
            if not conversation or len(conversation) == 0:
                self.logger.bind(tag=TAG).debug("对话为空，跳过任务匹配")
                return []
            
            # 过滤对话，只保留用户和助手的消息
            filtered_conv = [msg for msg in conversation if msg.get("role") in ["user", "assistant"]]
            if len(filtered_conv) == 0:
                self.logger.bind(tag=TAG).debug("没有用户对话内容，跳过任务匹配")
                return []
            
            # 构建对话历史文本
            conv_text = ""
            for msg in filtered_conv:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "")
                conv_text += f"{role}: {content}\n"
            
            # 构建任务列表文本
            tasks_text = ""
            for idx, task in enumerate(tasks, 1):
                task_id = task.get("id", "unknown")
                task_title = task.get("title", "No title")
                action_config = task.get("actionConfig", {})
                action = action_config.get("action", "N/A")
                tasks_text += f"{idx}. ID: {task_id}\n   Title: {task_title}\n   Action: {action}\n\n"
            
            # 构建LLM提示词
            matching_prompt = [
                {
                    "role": "user",
                    "content": f"""Analyze the following conversation and determine if the content is related to any of the user's assigned tasks.

Conversation:
{conv_text}

User's assigned tasks:
{tasks_text}

Carefully analyze the conversation content to determine if any of the above tasks were discussed, mentioned, or completed.
If there are matching tasks, return a JSON array with the following format:
[
  {{"task_id": "task ID", "task_action": "action from actionConfig", "match_reason": "brief explanation of why the conversation relates to this task"}}
]

If no tasks match, return an empty array: []

Return ONLY the JSON array, no other explanation."""
                }
            ]
            
            # 调用LLM进行任务匹配
            response_parts = []
            llm_responses = self.llm.response(
                f"{self.session_id}_task_match",
                matching_prompt,
                stateless=True
            )
            
            for response in llm_responses:
                if response:
                    response_parts.append(response)
            
            response_text = "".join(response_parts).strip()
            
            # 解析JSON响应
            import json
            try:
                # 尝试提取JSON数组
                if "[" in response_text and "]" in response_text:
                    start_idx = response_text.find("[")
                    end_idx = response_text.rfind("]") + 1
                    json_str = response_text[start_idx:end_idx]
                    matched_tasks = json.loads(json_str)
                    
                    if matched_tasks and len(matched_tasks) > 0:
                        self.logger.bind(tag=TAG).info(
                            f"Detected {len(matched_tasks)} matching tasks: {[t.get('task_action') for t in matched_tasks]}"
                        )
                        return matched_tasks
                    else:
                        self.logger.bind(tag=TAG).debug("No tasks matched in the conversation")
                        return []
                else:
                    self.logger.bind(tag=TAG).debug("LLM响应中未找到JSON格式")
                    # TODO try again
                    return []
            except json.JSONDecodeError as e:
                self.logger.bind(tag=TAG).warning(f"解析任务匹配JSON失败: {e}, 响应内容: {response_text[:200]}")
                return []
                
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"检查对话任务匹配失败: {e}")
            return []

    def get_current_conversation(self):
        """
        获取当前websocket连接中的对话历史
        
        Returns:
            list: 对话历史列表，包含所有消息
                 返回格式: [{"role": "user/assistant/system", "content": "..."}, ...]
            None: 如果对话为空或出错
        """
        try:
            if self.dialogue:
                # 获取完整的对话历史
                conversation = self.dialogue.get_llm_dialogue()
                self.logger.bind(tag=TAG).debug(
                    f"获取当前对话历史，共 {len(conversation)} 条消息"
                )
                return conversation
            else:
                self.logger.bind(tag=TAG).warning("对话对象不存在")
                return None
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取对话历史失败: {e}")
            return None
    
    def generate_ai_conversation_summary(self):
        """
        使用LLM生成对话内容的AI摘要
        
        Returns:
            str: AI生成的对话摘要文本，如果失败则返回None
        """
        try:
            if not self.llm:
                self.logger.bind(tag=TAG).warning("LLM未初始化，无法生成AI摘要")
                return None
            
            conversation = self.get_current_conversation()
            if not conversation or len(conversation) == 0:
                self.logger.bind(tag=TAG).debug("对话为空，跳过AI摘要生成")
                return None
            
            # 过滤掉system消息，只保留用户和助手的对话
            filtered_conv = [msg for msg in conversation if msg.get("role") in ["user", "assistant"]]
            
            if len(filtered_conv) == 0:
                self.logger.bind(tag=TAG).debug("没有用户对话内容，跳过AI摘要生成")
                return None
            
            # 构建对话历史文本
            conv_text = ""
            for msg in filtered_conv:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "")
                conv_text += f"{role}: {content}\n"
            
            # 构建摘要请求
            summary_prompt = [
                {
                    "role": "user",
                    "content": f"Please provide a concise summary of the following conversation, focusing on key information and themes. Keep the summary under 100 words.\n\nConversation:\n{conv_text}\n\nProvide summary:"
                }
            ]
            
            # 调用LLM生成摘要
            summary_parts = []
            llm_responses = self.llm.response(
                f"{self.session_id}_summary",
                summary_prompt,
                stateless=True  # 使用无状态模式，不保存这次摘要对话
            )
            
            for response in llm_responses:
                if response:
                    summary_parts.append(response)
            
            summary = "".join(summary_parts).strip()
            
            if summary:
                self.logger.bind(tag=TAG).info(f"AI对话摘要生成成功: {summary[:50]}...")
                return summary
            else:
                self.logger.bind(tag=TAG).warning("AI摘要生成为空")
                return None
                
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"生成AI对话摘要失败: {e}")
            return None
    
    def check_conversation_against_tasks(self, user_id: str):
        """
        检查当前对话内容是否匹配用户的已分配任务
        
        Args:
            user_id: 用户ID，用于获取分配的任务列表
            
        Returns:
            list: 匹配的任务列表，每个任务包含任务信息和匹配原因
                 格式: [{"task_id": "...", "task_title": "...", "match_reason": "..."}, ...]
        """
        try:
            if not self.llm:
                self.logger.bind(tag=TAG).warning("LLM未初始化，无法检查任务匹配")
                return []
            
            # 获取用户分配的任务
            tasks = get_assigned_tasks_for_user(user_id)
            if not tasks or len(tasks) == 0:
                self.logger.bind(tag=TAG).debug(f"用户 {user_id} 没有分配的任务")
                return []
            
            # 获取当前对话
            conversation = self.get_current_conversation()
            if not conversation or len(conversation) == 0:
                self.logger.bind(tag=TAG).debug("对话为空，跳过任务匹配")
                return []
            
            # 过滤对话，只保留用户和助手的消息
            filtered_conv = [msg for msg in conversation if msg.get("role") in ["user", "assistant"]]
            if len(filtered_conv) == 0:
                self.logger.bind(tag=TAG).debug("没有用户对话内容，跳过任务匹配")
                return []
            
            # 构建对话历史文本
            conv_text = ""
            for msg in filtered_conv:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "")
                conv_text += f"{role}: {content}\n"
            
            # 构建任务列表文本
            tasks_text = ""
            for idx, task in enumerate(tasks, 1):
                task_id = task.get("id", "unknown")
                task_title = task.get("title", "No title")
                action_config = task.get("actionConfig", {})
                action = action_config.get("action", "N/A")
                tasks_text += f"{idx}. ID: {task_id}\n   Title: {task_title}\n   Action: {action}\n\n"
            
            # 构建LLM提示词
            matching_prompt = [
                {
                    "role": "user",
                    "content": f"""Analyze the following conversation and determine if the content is related to any of the user's assigned tasks.

Conversation:
{conv_text}

User's assigned tasks:
{tasks_text}

Carefully analyze the conversation content to determine if any of the above tasks were discussed, mentioned, or completed.
If there are matching tasks, return a JSON array with the following format:
[
  {{"task_id": "task ID", "task_action": "action from actionConfig", "match_reason": "brief explanation of why the conversation relates to this task"}}
]

If no tasks match, return an empty array: []

Return ONLY the JSON array, no other explanation."""
                }
            ]
            
            # 调用LLM进行任务匹配
            response_parts = []
            llm_responses = self.llm.response(
                f"{self.session_id}_task_match",
                matching_prompt,
                stateless=True
            )
            
            for response in llm_responses:
                if response:
                    response_parts.append(response)
            
            response_text = "".join(response_parts).strip()
            
            # 解析JSON响应
            import json
            try:
                # 尝试提取JSON数组
                if "[" in response_text and "]" in response_text:
                    start_idx = response_text.find("[")
                    end_idx = response_text.rfind("]") + 1
                    json_str = response_text[start_idx:end_idx]
                    matched_tasks = json.loads(json_str)
                    
                    if matched_tasks and len(matched_tasks) > 0:
                        self.logger.bind(tag=TAG).info(
                            f"Detected {len(matched_tasks)} matching tasks: {[t.get('task_action') for t in matched_tasks]}"
                        )
                        return matched_tasks
                    else:
                        self.logger.bind(tag=TAG).debug("No tasks matched in the conversation")
                        return []
                else:
                    self.logger.bind(tag=TAG).debug("LLM响应中未找到JSON格式")
                    # TODO try again
                    return []
            except json.JSONDecodeError as e:
                self.logger.bind(tag=TAG).warning(f"解析任务匹配JSON失败: {e}, 响应内容: {response_text[:200]}")
                return []
                
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"检查对话任务匹配失败: {e}")
            return []

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            # 获取并记录对话摘要（包含AI生成的摘要和任务匹配）
            if self.dialogue:
                # 使用线程池异步完成task
                def complete_task_task():
                    try:
                        conversation = self.dialogue.get_llm_dialogue()
                        if conversation:
                            user_msgs = sum(1 for msg in conversation if msg.get("role") == "user")
                            assistant_msgs = sum(1 for msg in conversation if msg.get("role") == "assistant")
                            
                            # 生成AI摘要
                            ai_summary = self.generate_ai_conversation_summary()
                            
                            # 检查任务匹配
                            matched_tasks = []
                            try:
                                # 获取用户ID (使用owner_phone作为user_id)
                                if self.device_id:
                                    owner_phone = get_owner_phone_for_device(self.device_id)
                                    if owner_phone:
                                        matched_tasks = self.check_conversation_against_tasks(owner_phone)
                                        process_user_action(owner_phone, matched_tasks)
                            except Exception as task_err:
                                self.logger.bind(tag=TAG).warning(f"检查任务匹配失败: {task_err}")
                            
                            # 记录对话信息
                            log_msg = (
                                f"会话结束 - Session: {self.session_id}, Device: {self.device_id}, "
                                f"总消息: {len(conversation)}, 用户: {user_msgs}, 助手: {assistant_msgs}"
                            )
                            
                            if ai_summary:
                                log_msg += f"\nAI摘要: {ai_summary}"
                            
                            if matched_tasks and len(matched_tasks) > 0:
                                log_msg += f"\nMatched tasks ({len(matched_tasks)}):"
                                for task in matched_tasks:
                                    log_msg += f"\n  - Action: {task.get('task_action')} (ID: {task.get('task_id')}): {task.get('match_reason')}"
                            
                            self.logger.bind(tag=TAG).info(log_msg)
                    except Exception as e:
                        self.logger.bind(tag=TAG).warning(f"获取对话摘要失败: {e}")
                threading.Thread(target=complete_task_task, daemon=True).start()
            try:
                self._persist_conversation_state_before_close()
            except Exception as conv_err:
                self.logger.bind(tag=TAG).warning(
                    f"Failed to persist conversation metadata: {conv_err}"
                )
            if self.memory:
                def save_memory_task():
                    try:
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

                threading.Thread(target=save_memory_task, daemon=True).start()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
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

            self.asr_audio_queue.put(message)

    async def _process_mqtt_audio_message(self, message):
        """
        处理来自MQTT网关的音频消息，解析16字节头部并提取音频数据
        """
        try:
            timestamp = int.from_bytes(message[8:12], "big")
            audio_length = int.from_bytes(message[12:16], "big")

            if audio_length > 0 and len(message) >= 16 + audio_length:
                audio_data = message[16 : 16 + audio_length]
                self._process_websocket_audio(audio_data, timestamp)
                return True
            elif len(message) > 16:
                audio_data = message[16:]
                self.asr_audio_queue.put(audio_data)
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"解析WebSocket音频包失败: {e}")
        return False

    def _process_websocket_audio(self, audio_data, timestamp):
        """处理WebSocket格式的音频包"""
        if not hasattr(self, "audio_timestamp_buffer"):
            self.audio_timestamp_buffer = {}
            self.last_processed_timestamp = 0
            self.max_timestamp_buffer_size = 20

        if timestamp >= self.last_processed_timestamp:
            self.asr_audio_queue.put(audio_data)
            self.last_processed_timestamp = timestamp
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
            if len(self.audio_timestamp_buffer) < self.max_timestamp_buffer_size:
                self.audio_timestamp_buffer[timestamp] = audio_data
            else:
                self.asr_audio_queue.put(audio_data)

    async def handle_restart(self, message):
        """处理服务器重启请求"""
        try:
            self.logger.bind(tag=TAG).info("收到服务器重启指令，准备执行...")
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

            def restart_server():
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
        try:
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str)

            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                prompt = self.prompt_manager.get_quick_prompt(user_prompt)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"快速初始化组件: prompt成功: {prompt}..."
                )

            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()

            self._initialize_voiceprint()

            asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )
            if self.tts is None:
                self.tts = self._initialize_tts()
            asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )

            self._initialize_memory()
            self._initialize_intent()
            self._init_report_threads()
            self._init_prompt_enhancement()

            self.logger.bind(tag=TAG).info("所有组件初始化完成")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"实例化组件失败: {e}")
            self.logger.bind(tag=TAG).error(
                f"Traceback:\n{traceback.format_exc()}"
            )
        finally:
            self.loop.call_soon_threadsafe(self.components_initialized.set)

    def _init_prompt_enhancement(self):
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"], self.device_id, self.client_ip
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).info(
                f"系统提示词已增强更新: {enhanced_prompt}"
            )

    def _init_report_threads(self):
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
        tts = None
        if not self.need_bind:
            tts = initialize_tts(self.config)

        if tts is None:
            tts = DefaultTTS(self.config, delete_audio_file=True)

        return tts

    def _initialize_asr(self):
        if self._asr.interface_type == InterfaceType.LOCAL:
            asr = self._asr
        else:
            asr = initialize_asr(self.config)
        return asr

    def _initialize_voiceprint(self):
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
        if not self.read_config_from_api:
            return
        try:
            begin_time = time.time()
            private_config = get_private_config_from_api(
                self.config,
                self.headers.get("device-id"),
                self.headers.get("client-id", self.headers.get("device-id")),
            )
            private_config["delete_audio"] = bool(
                self.config.get("delete_audio", True)
            )
            self.logger.bind(tag=TAG).info(
                f"{time.time() - begin_time} 秒，获取差异化配置成功: {json.dumps(filter_sensitive_info(private_config), ensure_ascii=False)}"
            )
        except DeviceNotFoundException:
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

        init_llm = init_tts = init_memory = init_intent = False
        init_vad = check_vad_update(self.common_config, private_config)
        init_asr = check_asr_update(self.common_config, private_config)

        if init_vad:
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"]["VAD"]
        if init_asr:
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"]["ASR"]

        if private_config.get("TTS") is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"]["TTS"]

        if private_config.get("LLM") is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"]["LLM"]

        if private_config.get("VLLM") is not None:
            self.config["VLLM"] = private_config["VLLM"]
            self.config["selected_module"]["VLLM"] = private_config["selected_module"]["VLLM"]

        if private_config.get("Memory") is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config["selected_module"]["Memory"]

        if private_config.get("Intent") is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            model_intent = private_config.get("selected_module", {}).get("Intent", {})
            self.config["selected_module"]["Intent"] = model_intent
            if model_intent != "Intent_nointent":
                plugin_from_server = private_config.get("plugins", {})
                for plugin, config_str in plugin_from_server.items():
                    plugin_from_server[plugin] = json.loads(config_str)
                self.config["plugins"] = plugin_from_server
                self.config["Intent"][self.config["selected_module"]["Intent"]][
                    "functions"
                ] = plugin_from_server.keys()

        if private_config.get("prompt") is not None:
            self.config["prompt"] = private_config["prompt"]
        if private_config.get("voiceprint") is not None:
            self.config["voiceprint"] = private_config["voiceprint"]
        if private_config.get("summaryMemory") is not None:
            self.config["summaryMemory"] = private_config["summaryMemory"]
        if private_config.get("device_max_output_size") is not None:
            self.max_output_size = int(private_config["device_max_output_size"])
        if private_config.get("chat_history_conf") is not None:
            self.chat_history_conf = int(private_config["chat_history_conf"])
        if private_config.get("mcp_endpoint") is not None:
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

        if modules.get("tts") is not None:
            self.tts = modules["tts"]
        if modules.get("vad") is not None:
            self.vad = modules["vad"]
        if modules.get("asr") is not None:
            self.asr = modules["asr"]
        if modules.get("llm") is not None:
            self.llm = modules["llm"]
        if modules.get("intent") is not None:
            self.intent = modules["intent"]
        if modules.get("memory") is not None:
            self.memory = modules["memory"]

    def _initialize_memory(self):
        if self.memory is None:
            return
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
            save_to_file=not self.read_config_from_api,
        )

        memory_config = self.config["Memory"]
        memory_type = memory_config[self.config["selected_module"]["Memory"]]["type"]

        if memory_type == "nomem":
            return
        elif memory_type == "mem_local_short":
            memory_llm_name = memory_config[self.config["selected_module"]["Memory"]][
                "llm"
            ]
            if memory_llm_name and memory_llm_name in self.config["LLM"]:
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
                self.memory.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

    def _initialize_intent(self):
        if self.intent is None:
            return
        self.intent_type = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ]["type"]
        if self.intent_type in ("function_call", "intent_llm"):
            self.load_function_plugin = True

        intent_config = self.config["Intent"]
        intent_type = intent_config[self.config["selected_module"]["Intent"]]["type"]

        if intent_type == "nointent":
            return
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
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
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

        self.func_handler = UnifiedToolHandler(self)
        if hasattr(self, "loop") and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.func_handler._initialize(), self.loop
            )

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        self.dialogue.update_system_message(self.prompt)
        self.logger.bind(tag=TAG).info(
            f"Ran change_system_prompt (new prompt length {len(prompt)}） with prompt:\n\n{prompt}\n"
        )

    # ------------------------------------------------------------------
    # Mode session + conversation/sessionContext integration
    # ------------------------------------------------------------------
    def _hydrate_mode_session(self):
        """Load any active mode session for this device from Firestore."""
        session = None
        if not self.device_id:
            self.mode_session = None
            self._apply_mode_session_settings()
            return
        try:
            session = session_context_store.get_session(self.device_id)
            if session:
                session_mode = (session.session_config or {}).get("mode")
                self.logger.bind(tag=TAG).info(
                    f"Mode session detected for device {self.device_id}: "
                    f"type={session.session_type}, mode={session_mode}"
                )
        except Exception as e:
            self.logger.bind(tag=TAG).warning(
                f"Failed to hydrate mode session for {self.device_id}: {e}"
            )
        finally:
            self.mode_session = session
            self._apply_mode_session_settings()

    def _apply_mode_session_settings(self):
        """Apply MODE_CONFIG and sessionConfig to runtime state."""
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
        self.use_mode_conversation = bool(
            config.get("use_separate_conversation", False)
        )

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
        # Optional escalating delays array
        delays = config.get("followup_delays")
        if isinstance(delays, list) and len(delays) > 0:
            try:
                self.followup_delays = [int(d) for d in delays]
            except Exception:
                self.followup_delays = None
        else:
            # sensible defaults per product guidance
            self.followup_delays = [10, 15, 20]
        # Optional exit timeout after last speech
        if isinstance(config.get("followup_exit_after"), (int, float)):
            self.followup_exit_after = int(config["followup_exit_after"])

    def _initialize_conversation_binding(self):
        """Bind this connection to either a mode-scoped or device-scoped conversation."""
        try:
            if not self.llm or not self.device_id:
                return
            if self.use_mode_conversation and self.mode_session:
                self._ensure_mode_scoped_conversation()
            else:
                self._ensure_device_scoped_conversation()
        except Exception as exc:
            self.logger.bind(tag=TAG).warning(
                f"Failed to load conversation state: {exc}"
            )

    def _ensure_mode_scoped_conversation(self):
        session = self.mode_session
        if not session:
            self._ensure_device_scoped_conversation()
            return

        conversation = session.conversation or {}
        conv_id = conversation.get("id")
        self.logger.bind(tag=TAG).debug(
            f"Mode session conversation state: conversation={conversation}, "
            f"conv_id={conv_id}, is_snooze_follow_up={session.is_snooze_follow_up}"
        )

        if conv_id and hasattr(self.llm, "adopt_conversation_id_for_session"):
            self.llm.adopt_conversation_id_for_session(self.session_id, conv_id)
            self.logger.bind(tag=TAG).info(
                f"Loaded mode-scoped conversation {conv_id} for device {self.device_id}"
            )
            self.current_conversation_id = conv_id
            return

        new_conv_id = self._create_llm_conversation()
        if not new_conv_id:
            return

        self.logger.bind(tag=TAG).info(
            f"Created mode-scoped conversation {new_conv_id} for device {self.device_id}"
        )
        self._seed_instructions_once = True
        self._update_mode_session_conversation({"id": new_conv_id})
        self.current_conversation_id = new_conv_id

    def _ensure_device_scoped_conversation(self):
        state = get_conversation_state_for_device(self.device_id)
        conv_id = state.get("id") if state else None
        if state and self._device_conversation_expired(state.get("last_used")):
            preserved_summary = state.get("last_interaction_summary")
            update_conversation_state_for_device(
                self.device_id,
                conversation_id=None,
                last_used=None,
                last_interaction_summary=preserved_summary,
            )
            conv_id = None

        if conv_id and hasattr(self.llm, "adopt_conversation_id_for_session"):
            self.llm.adopt_conversation_id_for_session(self.session_id, conv_id)
            self.logger.bind(tag=TAG).info(
                f"Loaded conversationId for device: {conv_id}"
            )
            self.current_conversation_id = conv_id
            return

        new_conv_id = self._create_llm_conversation()
        if not new_conv_id:
            return

        ok = update_conversation_state_for_device(
            self.device_id,
            conversation_id=new_conv_id,
            last_used=datetime.now(timezone.utc).isoformat(),
        )
        if ok:
            self.logger.bind(tag=TAG).info(
                f"Created and saved conversationId for device: {new_conv_id}"
            )
            self._seed_instructions_once = True
            self.current_conversation_id = new_conv_id
        else:
            self.logger.bind(tag=TAG).warning(
                "Failed to save conversationId to Firestore"
            )

    def _create_llm_conversation(self) -> Optional[str]:
        if hasattr(self.llm, "ensure_conversation_with_system"):
            return self.llm.ensure_conversation_with_system(
                self.session_id, self.prompt
            )
        if hasattr(self.llm, "ensure_conversation"):
            return self.llm.ensure_conversation(self.session_id)
        return None

    def _update_mode_session_conversation(
        self,
        conversation: Optional[Dict[str, Any]],
        is_snooze_follow_up: Optional[bool] = None,
    ) -> None:
        if not self.device_id:
            return
        try:
            self.logger.bind(tag=TAG).debug(
                f"Updating mode session conversation: device={self.device_id}, "
                f"conversation={conversation}, is_snooze_follow_up={is_snooze_follow_up}"
            )
            session_context_store.update_session(
                self.device_id,
                conversation=conversation if conversation is not None else None,
                is_snooze_follow_up=is_snooze_follow_up,
            )
            if self.mode_session:
                self.mode_session.conversation = conversation or {}
                if is_snooze_follow_up is not None:
                    self.mode_session.is_snooze_follow_up = bool(is_snooze_follow_up)
        except Exception as exc:
            self.logger.bind(tag=TAG).warning(
                f"Failed to persist mode conversation for {self.device_id}: {exc}"
            )

    def _persist_conversation_state_before_close(self):
        conv_id = self.current_conversation_id
        if not conv_id:
            self.logger.bind(tag=TAG).debug("No conversation ID to persist on close")
            return

        last_used = datetime.now(timezone.utc).isoformat()
        summary = self._build_last_interaction_summary()

        if self.use_mode_conversation and self.mode_session:
            self.logger.bind(tag=TAG).info(
                f"Persisting mode conversation {conv_id} for device {self.device_id}"
            )
            self._update_mode_session_conversation(
                {"id": conv_id, "last_used": last_used}
            )
            return

        update_conversation_state_for_device(
            self.device_id,
            conversation_id=conv_id,
            last_used=last_used,
            last_interaction_summary=summary or None,
        )

    def _build_last_interaction_summary(self, max_chars: int = 256) -> str:
        parts = []
        total = 0
        for message in reversed(self.dialogue.dialogue):
            if message.role not in ("user", "assistant"):
                continue
            if not message.content:
                continue
            fragment = f"{message.role}: {message.content.strip()}"
            parts.append(fragment)
            total += len(fragment)
            if total >= max_chars * 2:
                break
        if not parts:
            return ""
        summary = " | ".join(reversed(parts))
        if len(summary) > max_chars:
            summary = summary[-max_chars:]
        return summary

    def _derive_conversation_ttl(self) -> timedelta:
        try:
            selected_llm = (self.config.get("selected_module") or {}).get("LLM")
            llm_config = (self.config.get("LLM") or {}).get(selected_llm or "", {})
            hours = llm_config.get("conversation_ttl_hours", 6)
            hours_val = float(hours)
            if hours_val <= 0:
                return timedelta(0)
            return timedelta(hours=hours_val)
        except (TypeError, ValueError):
            return timedelta(hours=6)

    def _device_conversation_expired(self, last_used_iso: Optional[str]) -> bool:
        if not last_used_iso:
            return False
        if not self.device_conversation_ttl or self.device_conversation_ttl <= timedelta(0):
            return False
        try:
            last_used = datetime.fromisoformat(last_used_iso)
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        now = datetime.now(timezone.utc)
        return now - last_used > self.device_conversation_ttl

    # ------------------------------------------------------------------
    # Follow-up scheduling (for modes like alarms)
    # ------------------------------------------------------------------
    def _schedule_followup(self):
        """Schedule a mode follow-up check based on silence since last TTS stop and STT."""
        if self.followup_task and not self.followup_task.done():
            self.followup_task.cancel()

        # determine the next escalating delay
        next_index = self.followup_count
        if isinstance(self.followup_delays, list) and next_index < len(self.followup_delays):
            next_delay = int(self.followup_delays[next_index])
        else:
            base = int(getattr(self, "followup_delay", 25) or 25)
            # simple escalation if we lack a list
            next_delay = base + next_index * 10

        self.followup_task = asyncio.run_coroutine_threadsafe(self._followup_trigger(next_delay), self.loop)
        self.logger.bind(tag=TAG).info(f"Scheduled follow-up #{self.followup_count + 1} (silence-based) with target delay {next_delay}s")

    async def _followup_trigger(self, delay):
        """
        Wait and trigger mode follow-up after TTS stop only if:
        - no user speaking,
        - no STT activity since TTS stop,
        - silence window exceeds the configured delay,
        - and LLM is idle.
        """
        try:
            # poll conditions rather than blind-sleep to avoid racing with user input
            start_ms = self.last_tts_stop_ms or int(time.time() * 1000)
            while True:
                await asyncio.sleep(0.5)
                now_ms = int(time.time() * 1000)
                # if conversation progressed or user responded, abort
                if getattr(self, "followup_user_has_responded", False):
                    raise asyncio.CancelledError()
                if getattr(self, "client_is_speaking", False):
                    raise asyncio.CancelledError()
                # check idle
                if not self.llm_finish_task:
                    continue
                # compute silence since last speech or STT activity, measured from end of TTS
                last_activity_ms = max(self.last_tts_stop_ms, self.last_stt_activity_ms)
                if last_activity_ms == 0:
                    last_activity_ms = start_ms
                silence_secs = max(0, (now_ms - last_activity_ms) / 1000.0)
                if silence_secs >= delay:
                    # trigger follow-up
                    self.followup_count += 1
                    self.logger.bind(tag=TAG).info(f"Triggering follow-up #{self.followup_count} after {silence_secs:.1f}s of silence")
                    followup_query = f"[No response from user - follow-up #{self.followup_count}]"
                    self.executor.submit(self.chat, followup_query, depth=0, extra_inputs=None, is_user_input=False)
                    break
        except asyncio.CancelledError:
            self.logger.bind(tag=TAG).info("Follow-up cancelled (user responded)")

    # ------------------------------------------------------------------

    def chat(self, query, depth=0, extra_inputs=None, is_user_input=True):
        self.logger.bind(tag=TAG).info(f"大模型收到用户消息: {query}")
        self.llm_finish_task = False

        # Genuine user input cancels any pending follow-up
        if query and is_user_input:
            self.followup_user_has_responded = True
            if self.followup_task and not self.followup_task.done():
                self.followup_task.cancel()
                self.logger.bind(tag=TAG).info("User responded - cancelling follow-up")

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

        functions = None
        if self.intent_type == "function_call" and hasattr(self, "func_handler"):
            functions = self.func_handler.get_functions()
        response_message = []

        try:
            memory_str = None
            if self.memory is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self.memory.query_memory(query), self.loop
                    )
                    memory_str = future.result(timeout=5.0)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"记忆查询失败或超时: {e}")

            use_full_history = True
            try:
                if hasattr(self.llm, "has_conversation") and self.llm.has_conversation(
                    self.session_id
                ):
                    use_full_history = False
            except Exception:
                pass

            if use_full_history:
                current_input = self.dialogue.get_llm_dialogue_with_memory(
                    memory_str, self.config.get("voiceprint", {})
                )
            else:
                current_input = [{"role": "user", "content": query}]

            instructions = ""
            if self.mode_specific_instructions:
                instructions += self.mode_specific_instructions
                self.mode_specific_instructions = ""

            if memory_str:
                instructions += f"\n\n<memory>\n{memory_str}\n</memory>"

            kwargs: Dict[str, Any] = {}
            if instructions:
                kwargs["instructions"] = instructions
                self.logger.bind(tag=TAG).debug(
                    f"Passing instructions to LLM (length: {len(instructions)})"
                )
            if extra_inputs:
                kwargs["extra_inputs"] = extra_inputs

            if self.intent_type == "function_call" and functions is not None:
                llm_responses = self.llm.response_with_functions(
                    self.session_id,
                    current_input,
                    functions=functions,
                    **kwargs,
                )
            else:
                llm_responses = self.llm.response(
                    self.session_id, current_input, **kwargs
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        tool_call_flag = False
        function_name = None
        function_id = None
        function_arguments = ""
        content_arguments = ""
        self.client_abort = False
        emotion_flag = True

        for response in llm_responses:
            if self.client_abort:
                self.logger.bind(tag=TAG).info(
                    "LLM response generation interrupted by client."
                )
                break
            if self.intent_type == "function_call" and functions is not None:
                content, tools_call = response
                if tools_call is not None and len(tools_call) > 0:
                    try:
                        arg_sample = ""
                        try:
                            arg_sample = str(
                                getattr(
                                    getattr(tools_call[0], "function", None),
                                    "arguments",
                                    "",
                                )
                            )[:80]
                        except Exception:
                            arg_sample = ""
                        self.logger.bind(tag=TAG).info(
                            f"tool_call delta: id={getattr(tools_call[0], 'id', None)}, "
                            f"name={getattr(getattr(tools_call[0], 'function', None), 'name', None)}, "
                            f"args_len={len(str(getattr(getattr(tools_call[0], 'function', None), 'arguments', '') or ''))}, "
                            f"args_prefix={arg_sample}"
                        )
                    except Exception:
                        pass
                if isinstance(response, dict) and "content" in response:
                    content = response["content"]
                    tools_call = None
                if content is not None and len(content) > 0:
                    content_arguments += content

                if not tool_call_flag and content_arguments.startswith("<tool_call>"):
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

        if tool_call_flag:
            bHasError = False
            if function_id is None:
                from core.utils.textUtils import extract_json_from_string

                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        function_name = content_arguments_json["name"]
                        function_arguments = json.dumps(
                            content_arguments_json["arguments"], ensure_ascii=False
                        )
                        function_id = str(uuid.uuid4().hex)
                    except Exception:
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
                if len(response_message) > 0:
                    text_buff = "".join(response_message)
                    self.tts_MessageText = text_buff
                    self.dialogue.put(Message(role="assistant", content=text_buff))
                response_message.clear()
                try:
                    self.logger.bind(tag=TAG).info(
                        f"Consolidated function_call: id={function_id}, name={function_name}, "
                        f"args_len={len(function_arguments) if function_arguments else 0}, "
                        f"args_prefix={(function_arguments or '')[:120]}"
                    )
                except Exception:
                    pass
                function_call_data = {
                    "name": function_name,
                    "id": function_id,
                    "arguments": function_arguments,
                }

                result = asyncio.run_coroutine_threadsafe(
                    self.func_handler.handle_llm_function_call(
                        self, function_call_data
                    ),
                    self.loop,
                ).result()
                self._handle_function_result(result, function_call_data, depth=depth)

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

        # Do not schedule here; follow-ups are driven by TTS 'stop' in alarm mode

        self.logger.bind(tag=TAG).debug(
            lambda: json.dumps(
                self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
            )
        )

        return True

    def _handle_function_result(self, result, function_call_data, depth):
        using_conversation = False
        if hasattr(self.llm, "has_conversation"):
            try:
                using_conversation = bool(
                    self.llm.has_conversation(self.session_id)
                )
            except Exception:
                using_conversation = False

        function_id = function_call_data.get("id")
        if using_conversation and function_id:
            tool_output = None
            if result.action == Action.REQLLM:
                tool_output = result.result
            else:
                tool_output = result.response if result.response else result.result
            if tool_output is not None and not isinstance(tool_output, str):
                try:
                    tool_output = json.dumps(tool_output, ensure_ascii=False)
                except Exception:
                    tool_output = str(tool_output)
            try:
                self.logger.bind(tag=TAG).debug(
                    f"Preparing function_call_output: id={function_id}, "
                    f"output_len={len(tool_output) if tool_output is not None else 0}"
                )
            except Exception:
                pass
            extra_inputs = [
                {
                    "type": "function_call_output",
                    "call_id": function_id,
                    "output": "" if tool_output is None else tool_output,
                }
            ]
            self.logger.bind(tag=TAG).info(
                f"Passing function_call_output in next turn: id={function_id}, "
                f"len={len(str(tool_output)) if tool_output is not None else 0}"
            )
            self.chat(
                "",
                depth=depth + 1,
                extra_inputs=extra_inputs,
                is_user_input=False,
            )
            return

        if result.action == Action.RESPONSE:
            text = result.response
            self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
            self.dialogue.put(Message(role="assistant", content=text))
        elif result.action == Action.REQLLM:
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
                self.chat(text, depth=depth + 1, is_user_input=False)
        elif result.action in (Action.NOTFOUND, Action.ERROR):
            text = result.response if result.response else result.result
            self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
            self.dialogue.put(Message(role="assistant", content=text))

    def _report_worker(self):
        while not self.stop_event.is_set():
            try:
                item = self.report_queue.get(timeout=1)
                if item is None:
                    break
                try:
                    if self.executor is None:
                        continue
                    self.executor.submit(self._process_report, *item)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(
                        f"聊天记录上报线程异常: {e}"
                    )
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.bind(tag=TAG).error(
                    f"聊天记录上报工作线程异常: {e}"
                )
        self.logger.bind(tag=TAG).info("聊天记录上报线程已退出")

    def _process_report(self, type, text, audio_data, report_time):
        try:
            report(self, type, text, audio_data, report_time)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"上报处理异常: {e}")
        finally:
            self.report_queue.task_done()

    def clearSpeakStatus(self):
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug("清除服务端讲话状态")

    async def close(self, ws=None):
        try:
            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"清理工具处理器时出错: {cleanup_error}"
                    )

            if self.stop_event:
                self.stop_event.set()

            self.clear_queues()

            try:
                if ws:
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            await ws.close()
                    except Exception:
                        pass
                elif self.websocket:
                    try:
                        if hasattr(self.websocket, "closed") and not self.websocket.closed:
                            await self.websocket.close()
                        elif hasattr(self.websocket, "state") and self.websocket.state.name != "CLOSED":
                            await self.websocket.close()
                        else:
                            await self.websocket.close()
                    except Exception:
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"关闭WebSocket连接时出错: {ws_error}")

            if self.tts:
                await self.tts.close()

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
            if self.stop_event:
                self.stop_event.set()

    def clear_queues(self):
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"开始清理: TTS队列大小={self.tts.tts_text_queue.qsize()}, "
                f"音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )
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
                f"清理结束: TTS队列大小={self.tts.tts_text_queue.qsize()}, "
                f"音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_vad_states(self):
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.logger.bind(tag=TAG).debug("VAD states reset.")

    def chat_and_close(self, text):
        try:
            self.chat(text, is_user_input=False)
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def _check_timeout(self):
        try:
            while not self.stop_event.is_set():
                if self.last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if (
                        current_time - self.last_activity_time
                        > self.timeout_seconds * 1000
                    ):
                        if not self.stop_event.is_set():
                            self.logger.bind(tag=TAG).info("连接超时，准备关闭")
                            self.stop_event.set()
                            try:
                                await self.close(self.websocket)
                            except Exception as close_error:
                                self.logger.bind(tag=TAG).error(
                                    f"超时关闭连接时出错: {close_error}"
                                )
                        break
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"超时检查任务出错: {e}")
        finally:
            self.logger.bind(tag=TAG).info("超时检查任务已退出")

    # ------------------------------------------------------------------
    # Listen control helpers
    # ------------------------------------------------------------------
    async def _send_listen_start(self, mode: str = "manual", state: str = "start"):
        """
        Ask client to start listening/recording. Used to force reactivation after TTS in alarm mode.
        """
        try:
            if not self.websocket:
                return
            message = {
                "session_id": self.session_id,
                "type": "listen",
                "mode": mode,
                "state": state,
            }
            await self.websocket.send(json.dumps(message))
            self.logger.bind(tag=TAG).info(f"Sent listen control: {message}")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"Failed to send listen start: {e}")