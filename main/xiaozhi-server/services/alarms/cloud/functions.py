from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from google.cloud import firestore
from exponent_server_sdk import (
    DeviceNotRegisteredError,
    PushClient,
    PushMessage,
    PushTicketError,
)

from services.logging import setup_logging
from services.alarms import reminder_scheduler, scheduler, tasks
from services.messaging.mqtt import publish_ws_start
from services.alarms.config import ALARM_TIMING
from core.utils.mac import normalize_mac

TAG = __name__
logger = setup_logging()
_db = firestore.Client()
_user_cache: Dict[str, Dict[str, Any]] = {}
_character_cache: Dict[str, Dict[str, Any]] = {}
_openai_import_error_logged = False


def scan_due_alarms(request) -> Dict[str, Any]:
    """HTTP entrypoint for Cloud Scheduler."""
    now = datetime.now(timezone.utc)
    wake_requests = scheduler.prepare_wake_requests(
        now, lookahead=ALARM_TIMING["lookahead"]
    )
    triggered = 0
    results: List[Dict[str, Any]] = []
    for wake_request in wake_requests:
        if not _is_device_allowed(wake_request.target.device_id):
            logger.bind(tag=TAG).info(
                f"Skipping wake for filtered device {wake_request.target.device_id}"
            )
            try:
                scheduler.rollback_wake_request(wake_request)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to rollback filtered wake request for {wake_request.target.device_id}: {exc}"
                )
            results.append(
                {
                    "alarmId": wake_request.alarm.alarm_id,
                    "label": wake_request.alarm.label,
                    "deviceId": wake_request.target.device_id,
                    "mode": wake_request.target.mode,
                    "fired": False,
                    "skipped": "device_filter",
                }
            )
            continue
        fired = _wake_device(wake_request)
        if fired:
            triggered += 1
            try:
                scheduler.finalize_wake_request(wake_request, now=now)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to finalize alarm {wake_request.alarm.alarm_id}: {exc}"
                )
        else:
            try:
                scheduler.rollback_wake_request(wake_request)
            except Exception as exc:
                logger.bind(tag=TAG).warning(
                    f"Failed to rollback wake request for {wake_request.target.device_id}: {exc}"
                )
        results.append(
            {
                "alarmId": wake_request.alarm.alarm_id,
                "label": wake_request.alarm.label,
                "deviceId": wake_request.target.device_id,
                "mode": wake_request.target.mode,
                "fired": bool(fired),
            }
        )
    logger.bind(tag=TAG).info(
        f"Processed {len(wake_requests)} wake requests; fired {triggered}"
    )
    return {
        "ok": True,
        "count": len(wake_requests),
        "triggered": triggered,
        "results": results,
    }


def _wake_device(wake_request: tasks.WakeRequest) -> bool:
    ws_url = _resolve_ws_url()
    if not ws_url:
        logger.bind(tag=TAG).warning("ALARM_WS_URL not configured; skipping wake")
        return False
    broker = _resolve_broker_url()
    ok = publish_ws_start(broker, wake_request.target.device_id, ws_url)
    if ok:
        logger.bind(tag=TAG).info(
            f"Published ws_start to {wake_request.target.device_id} "
            f"for alarm {wake_request.alarm.alarm_id}"
        )
    else:
        logger.bind(tag=TAG).warning(
            f"Failed to publish ws_start to {wake_request.target.device_id}"
        )
    return bool(ok)


def _resolve_ws_url() -> str:
    return os.environ.get("ALARM_WS_URL") or os.environ.get("DEFAULT_WS_URL", "")


def _resolve_broker_url() -> str:
    return os.environ.get("ALARM_MQTT_URL") or os.environ.get("MQTT_URL", "")


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


def _parse_device_set(raw: str) -> set[str]:
    tokens = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            tokens.add(normalize_mac(token))
        except Exception:
            tokens.add(token.lower())
    return tokens


