import time
import asyncio
from typing import Dict, Any

from core.handle.receiveAudioHandle import handleAudioMessage, startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import sendAudioMessage, send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.providers.tts.dto.dto import SentenceType
from core.utils.dialogue import Message
from core.utils.next_starter_client import fetch_next_starter_audio, mark_next_starter_consumed
from core.utils.util import audio_bytes_to_data, remove_punctuation_and_length

TAG = __name__


async def _maybe_play_next_starter(conn) -> bool:
    payload = getattr(conn, "next_starter_payload", None) or {}
    character_id = getattr(conn, "active_character_id", None)
    audio_url = payload.get("audioUrl")
    starter_text = payload.get("text") or ""
    if not character_id or not audio_url:
        return False
    if getattr(conn, "next_starter_scheduled", False):
        return False

    conn.next_starter_scheduled = True
    try:
        start_time = time.time()
        while time.time() - start_time < 3.0:
            if getattr(conn, "tts", None):
                break
            await asyncio.sleep(0.1)
        else:
            conn.logger.bind(tag=TAG).warning("next_starter skipped: TTS not ready within 3s")
            return False

        conn.client_abort = False
        audio_bytes = await asyncio.to_thread(fetch_next_starter_audio, audio_url)
        opus_packets = await asyncio.to_thread(audio_bytes_to_data, audio_bytes, "mp3", True)
        await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, starter_text)
        await sendAudioMessage(conn, SentenceType.LAST, [], None)
        if starter_text:
            conn.dialogue.put(Message(role="assistant", content=starter_text))
        try:
            await asyncio.to_thread(mark_next_starter_consumed, character_id, payload)
        except Exception as consume_error:
            conn.logger.bind(tag=TAG).warning(
                f"Failed marking next_starter consumed for character_id={character_id}: {consume_error}"
            )
        conn.next_starter_payload = None
        conn.logger.bind(tag=TAG).info(
            f"Played next_starter on listen start for character_id={character_id}"
        )
        return True
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"Failed to play next_starter on listen start: {exc}")
        return False
    finally:
        conn.next_starter_scheduled = False

class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            conn.client_have_voice = True
            conn.client_voice_stop = False
            await _maybe_play_next_starter(conn)
        elif msg_json["state"] == "stop":
            conn.client_have_voice = True
            conn.client_voice_stop = True
            if len(conn.asr_audio) > 0:
                await handleAudioMessage(conn, b"")
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.asr_audio.clear()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # 保留原始文本
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # 识别是否是唤醒词
                is_wakeup_words = filtered_text in conn.config.get("wakeup_words")
                # 是否开启唤醒词回复
                enable_greeting = conn.config.get("enable_greeting", True)

                if is_wakeup_words and not enable_greeting:
                    # 如果是唤醒词，且关闭了唤醒词回复，就不用回答
                    await send_stt_message(conn, original_text)
                    await send_tts_message(conn, "stop", None)
                    conn.client_is_speaking = False
                elif is_wakeup_words:
                    conn.just_woken_up = True
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, "嘿，你好呀", [])
                    await startToChat(conn, "嘿，你好呀")
                else:
                    # 上报纯文字数据（复用ASR上报功能，但不提供音频数据）
                    enqueue_asr_report(conn, original_text, [])
                    # 否则需要LLM对文字内容进行答复
                    await startToChat(conn, original_text)