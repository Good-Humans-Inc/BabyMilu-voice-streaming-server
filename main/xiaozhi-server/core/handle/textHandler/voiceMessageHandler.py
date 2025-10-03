import json
import re
from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType


class VoiceTextMessageHandler(TextMessageHandler):
    """处理与语音相关的消息，例如设置TTS voice-id"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.VOICE

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        action = msg_json.get("action")
        if action == "set_tts_voice":
            raw_voice_id = (msg_json.get("voice_id") or msg_json.get("voice-id") or "").strip()
            if not raw_voice_id:
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "voice",
                            "status": "error",
                            "message": "voice_id is required",
                            "content": {"action": action},
                        }
                    )
                )
                return

            safe_voice_id = re.sub(r"[^A-Za-z0-9_-]", "", str(raw_voice_id))
            if not safe_voice_id:
                await conn.websocket.send(
                    json.dumps(
                        {
                            "type": "voice",
                            "status": "error",
                            "message": "voice_id format invalid",
                            "content": {"action": action},
                        }
                    )
                )
                return

            conn.voice_id = safe_voice_id
            await conn.websocket.send(
                json.dumps(
                    {
                        "type": "voice",
                        "status": "success",
                        "message": "voice_id updated",
                        "content": {"action": action, "voice_id": safe_voice_id},
                    }
                )
            )
            return

        await conn.websocket.send(
            json.dumps(
                {
                    "type": "voice",
                    "status": "error",
                    "message": "unknown action",
                    "content": {"action": action},
                }
            )
        )





