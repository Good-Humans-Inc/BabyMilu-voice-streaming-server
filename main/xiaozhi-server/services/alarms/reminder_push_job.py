"""
Reminder push + Firestore updates aligned with babymilu-backend sendReminderPush.

Query: status == on AND nextOccurrenceUTC <= now (same as backend scheduled job).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from exponent_server_sdk import (
    DeviceNotRegisteredError,
    PushClient,
    PushMessage,
    PushTicketError,
)

from openai import APIError, OpenAI

from services.alarms import reminder_advancement
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()


class ExpoRichPushMessage(PushMessage):
    """
    Expo HTTP API supports richContent (e.g. image); stock PushMessage.get_payload
    does not serialize it. Matches https://exp.host/--/api/v2/push/send payloads.
    """

    def __new__(cls, rich_content=None, **kwargs):
        inst = super().__new__(cls, **kwargs)
        object.__setattr__(inst, "_rich_content", rich_content)
        return inst

    def get_payload(self):
        payload = super().get_payload()
        rc = getattr(self, "_rich_content", None)
        if rc:
            payload["richContent"] = rc
        return payload


OPENAI_MODEL = os.environ.get("REMINDER_OPENAI_MODEL", "gpt-4o-mini")

# Copied from babymilu-backend src/functions/services/text.py (REMINDER_LLM_PROMPT)
REMINDER_LLM_PROMPT = """
# Character Reminder Message Generator

**Instruction:**

You are writing a personalized reminder message from the point of view of a fictional character named `{characterName}` to their companion `{userName}`.
This is a reminder notification that should feel natural, caring, and in-character. The message should gently remind the user about their scheduled task while maintaining the character's personality.

**Context:**
- User name: `{userName}`
- Character name: `{characterName}`
- Character personality: `{characterBio}`
- Character relationship to user: `{characterRelationship}`
- User prefers to be called: `{characterCallMe}`
- Reminder task: `{reminderLabel}`
- Scheduled time: `{reminderTime}`

**Guidelines:**

1. **Persona:** The message must strictly follow the character's personality (`{characterBio}`).
2. **Relationship:** Consider the `{characterRelationship}` when crafting the tone (e.g., caring friend, supportive partner, mentor).
3. **Reminder Context:** Naturally incorporate the reminder task (`{reminderLabel}`) and acknowledge the time if relevant.
4. **Style:** Write as a caring text message (10–20 words). Sound natural, not robotic or overly formal.
5. **Tone:** Be supportive and gentle. This is a helpful reminder, not a command.
6. **Constraints:** No hashtags, no emojis. Use first-person POV. DON'T mention that this is an AI-generated message. It should feel like it came directly from the character. It should not have exact time in the message, just a natural reminder related to the time (e.g. "Don't forget about your meeting later!" or "It's almost time for your appointment!").
**Output Format:**
Return ONLY the reminder message text as a plain string. No JSON, no markdown code blocks, no introductory text.