def scan_due_scheduled_items(request) -> Dict[str, Any]:
    """
    Unified Cloud Scheduler entrypoint for alarms + reminders.

    Phase-1 default:
    - alarms execute as-is
    - reminders are scanned in dry-run mode unless REMINDER_EXECUTE=true
    """
    now = datetime.now(timezone.utc)

    alarms_result = scan_due_alarms(request)

    include_reminders = _env_bool("INCLUDE_REMINDERS_IN_UNIFIED_SCAN", True)
    if include_reminders:
        lookahead = _resolve_reminder_lookahead(ALARM_TIMING["lookahead"])
        reminders_execute = _env_bool("REMINDER_EXECUTE", False)
        reminders_result = reminder_scheduler.process_due_reminders(
            now=now,
            lookahead=lookahead,
            execute=reminders_execute,
            trigger_fn=_send_reminder_notification,
        )
    else:
        reminders_result = {
            "ok": True,
            "count": 0,
            "triggered": 0,
            "skipped": 0,
            "execute": False,
            "results": [],
            "errors": [],
            "disabled": True,
        }

    return {
        "ok": bool(alarms_result.get("ok")) and bool(reminders_result.get("ok")),
        "timestamp": now.isoformat(),
        "alarms": alarms_result,
        "reminders": reminders_result,
        "totals": {
            "count": int(alarms_result.get("count", 0))
            + int(reminders_result.get("count", 0)),
            "triggered": int(alarms_result.get("triggered", 0))
            + int(reminders_result.get("triggered", 0)),
        },
    }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_reminder_lookahead(default_lookahead: timedelta) -> timedelta:
    raw_seconds = os.environ.get("REMINDER_LOOKAHEAD_SECONDS")
    if not raw_seconds:
        return default_lookahead
    try:
        seconds = max(0, int(raw_seconds))
        return timedelta(seconds=seconds)
    except ValueError:
        logger.bind(tag=TAG).warning(
            f"Invalid REMINDER_LOOKAHEAD_SECONDS={raw_seconds}; using default"
        )
        return default_lookahead


def _send_reminder_notification(reminder: reminder_scheduler.ReminderDoc) -> bool:
    """
    Send reminder notification directly via Expo SDK.
    """
    payload = _build_reminder_push_payload(reminder)
    if payload is None:
        return False
    token = str(payload.get("fcmToken") or "").strip()
    if not token:
        logger.bind(tag=TAG).warning(
            f"Reminder {reminder.reminder_id} missing fcm token; skipping"
        )
        return False
    if not PushClient.is_exponent_push_token(token):
        logger.bind(tag=TAG).warning(
            f"Reminder {reminder.reminder_id} token is not Expo format; skipping"
        )
        return False

    try:
        system = str(payload.get("system") or "").lower()
        if system == "android":
            push_message = PushMessage(
                to=token,
                # title=payload.get("title", "Milu"),
                # body=payload.get("body", ""),
                data={
                    "type": "reminder",
                    "title": payload.get("title", "Milu"),
                    "body": payload.get("body", ""),
                    "largeIcon": payload.get("largeIcon", ""),
                    "reminderId": payload.get("reminderId", reminder.reminder_id),
                    "action": "custom_display",
                    "label": payload.get("label", ""),
                    "nextOccurrenceUTC": payload.get("nextOccurrenceUTC", ""),
                },
                sound="reminder_sound.wav",
                priority="high",
                channel_id="reminders",
                mutable_content=True,
            )
        else:
            push_message = PushMessage(
                to=token,
                title=payload.get("title", "Milu"),
                body=payload.get("body", ""),
                data={
                    "type": "reminder",
                    "title": payload.get("title", "Milu"),
                    "body": payload.get("body", ""),
                    "userAvatar": payload.get("userAvatar", ""),
                    "reminderId": payload.get("reminderId", reminder.reminder_id),
                    "label": payload.get("label", ""),
                    "nextOccurrenceUTC": payload.get("nextOccurrenceUTC", ""),
                },
                sound="reminder_sound.wav",
                priority="high",
                channel_id="reminders",
                mutable_content=True,
            )

        response = PushClient().publish(push_message)
        response.validate_response()
        logger.bind(tag=TAG).info(
            f"Sent Expo reminder push for reminder={reminder.reminder_id} uid={reminder.user_id}"
        )
        return True
    except PushTicketError as exc:
        logger.bind(tag=TAG).warning(
            f"Expo push ticket error for reminder={reminder.reminder_id}: {exc}"
        )
        if isinstance(exc, DeviceNotRegisteredError):
            logger.bind(tag=TAG).warning(
                f"Expo device not registered for uid={reminder.user_id}"
            )
        return False
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Reminder Expo push request failed: {exc}")
        return False


