"""LLM tool: set_reminder

Use this when the user asks to be reminded about something at a future time
(e.g. "remind me to drink water in 2 hours", "remind me to call mom tomorrow").

For wake-up alarms use set_alarm instead.
"""
from __future__ import annotations

import dateparser
from zoneinfo import ZoneInfo
from datetime import datetime

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device
from services.alarms.firestore_client import create_reminder
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

SET_REMINDER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "Set a reminder for the user. "
            "Use this when the user wants to be reminded about something at a future time "
            "(e.g. 'remind me to drink water', 'remind me to call mom', 'remind me to take my medication'). "
            "For wake-up alarms (e.g. 'wake me up at 7am') use set_alarm instead. "
            "You MUST have both a time expression and a reason before calling — "
            "ask the user if either is missing. "
            "Pass the time exactly as the user said it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_expression": {
                    "type": "string",
                    "description": (
                        "The time the user wants the reminder, as they expressed it "
                        "(e.g. 'in 2 hours', 'tomorrow at 3pm', 'in 30 minutes'). "
                        "Do NOT convert — pass the raw expression."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "What the reminder is for "
                        "(e.g. 'drink water', 'take vitamins', 'call mom')."
                    ),
                },
            },
            "required": ["time_expression", "reason"],
        },
    },
}


def _format_reminder_time(dt: datetime) -> str:
    return f"{dt.strftime('%A, %B')} {dt.day} at {dt.strftime('%I:%M %p').lstrip('0')}"


@register_function("set_reminder", SET_REMINDER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def set_reminder(conn, time_expression: str, reason: str) -> ActionResponse:
    # Resolve user's timezone
    tz_str = get_timezone_for_device(conn.device_id) or "UTC"
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)

    # Parse the natural language time expression relative to "now"
    resolved = dateparser.parse(
        time_expression,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": tz_str,
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )

    if resolved is None:
        logger.bind(tag=TAG).warning(
            f"Could not parse time expression '{time_expression}' for device {conn.device_id}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            response="Sorry, I couldn't understand that time. Could you say it a different way?",
        )

    logger.bind(tag=TAG).info(
        f"Reminder for device {conn.device_id}: '{reason}' at {resolved.isoformat()} "
        f"(from '{time_expression}')"
    )

    uid = get_owner_phone_for_device(conn.device_id)
    if uid:
        try:
            reminder_id = create_reminder(
                uid=uid,
                device_id=conn.device_id,
                resolved_dt=resolved,
                label=reason,
                context=reason,
                tz_str=tz_str,
            )
            logger.bind(tag=TAG).info(f"Reminder {reminder_id} written to Firestore for user {uid}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to write reminder to Firestore: {e}")
    else:
        logger.bind(tag=TAG).warning(
            f"Could not resolve uid for device {conn.device_id}; reminder not persisted"
        )

    return ActionResponse(
        action=Action.REQLLM,
        result=f"Reminder set: '{reason}' on {_format_reminder_time(resolved)} ({tz_str}).",
        response=None,
    )
