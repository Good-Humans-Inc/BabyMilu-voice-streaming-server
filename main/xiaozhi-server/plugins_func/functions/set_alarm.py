import dateparser
from zoneinfo import ZoneInfo
from datetime import datetime

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device
from services.alarms.firestore_client import create_alarm
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

SET_ALARM_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "set_alarm",
        "description": (
            "[DEPRECATED — use schedule_conversation instead] "
            "Set a basic alarm for the user. "
            "For any new reminder, habit, check-in, or emotional support request, "
            "use schedule_conversation — it supports richer context and better delivery. "
            "Only use set_alarm if schedule_conversation is unavailable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_expression": {
                    "type": "string",
                    "description": (
                        "The time the user wants the alarm, as they expressed it "
                        "(e.g. 'in 5 minutes', 'tomorrow at 7am', 'next Friday at 9pm'). "
                        "Do NOT convert — pass the raw expression."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "What the alarm is for — the label or reminder message "
                        "(e.g. 'take medication', 'team standup', 'study session')."
                    ),
                },
            },
            "required": ["time_expression", "reason"],
        },
    },
}


@register_function("set_alarm", SET_ALARM_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def set_alarm(conn, time_expression: str, reason: str) -> ActionResponse:
    # Resolve user's timezone
    tz_str = get_timezone_for_device(conn.device_id) or "UTC"
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)

    _parse_settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
        "TIMEZONE": tz_str,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "LANGUAGES": ["en"],
    }

    # Parse the natural language time expression relative to "now".
    # Retry with "today at" prefix for bare times like "8 am" that dateparser
    # cannot resolve without a date anchor.
    resolved = dateparser.parse(time_expression, settings=_parse_settings)
    if resolved is None:
        resolved = dateparser.parse(f"today at {time_expression}", settings=_parse_settings)

    if resolved is None:
        logger.bind(tag=TAG).warning(
            f"Could not parse time expression '{time_expression}' for device {conn.device_id}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            response="Sorry, I couldn't understand that time. Could you say it a different way?",
        )

    logger.bind(tag=TAG).info(
        f"Alarm for device {conn.device_id}: '{reason}' at {resolved.isoformat()} (from '{time_expression}')"
    )

    uid = get_owner_phone_for_device(conn.device_id)
    if uid:
        try:
            alarm_id = create_alarm(
                uid=uid,
                device_id=conn.device_id,
                resolved_dt=resolved,
                label=reason,
                context=reason,
                tz_str=tz_str,
            )
            logger.bind(tag=TAG).info(f"Alarm {alarm_id} written to Firestore for user {uid}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to write alarm to Firestore: {e}")
    else:
        logger.bind(tag=TAG).warning(
            f"Could not resolve uid for device {conn.device_id}; alarm not persisted"
        )

    return ActionResponse(
        action=Action.REQLLM,
        result=f"Alarm set: '{reason}' on {resolved.strftime('%A, %B %-d at %-I:%M %p')} ({tz_str}).",
        response=None,
    )
