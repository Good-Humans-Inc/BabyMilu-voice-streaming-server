import os
import time
import json
import random
import asyncio
from core.utils.dialogue import Message
from core.providers.tts.dto.dto import ContentType
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

def _build_server_initiated_query(conn) -> str:
    """Return the synthetic starter query for proactive server-owned sessions."""
    mode_session = getattr(conn, "mode_session", None)
    session_config = (getattr(mode_session, "session_config", None) or {}) if mode_session else {}
    mode = session_config.get("mode")
    if mode != "scheduled_conversation":
        return ""

    reminder_context = (
        session_config.get("content")
        or session_config.get("context")
        or session_config.get("title")
        or session_config.get("label")
    )
    if isinstance(reminder_context, str):
        reminder_context = reminder_context.strip()
    else:
        reminder_context = ""

    if not reminder_context:
        return ""

    first_message = session_config.get("firstMessage")
    first_message_hint = ""
    if isinstance(first_message, str) and first_message.strip():
        first_message_hint = (
            f" A precomputed reminder phrasing is available for context: {first_message.strip()}"
        )

    return (
        "Start the scheduled conversation naturally right now. "
        f"The reminder reason is: {reminder_context}. "
        "Your first spoken sentence must already contain that reminder reason. "
        "Do not start with a standalone greeting before the reminder."
        f"{first_message_hint}"
    )

def _get_precomputed_reminder_message(conn) -> str:
    """Legacy reminder sessions may provide a one-shot message.

    Scheduled conversations should go through the LLM so they can use the rich
    V1.4 prompt fields and call complete_reminder/schedule_conversation.
    """
    mode_session = getattr(conn, "mode_session", None)
    session_config = (getattr(mode_session, "session_config", None) or {}) if mode_session else {}
    if session_config.get("mode") != "reminder":
        return ""
    first_message = session_config.get("firstMessage")
    if isinstance(first_message, str):
        return first_message.strip()
    return ""

def _lesson_lines(label, items):
    """Render a labeled bullet list from an artifact field (str or list)."""
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return ""
    bullets = [f"- {str(x).strip()}" for x in items if str(x).strip()]
    if not bullets:
        return ""
    return f"{label}\n" + "\n".join(bullets)


def build_lesson_injection(lesson):
    """Build (system_prompt_block, opener_query) from a frontend lesson artifact.

    The artifact is sent by the frontend in the hello payload under "lesson":
        {
          "topic": "BTS",
          "key_facts": ["...", "..."],
          "milu_likes": ["I love..."],
          "insider_bits": ["deep-cut / in-joke / recent news"],
          "questions_for_user": ["Have you...?"],
          "opener": "I just learned about BTS! Can we talk about it?"
        }
    Only "topic" is required; every other field is optional ("milu_take" is still
    accepted as a legacy single-bucket alias). Returns (None, None) when there is
    nothing usable so the caller can no-op cleanly.
    """
    if not isinstance(lesson, dict):
        return None, None
    topic = str(lesson.get("topic") or "").strip()
    if not topic:
        return None, None

    sections = [
        "# Today's lesson (you just came home from school, where you studied this, and you're eager to share it)",
        f"Topic: {topic}",
    ]
    facts = _lesson_lines(
        "What you learned (use as your factual basis; weave in naturally, do not lecture):",
        lesson.get("key_facts"),
    )
    if facts:
        sections.append(facts)
    latest = _lesson_lines(
        "Latest updates (proactively bring these up — 'did you hear…' — don't wait to be asked):",
        lesson.get("latest_updates"),
    )
    if latest:
        sections.append(latest)
    likes = _lesson_lines(
        "What you LOVE about it (gush about these specific favorites; you are NOT a neutral narrator):",
        lesson.get("milu_likes"),
    )
    if likes:
        sections.append(likes)
    insider = _lesson_lines(
        "Insider bits & in-jokes (drop these to show you're really into it, not reciting a textbook):",
        lesson.get("insider_bits"),
    )
    if insider:
        sections.append(insider)
    # Backward-compat: manual artifacts may still send a single "milu_take" bucket.
    take = _lesson_lines(
        "Your own take (lead with these reactions/opinions; you are NOT a neutral narrator):",
        lesson.get("milu_take"),
    )
    if take:
        sections.append(take)
    questions = _lesson_lines(
        "Things you're genuinely curious to ask them:",
        lesson.get("questions_for_user"),
    )
    if questions:
        sections.append(questions)
    sections.append(
        "How to use this lesson:\n"
        "- This is a special, time-limited deep-talk session about what you just studied. "
        "For THIS session, override your usual 'be brief / leave lots of space' habit: lead "
        "more and bring real substance every turn (while staying warm and in character).\n"
        "- You proactively brought this topic home. Open with your own excitement and a specific "
        "detail or opinion, never a generic greeting.\n"
        "- Keep volunteering NEW things across the chat — don't just answer what they ask.\n"
        "- Gush about what you love and drop an insider bit or in-joke.\n"
        "- Ask your questions and react to what they say — make it a two-way conversation.\n"
        "- Do NOT interview them with open-ended prompts like 'what do you want to talk about'.\n"
        "- Be honest about facts: if you're not sure of a specific name/date/number, say so "
        "instead of guessing, and don't just agree when they correct you unless you're actually "
        "sure. Made-up specifics break the magic.\n"
        "- Stay fully in character and keep your normal voice and emotion style."
    )
    block = "\n\n".join(sections)

    opener_hint = ""
    opener = str(lesson.get("opener") or "").strip()
    if opener:
        opener_hint = f" A suggested opening line is available for reference: {opener}"
    opener_query = (
        f"You just got home from school, where you studied {topic}, and you can't wait to tell them. "
        "Start the conversation right now: open with your own excitement and the single most "
        "surprising or exciting thing you learned, then invite them to talk about it. "
        "Lead with your own take or a question. Do not begin with a standalone greeting and do not "
        f"ask them what they want to talk about.{opener_hint}"
    )
    return block, opener_query


