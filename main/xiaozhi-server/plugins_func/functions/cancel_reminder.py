from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_owner_phone_for_device
from services.alarms.firestore_client import (
    fetch_active_alarms_for_user,
    cancel_scheduled_conversation,
)
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

LIST_REMINDERS_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": (
            "List all of the user's active scheduled reminders. "
            "Call this before cancel_reminder when you don't already have the reminder_id."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@register_function("list_reminders", LIST_REMINDERS_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def list_reminders(conn) -> ActionResponse:
    uid = get_owner_phone_for_device(conn.device_id)
    if not uid:
        return ActionResponse(
            action=Action.RESPONSE,
            response="I couldn't look up your reminders right now.",
        )
    try:
        alarms = fetch_active_alarms_for_user(uid)
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed to fetch reminders for user {uid}: {e}")
        return ActionResponse(
            action=Action.RESPONSE,
            response="Something went wrong fetching your reminders.",
        )

    if not alarms:
        return ActionResponse(
            action=Action.REQLLM,
            result="No active reminders found.",
        )

    lines = []
    for alarm in alarms:
        label = alarm.content or alarm.label or "untitled"
        time_str = alarm.schedule.time_local or "unknown time"
        repeat_str = alarm.schedule.repeat.value
        lines.append(
            f"- reminder_id={alarm.alarm_id} | {label} | {time_str} | {repeat_str}"
        )

    return ActionResponse(
        action=Action.REQLLM,
        result="Active reminders:\n" + "\n".join(lines),
    )


CANCEL_REMINDER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "Cancel (turn off) a scheduled reminder by its reminder_id. "
            "If you don't already have the reminder_id, call list_reminders first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "alarm_id": {
                    "type": "string",
                    "description": "The reminder_id UUID of the reminder to cancel.",
                },
            },
            "required": ["alarm_id"],
        },
    },
}


@register_function("cancel_reminder", CANCEL_REMINDER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def cancel_reminder(conn, alarm_id: str) -> ActionResponse:
    uid = get_owner_phone_for_device(conn.device_id)
    if not uid:
        return ActionResponse(
            action=Action.RESPONSE,
            response="I couldn't cancel that reminder right now.",
        )
    try:
        cancel_scheduled_conversation(uid, alarm_id)
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"Failed to cancel reminder {alarm_id} for user {uid}: {e}"
        )
        return ActionResponse(
            action=Action.RESPONSE,
            response="Something went wrong cancelling that reminder. Want to try again?",
        )

    return ActionResponse(
        action=Action.REQLLM,
        result=f"Reminder {alarm_id} cancelled successfully.",
    )
