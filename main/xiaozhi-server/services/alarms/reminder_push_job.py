"""
Unified reminder delivery and Firestore finalization.

Query: status == on AND nextOccurrenceUTC <= now.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
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

from core.utils.mac import normalize_mac
from services.alarms import reminder_advancement
from services.alarms.config import ALARM_TIMING
from services.logging import setup_logging
from services.messaging.mqtt import publish_ws_start
from services.session_context import store as session_context_store

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
APP_CHANNEL = "app"
PLUSHIE_CHANNEL = "plushie"
REMINDER_SESSION_TTL = ALARM_TIMING["one_time_session_ttl"]

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
ONE_TIME_REPEATS = {"none", "once", "one_time", "one-time", "no_repeat"}

_fcm_messaging_mod = None
_fcm_init_attempted = False


def _get_fcm_messaging():
    """Lazy Firebase Admin for native FCM tokens used by reminder app delivery."""
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
        "characterCallMe": character_data.get("profile", {}).get("nicknameCharacterCallsUser", "") or character_data.get("callMe", "") or character_data.get("name", "BabyMilu"),
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


def _resolve_delivery_channels(reminder_data: Dict[str, Any]) -> List[str]:
    raw = reminder_data.get("deliveryChannel")
    if isinstance(raw, list):
        channels = [str(item).strip().lower() for item in raw if str(item).strip()]
    elif isinstance(raw, str) and raw.strip():
        channels = [raw.strip().lower()]
    else:
        # Legacy app reminders were created without deliveryChannel.
        channels = [APP_CHANNEL]

    seen = set()
    normalized: List[str] = []
    for channel in channels:
        if channel not in {APP_CHANNEL, PLUSHIE_CHANNEL} or channel in seen:
            continue
        seen.add(channel)
        normalized.append(channel)
    return normalized or [APP_CHANNEL]


def _normalized_repeat(reminder_data: Dict[str, Any]) -> Optional[str]:
    schedule = reminder_data.get("schedule") or {}
    raw = schedule.get("repeat")
    if raw is not None:
        normalized = str(raw).strip().lower()
        return normalized or None
    days = schedule.get("days") or []
    if not isinstance(days, list) or not days:
        return None
    if all(isinstance(day, int) for day in days):
        return "monthly"
    weekday_days = [day for day in days if isinstance(day, str)]
    if len(weekday_days) == 7:
        return "daily"
    if weekday_days:
        return "weekly"
    return None


def _is_one_time_reminder(reminder_data: Dict[str, Any]) -> bool:
    repeat = _normalized_repeat(reminder_data)
    if repeat in ONE_TIME_REPEATS:
        return True
    if repeat is None:
        schedule = reminder_data.get("schedule") or {}
        return not bool(schedule.get("days")) and bool(schedule.get("dateLocal"))
    return False


def _resolve_ws_url() -> str:
    return os.environ.get("ALARM_WS_URL") or os.environ.get("DEFAULT_WS_URL", "")


def _resolve_broker_url() -> str:
    return os.environ.get("ALARM_MQTT_URL") or os.environ.get("MQTT_URL", "")


def _parse_device_set(raw: str) -> set[str]:
    tokens: set[str] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            tokens.add(normalize_mac(token))
        except Exception:
            tokens.add(token.lower())
    return tokens


def _parse_user_set(raw: str) -> set[str]:
    return {token.strip() for token in (raw or "").split(",") if token.strip()}


def _is_reminder_user_allowed(uid: str) -> bool:
    allowed = _parse_user_set(os.environ.get("REMINDER_USER_ALLOWLIST", ""))
    denied = _parse_user_set(
        os.environ.get("REMINDER_USER_DENYLIST", "")
        or os.environ.get("NOT_ALLOWED_PHONENUMBERS", "")
    )
    normalized = (uid or "").strip()
    if normalized in denied:
        return False
    if not allowed:
        return True
    return normalized in allowed


def _is_device_allowed(device_id: str) -> bool:
    allowed = _parse_device_set(os.environ.get("ALARM_DEVICE_ALLOWLIST", ""))
    denied = _parse_device_set(os.environ.get("ALARM_DEVICE_DENYLIST", ""))
    try:
        normalized = normalize_mac(device_id)
    except Exception:
        normalized = (device_id or "").lower()
    if normalized in denied:
        return False
    if not allowed:
        return True
    return normalized in allowed


def _reminder_max_lateness() -> timedelta:
    raw = os.environ.get("REMINDER_MAX_LATENESS_SECONDS", "180")
    try:
        seconds = max(0, int(raw))
    except ValueError:
        seconds = 180
    return timedelta(seconds=seconds)


def _resolve_user_timezone(user_data: Dict[str, Any]) -> Optional[str]:
    timezone_value = (
        user_data.get("timezone")
        or user_data.get("timeZone")
        or user_data.get("timezoneId")
        or user_data.get("userTimezone")
    )
    if not timezone_value:
        return None
    timezone_name = str(timezone_value).strip()
    return timezone_name or None


def _same_occurrence(current_value: Any, expected_iso: str) -> bool:
    current_dt = _parse_occurrence(current_value)
    expected_dt = _parse_occurrence(expected_iso)
    if current_dt is None or expected_dt is None:
        return str(current_value or "") == str(expected_iso or "")
    return current_dt == expected_dt


def _parse_occurrence(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _send_app_notification(
    *,
    reminder_id: str,
    reminder_data: Dict[str, Any],
    uid: str,
    user_data: Dict[str, Any],
    character_data: Dict[str, Any],
    label: str,
    next_occurrence_str: Optional[str],
    ai_message: str,
) -> bool:
    fcm_token = user_data.get("fcm")
    if not fcm_token:
        logger.bind(tag=TAG).warning(
            f"Skipping app delivery for reminder {reminder_id}: user {uid} has no FCM token"
        )
        return False

    character_name = (
        character_data.get("profile", {}).get("name", "Milu")
        or character_data.get("name", "Milu")
        if character_data
        else "Milu"
    )
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
                    data={
                        "type": "reminder",
                        "title": character_name,
                        "body": ai_message,
                        "largeIcon": user_avatar or "",
                        "reminderId": reminder_id,
                        "action": "custom_display",
                        "label": label,
                        "nextOccurrenceUTC": next_occurrence_str,
                    },
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
            return True

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
        return True
    except PushTicketError as exc:
        logger.bind(tag=TAG).warning(
            f"Expo push error uid={uid} reminder={reminder_id}: {exc}"
        )
        if isinstance(exc, DeviceNotRegisteredError):
            logger.bind(tag=TAG).warning(
                f"Expo device not registered for uid={uid}"
            )
        return False
    except Exception as exc:
        logger.bind(tag=TAG).warning(
            f"Push failed uid={uid} reminder={reminder_id}: {exc}"
        )
        return False


def _send_plushie_notification(
    *,
    reminder_id: str,
    reminder_data: Dict[str, Any],
    uid: str,
    label: str,
    first_message: str,
    now: datetime,
) -> bool:
    ws_url = _resolve_ws_url()
    broker_url = _resolve_broker_url()
    if not ws_url or not broker_url:
        logger.bind(tag=TAG).warning(
            f"Skipping plushie delivery for reminder {reminder_id}: alarm ws/mqtt env missing"
        )
        return False

    targets = reminder_data.get("targets")
    if not isinstance(targets, list) or not targets:
        logger.bind(tag=TAG).warning(
            f"Skipping plushie delivery for reminder {reminder_id}: no targets"
        )
        return False

    any_sent = False
    for target in targets:
        try:
            device_id = normalize_mac(target["deviceId"])
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Skipping plushie target for reminder {reminder_id}: {exc}"
            )
            continue
        if not _is_device_allowed(device_id):
            logger.bind(tag=TAG).info(
                f"Skipping plushie delivery for filtered device {device_id}"
            )
            continue
        existing = session_context_store.get_session(device_id, now=now)
        if existing:
            logger.bind(tag=TAG).warning(
                f"Skipping plushie delivery for {device_id}: existing session active ({existing.session_type})"
            )
            continue

        session = session_context_store.create_session(
            device_id=device_id,
            session_type="alarm",
            ttl=REMINDER_SESSION_TTL,
            triggered_at=now,
            session_config={
                "mode": "scheduled_conversation",
                "alarmId": reminder_id,
                "reminderId": reminder_id,
                "userId": uid,
                "title": label,
                "label": label,
                "context": reminder_data.get("context"),
                "content": reminder_data.get("content"),
                "typeHint": reminder_data.get("typeHint"),
                "priority": reminder_data.get("priority"),
                "conversationOutline": reminder_data.get("conversationOutline"),
                "characterReminder": reminder_data.get("characterReminder"),
                "emotionalContext": reminder_data.get("emotionalContext"),
                "completionSignal": reminder_data.get("completionSignal"),
                "deliveryPreference": reminder_data.get("deliveryPreference"),
                "firstMessage": first_message,
            },
        )
        if publish_ws_start(broker_url, device_id, ws_url):
            any_sent = True
            continue

        logger.bind(tag=TAG).warning(
            f"Failed plushie ws_start for reminder {reminder_id} device {device_id}"
        )
        session_context_store.delete_session(session.device_id)

    return any_sent


def _build_finalize_updates(
    *,
    reminder_data: Dict[str, Any],
    expected_occurrence_iso: str,
    now: datetime,
    user_timezone: str,
    app_sent: bool,
    plushie_sent: bool,
) -> Dict[str, Any]:
    now_iso = now.isoformat()
    updates: Dict[str, Any] = {
        "updatedAt": now_iso,
        "lastDelivered.occurrenceUTC": expected_occurrence_iso,
    }
    if app_sent:
        updates["lastDelivered.app.at"] = now_iso
    if plushie_sent:
        updates["lastDelivered.plushie.at"] = now_iso

    if _is_one_time_reminder(reminder_data):
        updates["status"] = "off"
        return updates

    due_occurrence = _parse_occurrence(expected_occurrence_iso)
    if due_occurrence is None:
        raise ValueError("Missing or invalid expected occurrence")
    advanced = reminder_advancement.compute_advance_after_firing(
        reminder_data,
        user_timezone,
        due_occurrence_utc=due_occurrence,
        now_utc=now,
    )
    if advanced is None:
        raise ValueError("Could not advance recurring reminder")
    next_occ, next_trig = advanced
    updates["nextOccurrenceUTC"] = _format_occurrence_iso(next_occ)
    updates["nextTriggerUTC"] = _format_occurrence_iso(next_trig)
    updates["lastAction"] = None
    updates["lastActionAt"] = None
    return updates


def _finalize_if_occurrence_matches(
    *,
    client: firestore.Client,
    doc_ref,
    expected_occurrence_iso: str,
    updates: Dict[str, Any],
) -> bool:
    transaction = client.transaction()

    @firestore.transactional
    def _apply(transaction, ref):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        payload = snapshot.to_dict() or {}
        if str(payload.get("status", "")).strip().lower() != "on":
            return False
        if not _same_occurrence(payload.get("nextOccurrenceUTC"), expected_occurrence_iso):
            return False
        transaction.update(ref, updates)
        return True

    return bool(_apply(transaction, doc_ref))


def run_send_reminder_push_job(
    *,
    execute: bool = False,
    now: Optional[datetime] = None,
    client: Optional[firestore.Client] = None,
) -> Dict[str, Any]:
    """
    Unified reminder scheduler:
    - app only: send app, then finalize once
    - plushie only: send plushie, then finalize once
    - both: send app, then plushie, then finalize once
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
    max_lateness = _reminder_max_lateness()

    for reminder_doc in query.stream():
        count += 1
        reminder_id = reminder_doc.id
        try:
            reminder_data = reminder_doc.to_dict() or {}
            delivery_channels = _resolve_delivery_channels(reminder_data)
            uid = _resolve_uid_from_reminder_doc(reminder_doc, reminder_data)
            next_occurrence_str = reminder_data.get("nextOccurrenceUTC")
            if not uid:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "processed": False,
                        "skipped": "no_uid",
                    }
                )
                continue
            if not _is_reminder_user_allowed(uid):
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": "user_filtered",
                    }
                )
                continue
            if not next_occurrence_str:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "missing_occurrence",
                    }
                )
                continue
            next_occurrence = _parse_occurrence(next_occurrence_str)
            if next_occurrence is None:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "skipped": "invalid_occurrence",
                    }
                )
                continue
            if max_lateness and next_occurrence < (now - max_lateness):
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": "too_late",
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
            user_timezone = _resolve_user_timezone(user_data)
            if not _is_one_time_reminder(reminder_data) and not user_timezone:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": "missing_user_timezone",
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

            label = reminder_data.get("label", "Reminder")
            user_name = user_data.get("name") or "there"

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
                        "deliveryChannel": delivery_channels,
                    }
                )
                continue

            app_sent = False
            plushie_sent = False
            channel_errors: List[str] = []

            if APP_CHANNEL in delivery_channels:
                app_sent = _send_app_notification(
                    reminder_id=reminder_id,
                    reminder_data=reminder_data,
                    uid=uid,
                    user_data=user_data,
                    character_data=character_data,
                    label=label,
                    next_occurrence_str=next_occurrence_str,
                    ai_message=ai_message,
                )
                if not app_sent:
                    channel_errors.append("app_failed")

            if PLUSHIE_CHANNEL in delivery_channels:
                plushie_sent = _send_plushie_notification(
                    reminder_id=reminder_id,
                    reminder_data=reminder_data,
                    uid=uid,
                    label=label,
                    first_message=ai_message,
                    now=now,
                )
                if not plushie_sent:
                    channel_errors.append("plushie_failed")

            if not app_sent and not plushie_sent:
                skipped += 1
                errors.append(
                    {
                        "id": reminder_id,
                        "error": ",".join(channel_errors) or "delivery_failed",
                    }
                )
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": ",".join(channel_errors) or "delivery_failed",
                    }
                )
                continue

            try:
                updates = _build_finalize_updates(
                    reminder_data=reminder_data,
                    expected_occurrence_iso=str(next_occurrence_str),
                    now=now,
                    user_timezone=user_timezone,
                    app_sent=app_sent,
                    plushie_sent=plushie_sent,
                )
            except Exception as finalize_error:
                logger.bind(tag=TAG).warning(
                    f"Reminder finalize payload failed {reminder_id}: {finalize_error}"
                )
                errors.append({"id": reminder_id, "error": str(finalize_error)})
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": "finalize_payload_failed",
                    }
                )
                continue

            finalized = _finalize_if_occurrence_matches(
                client=client,
                doc_ref=reminder_doc.reference,
                expected_occurrence_iso=str(next_occurrence_str),
                updates=updates,
            )
            if not finalized:
                skipped += 1
                results.append(
                    {
                        "reminderId": reminder_id,
                        "userId": uid,
                        "processed": False,
                        "deliveryChannel": delivery_channels,
                        "skipped": "stale_occurrence",
                    }
                )
                continue

            triggered += 1

            results.append(
                {
                    "reminderId": reminder_id,
                    "userId": uid,
                    "processed": True,
                    "label": label,
                    "deliveryChannel": delivery_channels,
                    "appSent": app_sent,
                    "plushieSent": plushie_sent,
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