**Example:**
Hey, don't forget to take your medication now. I know you've been busy, but your health matters to me.
"""

_fcm_messaging_mod = None
_fcm_init_attempted = False


def _get_fcm_messaging():
    """Lazy Firebase Admin for native FCM tokens (same path as sendReminderPush)."""
    global _fcm_messaging_mod, _fcm_init_attempted
    if _fcm_init_attempted:
        return _fcm_messaging_mod
    _fcm_init_attempted = True
    try:
        import firebase_admin
        from firebase_admin import messaging as fcm_messaging

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        _fcm_messaging_mod = fcm_messaging
        return _fcm_messaging_mod
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Firebase Admin not available for FCM: {exc}")
        _fcm_messaging_mod = None
        return None


def _get_openai_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable")
    return OpenAI(api_key=api_key)


def _build_reminder_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    reminder_time = payload.get("reminderTime", "the scheduled time")
    instruction_text = REMINDER_LLM_PROMPT.format(
        userName=str(payload.get("userName", "")).strip() or "",
        characterName=str(payload.get("characterName", "")).strip() or "",
        characterBio=str(payload.get("characterBio", "")).strip() or "",
        characterRelationship=str(payload.get("characterRelationship", "")).strip()
        or "friend",
        characterCallMe=str(payload.get("characterCallMe", "")).strip()
        or payload.get("characterName", ""),
        reminderLabel=str(payload.get("reminderLabel", "")).strip() or "your reminder",
        reminderTime=reminder_time,
    )
    return [{"role": "user", "content": instruction_text}]


def get_ai_message(
    character_data: Dict[str, Any],
    user_name: str,
    reminder_label: str,
    reminder_time: Optional[str] = None,
) -> str:
    """
    Same behavior as babymilu-backend services.text.get_ai_message (chat completions).
    """
    payload = {
        "userName": user_name,
        "characterName": character_data.get("profile", {}).get("name", "BabyMilu") or character_data.get("name", "BabyMilu"),
        "characterBio": character_data.get("profile", {}).get("personality", "") or character_data.get("bio", ""),
        "characterRelationship": character_data.get("profile", {}).get("characterToUser", "friend") or character_data.get("relationship", "friend"),
        "characterCallMe": character_data.get("profile", {}).get("nicknameCharacterCallsUser", "") or character_data.get("callMe", "") or user_name,
        "reminderLabel": reminder_label,
        "reminderTime": reminder_time or "the scheduled time",
    }
    client = _get_openai_client()
    max_retries = 3
    reminder_text: Optional[str] = None

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=_build_reminder_messages(payload),
                max_tokens=150,
                temperature=0.7,
            )
        except APIError as exc:
            logging.error(
                "[Reminder] OpenAI API error (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                exc,
            )
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"OpenAI API error after {max_retries} attempts: {exc}"
                ) from exc
            continue

        if response.choices and len(response.choices) > 0:
            message = response.choices[0].message
            if message.content:
                reminder_text = message.content.strip()
                if reminder_text.startswith("```"):
                    reminder_text = (
                        reminder_text.replace("```json", "")
                        .replace("```", "")
                        .strip()
                    )
                if reminder_text.startswith("{") or reminder_text.startswith("["):
                    try:
                        parsed = json.loads(reminder_text)
                        if isinstance(parsed, dict) and "text" in parsed:
                            reminder_text = parsed["text"]
                        elif (
                            isinstance(parsed, list)
                            and len(parsed) > 0
                            and isinstance(parsed[0], dict)
                        ):
                            reminder_text = parsed[0].get("text", reminder_text)
                    except json.JSONDecodeError:
                        continue
                if (
                    reminder_text
                    and len(reminder_text) > 5
                    and not reminder_text.startswith("{")
                ):
                    break

    if not reminder_text:
        raise RuntimeError(
            f"Failed to generate valid reminder message after {max_retries} attempts"
        )
    return reminder_text


def _resolve_uid_from_reminder_doc(
    reminder_doc: Any, reminder_data: Optional[Dict] = None
) -> str:
    try:
        parent = reminder_doc.reference.parent.parent
        if parent and getattr(parent, "id", None):
            return str(parent.id)
    except Exception:
        pass
    payload = reminder_data or {}
    uid = payload.get("phoneNumber") or payload.get("uid") or payload.get("userId") or ""
    return str(uid).strip() if uid else ""


def _format_occurrence_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_send_reminder_push_job(
    *,
    execute: bool = False,
    now: Optional[datetime] = None,
    client: Optional[firestore.Client] = None,
) -> Dict[str, Any]:
    """
    Port of sendReminderPush: query due reminders, optional Expo/FCM send,
    sentLogs, then one-time off or recurring advance (nextOccurrenceUTC / nextTriggerUTC).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_iso = now.isoformat()
    client = client or firestore.Client()

    query = client.collection_group("reminders").where(
        filter=FieldFilter("status", "==", "on")
    ).where(filter=FieldFilter("nextOccurrenceUTC", "<=", now_iso))

    user_cache: Dict[str, Dict[str, Any]] = {}
    character_cache: Dict[str, Dict[str, Any]] = {}

    triggered = 0
    skipped = 0
    count = 0
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for reminder_doc in query.stream():
        count += 1
        reminder_id = reminder_doc.id
        try:
            reminder_data = reminder_doc.to_dict() or {}
            
            uid = _resolve_uid_from_reminder_doc(reminder_doc, reminder_data)
            if not uid or uid == "+14444444444":
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "processed": False,
                        "skipped": "no_uid",
                    }
                )
                continue
            if "app" not in reminder_data.get("deliveryChannel", []):
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "not_app_delivery_channel",
                    }
                )
                continue
            if uid not in user_cache:
                user_snap = client.collection("users").document(uid).get()
                if not user_snap.exists:
                    skipped += 1
                    results.append(
                        {
                            "reminderId": reminder_id,
                            "userId": uid,
                            "processed": False,
                            "skipped": "user_not_found",
                        }
                    )
                    continue
                user_cache[uid] = user_snap.to_dict() or {}
            user_data = user_cache[uid]
            fcm_token = user_data.get("fcm")
            if not fcm_token:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "no_fcm_token",
                    }
                )
                continue

            character_ids = user_data.get("characterIds") or []
            character_data: Dict[str, Any] = {}
            if character_ids:
                active_character_id = character_ids[-1]
                if active_character_id in character_cache:
                    character_data = character_cache[active_character_id]
                else:
                    char_snap = (
                        client.collection("characters")
                        .document(active_character_id)
                        .get()
                    )
                    if char_snap.exists:
                        character_data = char_snap.to_dict() or {}
                        character_cache[active_character_id] = character_data

            character_name = character_data.get("profile", {}).get("name", "Milu") or character_data.get("name", "Milu") if character_data else "Milu"
            label = reminder_data.get("label", "Reminder")
            next_occurrence_str = reminder_data.get("nextOccurrenceUTC")
            user_name = user_data.get("name") or "there"
            reminder_time_display = None
            if next_occurrence_str:
                try:
                    no = datetime.fromisoformat(
                        str(next_occurrence_str).replace("Z", "+00:00")
                    )
                    reminder_time_display = no.strftime("%I:%M %p")
                except Exception:
                    pass

            try:
                ai_message = get_ai_message(
                    character_data=character_data or {},
                    user_name=user_name,
                    reminder_label=label,
                    reminder_time=reminder_data.get("schedule", {}).get("timeLocal"),
                )
            except Exception as ai_error:
                logger.bind(tag=TAG).warning(
                    f"AI message failed for reminder {reminder_id}: {ai_error}"
                )
                ai_message = f"Hey {user_name}, reminder: {label}"

            if not execute:
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "dryRun": True,
                    }
                )
                continue

            user_avatar = ""
            if character_data:
                user_avatar = (
                    character_data.get("emotionUrls", {})
                    .get("normal", {})
                    .get("thumbnail", "")
                )

            try:
                is_expo = PushClient.is_exponent_push_token(str(fcm_token))
                if is_expo:
                    system = str(user_data.get("system", "") or "")
                    _rich = {"image": user_avatar} if user_avatar else None
                    if system.lower() == "android":
                        push_message = ExpoRichPushMessage(
                            to=str(fcm_token),
                            title=character_name,
                            body=ai_message,
                            channel_id="reminders",
                            rich_content=_rich,
                            data={"action": "custom_display"},
                            sound="reminder_sound.wav",
                            priority="high",
                        )
                    else:
                        push_message = PushMessage(
                            to=str(fcm_token),
                            title=character_name,
                            body=ai_message,
                            data={
                                "type": "reminder",
                                "title": character_name,
                                "body": ai_message,
                                "userAvatar": user_avatar or "",
                                "reminderId": reminder_id,
                                "label": label,
                                "nextOccurrenceUTC": next_occurrence_str,
                            },
                            sound="reminder_sound.wav",
                            priority="high",
                            channel_id="reminders",
                            mutable_content=True,
                        )
                    resp = PushClient().publish(push_message)
                    resp.validate_response()
                else:
                    fcm = _get_fcm_messaging()
                    if fcm is None:
                        raise RuntimeError("FCM not configured (firebase_admin)")
                    message = fcm.Message(
                        data={
                            "type": "reminder",
                            "title": str(character_name),
                            "body": str(ai_message),
                            "userAvatar": str(user_avatar or ""),
                            "reminderId": str(reminder_id),
                            "label": str(label),
                            "nextOccurrenceUTC": str(next_occurrence_str or ""),
                        },
                        android=fcm.AndroidConfig(priority="high"),
                        apns=fcm.APNSConfig(
                            headers={"apns-priority": "10"},
                            payload=fcm.APNSPayload(
                                aps=fcm.Aps(
                                    content_available=True,
                                    sound="reminder_sound.wav",
                                ),
                            ),
                        ),
                        token=str(fcm_token),
                    )
                    fcm.send(message)
            except PushTicketError as exc:
                logger.bind(tag=TAG).warning(
                    f"Expo push error uid={uid} reminder={reminder_id}: {exc}"
                )
                if isinstance(exc, DeviceNotRegisteredError):
                    logger.bind(tag=TAG).warning(
                        f"Expo device not registered for uid={uid}"
                    )
                errors.append(
                    {"id": reminder_id, "error": f"Push send failed: {exc}"}
                )
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "push_failed",
                    }
                )
                continue
            except Exception as push_error:
                logger.bind(tag=TAG).warning(
                    f"Push failed uid={uid} reminder={reminder_id}: {push_error}"
                )
                errors.append(
                    {
                        "id": reminder_id,
                        "error": f"Push send failed: {push_error}",
                    }
                )
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "push_failed",
                    }
                )
                continue

            triggered += 1

            # log_entry = {
            #     "reminderId": reminder_id,
            #     "uid": uid,
            #     "label": label,
            #     "aiMessage": ai_message,
            #     "sentAt": now_iso,
            #     "nextOccurrenceUTC": next_occurrence_str,
            #     "fcmToken": (str(fcm_token)[:20] + "...") if fcm_token else None,
            # }
            # try:
            #     reminder_doc.reference.collection("sentLogs").document().set(log_entry)
            # except Exception as log_error:
            #     logger.bind(tag=TAG).warning(
            #         f"sentLogs write failed reminder={reminder_id}: {log_error}"
            #     )

            if next_occurrence_str:
                try:
                    due = datetime.fromisoformat(
                        str(next_occurrence_str).replace("Z", "+00:00")
                    )
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    repeat = reminder_data.get("schedule", {}).get("repeat")
                    reminder_ref = reminder_doc.reference
                    if repeat == "none":
                        reminder_ref.update(
                            {"status": "off", "updatedAt": now_iso}
                        )
                    else:
                        user_timezone = user_data.get(
                            "timezone", "America/Los_Angeles"
                        )
                        advanced = reminder_advancement.compute_advance_after_firing(
                            reminder_data,
                            str(user_timezone),
                            due_occurrence_utc=due,
                            now_utc=now,
                        )
                        if advanced is None:
                            logger.bind(tag=TAG).warning(
                                f"Could not advance recurring reminder {reminder_id}"
                            )
                        else:
                            next_occ, next_trig = advanced
                            reminder_ref.update(
                                {
                                    "nextOccurrenceUTC": _format_occurrence_iso(
                                        next_occ
                                    ),
                                    "nextTriggerUTC": _format_occurrence_iso(
                                        next_trig
                                    ),
                                    "updatedAt": now_iso,
                                    "lastAction": None,
                                    "lastActionAt": None,
                                }
                            )
                except Exception as reschedule_error:
                    logger.bind(tag=TAG).warning(
                        f"Reminder update failed {reminder_id}: {reschedule_error}"
                    )

            results.append(
                {
                    "reminderId": reminder_id,
                    "userId": uid,
                    "processed": True,
                    "label": label,
                }
            )
        except Exception as inner_e:
            logger.bind(tag=TAG).warning(
                f"processing reminder {reminder_id}: {inner_e}"
            )
            errors.append({"id": reminder_id, "error": str(inner_e)})

    return {
        "ok": True,
        "count": count,
        "triggered": triggered,
        "skipped": skipped,
        "execute": execute,
        "results": results,
        "errors": errors,
    }