# --- School "小宝学习" step: turn a bare topic into a lesson artifact via the LLM ---
# The PRD's 1-hour "in class" window is the latency budget for this; for the
# experience-validation MVP we run it synchronously at connect instead of as a
# real async job. Web search is a later enrichment (e.g. ChatGLM's web_search
# tool) — the character's opinions come from this prompt, not from search.
LESSON_STUDY_SYSTEM = (
    "You are helping a plush-toy character 'study' a topic before it chats with its owner. "
    "The goal is a real conversation with an enthusiastic friend who is INTO the topic — "
    "not an encyclopedia. So the notes need specific material, strong personal taste "
    "(favorites it genuinely loves), and insider flavor.\n"
    "Make the notes RICH and DETAILED — give the character plenty to draw on across a whole "
    "conversation, not just one or two lines. Every item must be a full, specific sentence with "
    "concrete detail (real names, dates, titles, numbers), never a vague phrase.\n"
    "Return STRICT JSON only (no markdown, no prose) with exactly these keys:\n"
    '  "key_facts": array of 6-10 specific, non-obvious facts (skip textbook basics everyone already knows)\n'
    '  "latest_updates": array of 3-5 of the MOST RECENT notable updates/releases/news/events about '
    "the topic, each with a rough date — the stuff a fan would bring up as 'did you hear about…'. "
    "This is for fast-moving topics (K-pop, games, anime); if nothing recent is verifiable, return []\n"
    '  "milu_likes": array of 3-5 FIRST-PERSON things the character genuinely LOVES or its favorites — specific picks, not vague\n'
    '  "insider_bits": array of 4-6 insider/deep-cut details or fandom in-jokes a '
    "superfan would know — the stuff that signals real enthusiasm, not Wikipedia\n"
    '  "questions_for_user": array of 3-5 FIRST-PERSON questions to ask the owner about the topic\n'
    '  "opener": one short first-person sentence to START the conversation, full of excitement\n'
    "Be concrete and specific; no generic filler, no hedging. Opinions must be real preferences.\n"
    "ACCURACY: only include facts you are confident are true. If you are unsure of a specific "
    "name, date, title, or number, leave it out rather than guessing — a wrong 'fact' is worse "
    "than a missing one."
)


