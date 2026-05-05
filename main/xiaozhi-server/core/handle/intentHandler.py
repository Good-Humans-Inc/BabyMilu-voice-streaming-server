import json
import random
import uuid
import asyncio
from core.providers.tts.dto.dto import ContentType
from core.handle.helloHandle import checkWakeupWords
from plugins_func.register import Action, ActionResponse
from core.handle.sendAudioHandle import send_stt_message
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import TTSMessageDTO

TAG = __name__
MAGIC_SPELL = "Milu milu on the wall, who's the fairest of them all"
MAGIC_SPELL_REPLIES = (
    "Hmm... I checked... and it says... me",
    "Wait wait- I need better lighting for this question...",
    "The wall said 'loading...' hold on...",
    "The wall says... ERROR: TOO POWERFUL",
    "Can I pick you anyway?",
)

def _get_match_variants(text, filtered_text):
    raw = (text or "").strip().lower()
    squashed = (filtered_text or "").strip().lower()
    return raw, squashed


def _normalize_magic_spell_text(text: str) -> str:
    normalized = (text or "").strip().lower()
    replacements = (
        ("who's", "who"),
        ("who is", "who"),
        ("whos", "who"),
        ("who'", "who"),
    )
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    return remove_punctuation_and_length(normalized)[1].lower()


def _is_magic_spell(text, filtered_text) -> bool:
    raw, squashed = _get_match_variants(text, filtered_text)
    normalized_spell = _normalize_magic_spell_text(MAGIC_SPELL)
    normalized_text = _normalize_magic_spell_text(text)
    spell_variants = (
        normalized_spell,
        _normalize_magic_spell_text("Milu milu on the wall, who the fairest of them all"),
        _normalize_magic_spell_text("Milu milu on the wall, who is the fairest of them all"),
    )
    anchor_phrases = (
        ("milu milu", "on the wall", "fairest", "them all"),
        ("milu milu", "wall", "fairest", "all"),
    )
    return (
        MAGIC_SPELL.lower() in raw
        or any(variant in squashed for variant in spell_variants)
        or any(variant in normalized_text for variant in spell_variants)
        or any(all(anchor in normalized_text for anchor in anchors) for anchors in anchor_phrases)
    )


async def _trigger_magic_spell(conn, original_text: str):
    await send_stt_message(conn, original_text)
    conn.client_abort = False
    reply = random.choice(MAGIC_SPELL_REPLIES)
    speak_txt(conn, reply)
    conn.logger.bind(tag=TAG).info("触发镜子咒语彩蛋回复")


async def handle_user_intent(conn, text):
    # 预处理输入文本，处理可能的JSON格式
    try:
        if text.strip().startswith('{') and text.strip().endswith('}'):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                text = parsed_data["content"]  # 提取content用于意图分析
                conn.current_speaker = parsed_data.get("speaker")  # 保留说话人信息
    except (json.JSONDecodeError, TypeError):
        pass

    # 检查是否有明确的退出命令
    _, filtered_text = remove_punctuation_and_length(text)
    if await check_direct_exit(conn, filtered_text):
        return True

    # 检查是否是唤醒词
    if await checkWakeupWords(conn, filtered_text):
        return True

    if _is_magic_spell(text, filtered_text):
        await _trigger_magic_spell(conn, text)
        return True

    if conn.intent_type == "function_call":
        # 使用支持function calling的聊天方法,不再进行意图分析
        return False
    # 使用LLM进行意图分析
    intent_result = await analyze_intent_with_llm(conn, text)
    if not intent_result:
        return False
    # 会话开始时生成sentence_id
    conn.sentence_id = str(uuid.uuid4().hex)
    # 处理各种意图
    return await process_intent_result(conn, intent_result, text)


async def check_direct_exit(conn, text):
    """检查是否有明确的退出命令"""
    _, text = remove_punctuation_and_length(text)
    cmd_exit = conn.cmd_exit
    for cmd in cmd_exit:
        if text == cmd:
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            await conn.close()
            return True
    return False


async def analyze_intent_with_llm(conn, text):
    """使用LLM分析用户意图"""
    if not hasattr(conn, "intent") or not conn.intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None

    # 对话历史记录
    dialogue = conn.dialogue
    try:
        intent_result = await conn.intent.detect_intent(conn, dialogue.dialogue, text)
        return intent_result
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {str(e)}")

    return None