def _build_reminder_push_payload(
    reminder: reminder_scheduler.ReminderDoc,
) -> Optional[Dict[str, Any]]:
    """
    Build reminder notification payload, following reminder-api send flow:
    - load user and active character context
    - build title/body/avatar metadata
    """
    uid = reminder.user_id
    if not uid:
        logger.bind(tag=TAG).warning(
            f"Reminder {reminder.reminder_id} missing uid; skipping notification"
        )
        return None

    user_data = _get_user(uid)
    if not user_data:
        logger.bind(tag=TAG).warning(
            f"User {uid} not found for reminder {reminder.reminder_id}"
        )
        return None

    fcm_token = user_data.get("fcm")
    if not fcm_token:
        logger.bind(tag=TAG).info(
            f"User {uid} has no fcm token for reminder {reminder.reminder_id}"
        )
        return None

    character_data = _get_active_character(user_data)
    character_name = (
        character_data.get("name", "Milu")
        if isinstance(character_data, dict)
        else "Milu"
    )
    user_avatar = ""
    if isinstance(character_data, dict):
        user_avatar = (
            character_data.get("emotionUrls", {})
            .get("normal", {})
            .get("thumbnail", "")
        )

    label = reminder.label or reminder.raw.get("label") or "Reminder"
    schedule_time_local = (reminder.raw.get("schedule") or {}).get("timeLocal")
    user_name = user_data.get("name") or "there"
    body = _generate_reminder_ai_message(
        user_name=user_name,
        label=label,
        schedule_time_local=schedule_time_local,
        character_data=character_data or {},
    )

    base_payload = {
        "type": "reminder",
        "title": character_name,
        "body": body,
        "reminderId": reminder.reminder_id,
        "label": label,
        "nextOccurrenceUTC": (
            reminder.next_occurrence_utc.isoformat()
            if reminder.next_occurrence_utc
            else ""
        ),
        "uid": uid,
        "fcmToken": fcm_token,
        "reminder": reminder.raw,
    }
    system = str(user_data.get("system") or "").lower()
    base_payload["system"] = system
    if system == "android":
        base_payload["largeIcon"] = user_avatar
        base_payload["action"] = "custom_display"
    else:
        base_payload["userAvatar"] = user_avatar
    return base_payload


def _get_user(uid: str) -> Optional[Dict[str, Any]]:
    if uid in _user_cache:
        return _user_cache[uid]
    snapshot = _db.collection("users").document(uid).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    _user_cache[uid] = data
    return data


def _get_active_character(user_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    character_ids = user_data.get("characterIds") or []
    if not character_ids:
        return None
    active_character_id = character_ids[-1]
    if active_character_id in _character_cache:
        return _character_cache[active_character_id]
    snapshot = _db.collection("characters").document(active_character_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    _character_cache[active_character_id] = data
    return data


def _generate_reminder_ai_message(
    *,
    user_name: str,
    label: str,
    schedule_time_local: Optional[str],
    character_data: Dict[str, Any],
) -> str:
    """
    Generate reminder message with OpenAI using user/character context.
    Falls back to deterministic text if OpenAI is unavailable or fails.
    """
    fallback = f"Hey {user_name}, it's time to {label}"

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return fallback

    character_name = str(character_data.get("name") or "Milu")
    character_bio = str(
        character_data.get("bio")
        or character_data.get("profile", {}).get("bio")
        or ""
    )
    character_relationship = str(
        character_data.get("relationship")
        or character_data.get("profile", {}).get("relationship")
        or ""
    )
    character_call_me = str(
        character_data.get("callMe")
        or character_data.get("profile", {}).get("callMe")
        or ""
    )
    character_pronouns = str(
        character_data.get("pronouns")
        or character_data.get("profile", {}).get("pronouns")
        or ""
    )
    today = datetime.now().strftime("%B %d, %Y")
    model_name = os.environ.get("REMINDER_OPENAI_MODEL", "gpt-4o-mini")

    prompt = f"""
# Character Reminder Message Generator

You are writing ONE reminder push message from the POV of a fictional character.

## Context
- User name: {user_name}
- Character name: {character_name}
- Character personality: {character_bio}
- Character relationship to user: {character_relationship}
- User prefers to be called: {character_call_me}
- Character pronouns: {character_pronouns}
- Reminder task: {label}
- Scheduled time (local): {schedule_time_local or ""}
- Today: {today}

## Writing goals
1. Stay in-character and sound like a caring text message.
2. Remind the user about the task naturally.
4. Keep it concise: 10-20 words preferred.

## Hard constraints
- Plain text only.
- No emojis, no hashtags, no quotes, no markdown.
- Do not mention AI or system prompts.
- Do not include exact clock time; use natural phrasing like "around now" or "coming up".

## Output
Return only the final message text.
""".strip()

    global _openai_import_error_logged
    try:
        from openai import OpenAI
    except ImportError:
        if not _openai_import_error_logged:
            logger.bind(tag=TAG).warning(
                "OpenAI package is not installed; falling back to default reminder message"
            )
            _openai_import_error_logged = True
        return fallback

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model_name,
            input=prompt,
            max_output_tokens=40,
        )

        text = ""
        output = getattr(response, "output", None) or []
        for message in output:
            for part in getattr(message, "content", []) or []:
                part_type = getattr(part, "type", "")
                if part_type == "output_text":
                    text = (getattr(part, "text", "") or "").strip()
                    if text:
                        break
            if text:
                break

        if text:
            return text
        return fallback
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"OpenAI reminder message generation failed: {exc}")
        return fallback