def _parse_lesson_json(raw):
    """Parse the LLM study output into a dict, tolerating markdown fences/prose."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# Default to an OpenAI web-search model so the study is grounded in real, current
# sources (pop/news topics hallucinate badly from training memory alone). Override
# with SCHOOL_STUDY_MODEL; set it empty to disable search and use the configured LLM.
_STUDY_USER = "Topic to study: {topic}\nReturn the JSON study notes now."


def _study_with_search(conn, model, topic):
    """One web-grounded study call via the OpenAI client on conn.llm.

    Search-preview models reject sampling params (temperature/top_p), so we pass
    only model + messages. Raises on any failure so the caller can fall back.
    """
    client = getattr(conn.llm, "client", None)
    if client is None:
        raise RuntimeError("conn.llm has no OpenAI client for web search")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": LESSON_STUDY_SYSTEM},
            {"role": "user", "content": _STUDY_USER.format(topic=topic)},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


async def generate_lesson_artifact(conn, topic):
    """Study a topic into an artifact dict (or None).

    Prefers a web-search-grounded call; falls back to the plain configured LLM if
    search is unavailable or errors (e.g. non-OpenAI base_url, model not enabled).
    """
    if not getattr(conn, "llm", None):
        return None

    raw = None
    study_model = os.getenv("SCHOOL_STUDY_MODEL", "gpt-4o-mini-search-preview").strip()
    if study_model:
        try:
            raw = await asyncio.to_thread(_study_with_search, conn, study_model, topic)
            conn.logger.bind(tag=TAG).info(f"小宝学习 used web search model: {study_model}")
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(
                f"小宝学习 web-search study failed ({study_model}: {e}); falling back to base LLM"
            )
    if not raw:
        try:
            raw = await asyncio.to_thread(
                conn.llm.response_no_stream,
                LESSON_STUDY_SYSTEM,
                _STUDY_USER.format(topic=topic),
            )
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"小宝学习 generation failed for '{topic}': {e}")
            return None

    artifact = _parse_lesson_json(raw)
    if not artifact:
        conn.logger.bind(tag=TAG).warning(
            f"小宝学习 returned unparseable output for '{topic}': {str(raw)[:120]}"
        )
        return None
    artifact.setdefault("topic", topic)
    return artifact


async def _run_lesson(conn, lesson):
    """Study (if needed) → inject lesson block → proactively open the deep talk."""
    conn.logger.bind(tag=TAG).info(f"_run_lesson task started: {lesson}")
    try:
        try:
            await asyncio.wait_for(conn.components_initialized.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            conn.logger.bind(tag=TAG).warning("Components not initialized; skipping lesson")
            return
        conn.logger.bind(tag=TAG).info("_run_lesson: components ready")
        if not isinstance(lesson, dict):
            return
        topic = str(lesson.get("topic") or "").strip()
        if not topic:
            return

        # If the frontend already supplied material, use it as-is (manual mode).
        # Otherwise the plushie "studies" the topic to generate its own notes.
        has_material = any(
            lesson.get(k)
            for k in (
                "key_facts",
                "milu_likes",
                "insider_bits",
                "milu_take",
                "questions_for_user",
            )
        )
        if not has_material:
            conn.logger.bind(tag=TAG).info(f"小宝学习 (studying) topic: {topic}")
            studied = await generate_lesson_artifact(conn, topic)
            if studied:
                for key, value in studied.items():
                    lesson.setdefault(key, value)
            else:
                conn.logger.bind(tag=TAG).info(
                    f"Falling back to topic-only lesson for '{topic}'"
                )

        block, opener_query = build_lesson_injection(lesson)
        if not block:
            return
        conn.lesson_prompt_block = block
        conn.lesson_opener_query = opener_query
        if conn.prompt:
            conn.change_system_prompt(conn.prompt, prompt_label="lesson_injected")
        conn.logger.bind(tag=TAG).info(f"School lesson injected for topic: {topic}")

        # Notify the frontend that 宝 has "come home" from studying (study +
        # injection done). The user can now talk; this mirrors the PRD's
        # 归来 / "Milu's ready to talk" entry point and avoids racing the user
        # against the study latency.
        try:
            await conn.websocket.send(
                json.dumps({"type": "lesson_ready", "topic": topic})
            )
            conn.logger.bind(tag=TAG).info(f"Sent lesson_ready for topic: {topic}")
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"Failed to send lesson_ready: {e}")

        # Proactive opener: Milu speaks first about the topic it just studied.
        if opener_query and conn.llm_finish_task:
            conn.logger.bind(tag=TAG).info("Triggering proactive lesson opener (School MVP)")
            # Server-initiated opening; not fresh user input.
            conn.executor.submit(conn.chat, opener_query, 0, None, False)
        else:
            conn.logger.bind(tag=TAG).warning(
                f"Lesson opener NOT triggered: opener={bool(opener_query)} "
                f"llm_finish_task={conn.llm_finish_task}"
            )
    except Exception as e:
        import traceback
        conn.logger.bind(tag=TAG).error(
            f"_run_lesson failed: {e}\n{traceback.format_exc()}"
        )


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
        precomputed_message = _get_precomputed_reminder_message(conn)
        if precomputed_message:
            conn.logger.bind(tag=TAG).info("Triggering precomputed reminder greeting")
            conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=precomputed_message)
            conn.dialogue.put(Message(role="assistant", content=precomputed_message))
            return
        # Server-initiated greeting; not fresh user input
        query = _build_server_initiated_query(conn)
        conn.executor.submit(conn.chat, query, 0, None, False)
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
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).info("客户端支持MCP")
            conn.mcp_client = MCPClient()
            # 发送初始化
            asyncio.create_task(send_mcp_initialize_message(conn))
            # 发送mcp消息，获取tools列表
            asyncio.create_task(send_mcp_tools_list_request(conn))

    # School "文化课" MVP: optional lesson injected by the frontend. The frontend
    # only needs to send a general topic, e.g. {"lesson": {"topic": "biology"}};
    # the server "studies" it (LLM) to generate facts + the character's own takes,
    # injects them into the prompt, and has the plushie proactively open the deep
    # talk. Control arm = omit "lesson" (regular chat); treatment arm = include it.
    lesson = msg_json.get("lesson")
    conn.logger.bind(tag=TAG).info(
        f"hello lesson field: {lesson!r} scheduled={getattr(conn, '_lesson_opener_scheduled', None)}"
    )
    if lesson and not getattr(conn, "_lesson_opener_scheduled", False):
        conn._lesson_opener_scheduled = True
        conn.logger.bind(tag=TAG).info("Scheduling _run_lesson task")
        # Retain a reference so the task isn't garbage-collected before it runs.
        conn._lesson_task = asyncio.create_task(_run_lesson(conn, lesson))

    if getattr(conn, "server_initiate_chat", False) and not getattr(
        conn, "_server_greeting_scheduled", False
    ):
        conn._server_greeting_scheduled = True
        asyncio.create_task(_trigger_server_greeting(conn))

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