async def process_intent_result(conn, intent_result, original_text):
    """处理意图识别结果"""
    try:
        # 尝试将结果解析为JSON
        intent_data = json.loads(intent_result)

        # 检查是否有function_call
        if "function_call" in intent_data:
            # 直接从意图识别获取了function_call
            conn.logger.bind(tag=TAG).debug(
                f"检测到function_call格式的意图结果: {intent_data['function_call']['name']}"
            )
            function_name = intent_data["function_call"]["name"]
            if function_name == "continue_chat":
                return False

            if function_name == "result_for_context":
                await send_stt_message(conn, original_text)
                conn.client_abort = False
                
                def process_context_result():
                    conn.dialogue.put(Message(role="user", content=original_text))
                    
                    from core.utils.current_time import get_current_time_info
                    from core.utils.firestore_client import get_timezone_for_device

                    try:
                        tz = get_timezone_for_device(conn.device_id) if getattr(conn, "device_id", None) else None
                    except Exception:
                        tz = None
                    current_time, today_date, today_weekday, lunar_date = get_current_time_info()
                    
                    # 构建带上下文的基础提示
                    context_prompt = f"""当前时间：{current_time}
                                        今天日期：{today_date} ({today_weekday})
                                        今天农历：{lunar_date}

                                        请根据以上信息回答用户的问题：{original_text}"""
                    
                    response = conn.intent.replyResult(context_prompt, original_text)
                    speak_txt(conn, response)
                
                conn.executor.submit(process_context_result)
                return True

            function_args = {}
            if "arguments" in intent_data["function_call"]:
                function_args = intent_data["function_call"]["arguments"]
                if function_args is None:
                    function_args = {}
            # 确保参数是字符串格式的JSON
            if isinstance(function_args, dict):
                function_args = json.dumps(function_args)

            function_call_data = {
                "name": function_name,
                "id": str(uuid.uuid4().hex),
                "arguments": function_args,
            }

            await send_stt_message(conn, original_text)
            conn.client_abort = False

            # 使用executor执行函数调用和结果处理
            def process_function_call():
                conn.dialogue.put(Message(role="user", content=original_text))

                # 使用统一工具处理器处理所有工具调用
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        conn.func_handler.handle_llm_function_call(
                            conn, function_call_data
                        ),
                        conn.loop,
                    ).result()
                except Exception as e:
                    conn.logger.bind(tag=TAG).error(f"工具调用失败: {e}")
                    result = ActionResponse(
                        action=Action.ERROR, result=str(e), response=str(e)
                    )

                if result:
                    if result.action == Action.RESPONSE:  # 直接回复前端
                        text = result.response
                        if text is not None:
                            speak_txt(conn, text)
                    elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复
                        text = result.result
                        conn.dialogue.put(Message(role="tool", content=text))
                        llm_result = conn.intent.replyResult(text, original_text)
                        if llm_result is None:
                            llm_result = text
                        speak_txt(conn, llm_result)
                    elif (
                        result.action == Action.NOTFOUND
                        or result.action == Action.ERROR
                    ):
                        text = result.result
                        if text is not None:
                            speak_txt(conn, text)
                    elif function_name != "play_music":
                        # For backward compatibility with original code
                        # 获取当前最新的文本索引
                        text = result.response
                        if text is None:
                            text = result.result
                        if text is not None:
                            speak_txt(conn, text)

            # 将函数执行放在线程池中
            conn.executor.submit(process_function_call)
            return True
        return False
    except json.JSONDecodeError as e:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {e}")
        return False


def speak_txt(conn, text):
    conn.tts_MessageText = text
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
    if getattr(conn, "_session_created", False) and text and text.strip():
        conn.turn_index += 1
        conn.chat_store.insert_turn(
            session_id=conn.session_id,
            turn_index=conn.turn_index,
            speaker="assistant",
            text=text,
        )
    if getattr(conn, "websocket", None) and text and text.strip():
        try:
            llm_message = {
                "type": "llm",
                "text": text,
                "session_id": conn.session_id,
            }
            asyncio.run_coroutine_threadsafe(
                conn.websocket.send(json.dumps(llm_message, ensure_ascii=False)),
                conn.loop,
            )
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(
                f"Failed to send direct response to frontend: {e}"
            )
