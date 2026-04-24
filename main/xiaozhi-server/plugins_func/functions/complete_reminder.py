from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_owner_phone_for_device
from services.alarms.firestore_client import write_alarm_outcome
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

COMPLETE_REMINDER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "complete_reminder",
        "description": (
            "Signal the final outcome of this scheduled conversation. "
            "Call this exactly once when the conversation has reached its natural end:\n"
            "- 'done': the user confirmed they completed the task or meaningfully acknowledged the reminder.\n"
            "- 'resisting': the conversation ended with the user still resistant or refusing (even if they engaged).\n"
            "Do NOT call this for snooze (use schedule_conversation instead) "
            "or for ignored (the system handles that automatically on session expiry)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["done", "resisting"],
                    "description": "The final outcome of this conversation.",
                }
            },
            "required": ["outcome"],
        },
    },
}


@register_function("complete_reminder", COMPLETE_REMINDER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def complete_reminder(conn, outcome: str) -> ActionResponse:
    if outcome not in ("done", "resisting"):
        return ActionResponse(
            action=Action.RESPONSE,
            response="Invalid outcome value.",
        )

    # Pull alarm context from the active mode session
    mode_session = getattr(conn, "mode_session", None)
    session_config = getattr(mode_session, "session_config", None) or {}
    alarm_id = session_config.get("alarmId")
    uid = session_config.get("userId")

    if not alarm_id or not uid:
        # Not in a delivery session — no-op, but don't surface an error to the character
        logger.bind(tag=TAG).warning(
            f"complete_reminder called with outcome={outcome!r} but no active alarm session "
            f"(device={getattr(conn, 'device_id', '?')})"
        )
        return ActionResponse(action=Action.REQLLM, result="Outcome noted.")

    try:
        write_alarm_outcome(uid, alarm_id, outcome)
        logger.bind(tag=TAG).info(
            f"Outcome '{outcome}' written for alarm {alarm_id} "
            f"(device={conn.device_id})"
        )
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"Failed to write outcome for alarm {alarm_id}: {e}"
        )

    return ActionResponse(action=Action.REQLLM, result="Outcome noted.")
