import re
import dateparser
from zoneinfo import ZoneInfo
from datetime import datetime

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device
from services.alarms.firestore_client import modify_scheduled_conversation
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# Same bare-time regex as schedule_conversation
_BARE_TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)

MODIFY_REMINDER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "modify_reminder",
        "description": (
            "Modify an existing scheduled reminder. "
            "Pass only the fields that are changing — omit everything else. "
            "If the reminder's PURPOSE has changed, also regenerate conversation_outline, "
            "character_reminder, emotional_context, and completion_signal from the current conversation. "
            "If only scheduling details changed (time, priority, delivery_preference), omit those context fields. "
            "If you don't have the reminder_id, call list_reminders first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "alarm_id": {
                    "type": "string",
                    "description": "The reminder_id UUID of the reminder to modify.",
                },
                "time_expression": {
                    "type": "string",
                    "description": (
                        "New trigger time. Use the same exact templates as schedule_conversation: "
                        "'in N minutes', 'in N hours', 'in N days', 'in N weeks', "
                        "'today at H:MMam/pm', 'tomorrow at H:MMam/pm', "
                        "'[Weekday] at H:MMam/pm', 'next [Weekday] at H:MMam/pm', "
                        "'[Month] [D] at H:MMam/pm', or a bare time like '8pm'. "
                        "Omit if time is not changing."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "New user-facing label (e.g. 'take vitamins'). Omit if not changing.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "New priority level. Omit if not changing.",
                },
                "recurrence": {
                    "type": "string",
                    "description": (
                        "New recurrence setting. "
                        "'once' for one-time, 'daily' for every day, 'weekly' for every week. "
                        "Omit if not changing."
                    ),
                },
                "delivery_preference": {
                    "type": "string",
                    "description": "New or updated delivery preference. Omit if not changing.",
                },
                "conversation_outline": {
                    "type": "string",
                    "description": (
                        "Regenerate from current conversation if the reminder's purpose changed. "
                        "Omit if only scheduling details changed."
                    ),
                },
                "character_reminder": {
                    "type": "string",
                    "description": (
                        "Regenerate from current conversation if the reminder's purpose changed. "
                        "Omit if only scheduling details changed."
                    ),
                },
                "emotional_context": {
                    "type": "string",
                    "description": (
                        "Regenerate from current conversation if the reminder's purpose changed. "
                        "Omit if only scheduling details changed."
                    ),
                },
                "completion_signal": {
                    "type": "string",
                    "description": (
                        "Regenerate from current conversation if the reminder's purpose changed. "
                        "Omit if only scheduling details changed."
                    ),
                },
            },
            "required": ["alarm_id"],
        },
    },
}


@register_function("modify_reminder", MODIFY_REMINDER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def modify_reminder(
    conn,
    alarm_id: str,
    time_expression: str = None,
    content: str = None,
    priority: str = None,
    recurrence: str = None,
    delivery_preference: str = None,
    conversation_outline: str = None,
    character_reminder: str = None,
    emotional_context: str = None,
    completion_signal: str = None,
) -> ActionResponse:
    tz_str = get_timezone_for_device(conn.device_id) or "UTC"
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)

    # Resolve new trigger time if provided (same 3-pass approach as schedule_conversation)
    resolved = None
    if time_expression is not None:
        _parse_settings = {
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": tz_str,
            "RETURN_AS_TIMEZONE_AWARE": True,
        }
        resolved = dateparser.parse(time_expression, languages=["en"], settings=_parse_settings)
        if resolved is None:
            resolved = dateparser.parse(
                f"today at {time_expression}", languages=["en"], settings=_parse_settings
            )
        if resolved is None:
            m = _BARE_TIME_RE.search(time_expression)
            if m:
                resolved = dateparser.parse(
                    f"today at {m.group(1)}", languages=["en"], settings=_parse_settings
                )
        if resolved is None:
            logger.bind(tag=TAG).warning(
                f"Could not parse time expression '{time_expression}' for device {conn.device_id}"
            )
            return ActionResponse(
                action=Action.RESPONSE,
                response="Sorry, I couldn't understand that time. Could you say it a different way?",
            )

    uid = get_owner_phone_for_device(conn.device_id)
    if not uid:
        return ActionResponse(
            action=Action.RESPONSE,
            response="I couldn't update that reminder right now.",
        )

    try:
        modify_scheduled_conversation(
            uid=uid,
            alarm_id=alarm_id,
            resolved_dt=resolved,
            tz_str=tz_str if resolved is not None else None,
            content=content,
            priority=priority,
            recurrence=recurrence,
            delivery_preference=delivery_preference,
            conversation_outline=conversation_outline,
            character_reminder=character_reminder,
            emotional_context=emotional_context,
            completion_signal=completion_signal,
        )
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"Failed to modify reminder {alarm_id} for user {uid}: {e}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            response="Something went wrong updating that reminder. Want to try again?",
        )

    changes = []
    if resolved is not None:
        changes.append(f"time → {resolved.strftime('%A, %B %-d at %-I:%M %p')} ({tz_str})")
    if content is not None:
        changes.append(f"content → '{content}'")
    if priority is not None:
        changes.append(f"priority → {priority}")
    if recurrence is not None:
        changes.append(f"recurrence → {recurrence}")
    if delivery_preference is not None:
        changes.append(f"delivery_preference → {delivery_preference}")
    if conversation_outline is not None:
        changes.append("conversation_outline regenerated")
    if character_reminder is not None:
        changes.append("character_reminder regenerated")
    if emotional_context is not None:
        changes.append("emotional_context regenerated")
    if completion_signal is not None:
        changes.append("completion_signal regenerated")

    summary = ", ".join(changes) if changes else "no fields changed"
    return ActionResponse(
        action=Action.REQLLM,
        result=f"Reminder {alarm_id} updated: {summary}.",
    )
