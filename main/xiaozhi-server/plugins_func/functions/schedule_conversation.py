import dateparser
from zoneinfo import ZoneInfo
from datetime import datetime

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device
from services.alarms.firestore_client import create_scheduled_conversation
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

SCHEDULE_CONVERSATION_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "schedule_conversation",
        "description": (
            "Schedule a future conversation or reminder. "
            "Use this for ALL reminders, check-ins, habits, emotional support, and wake-up calls. "
            "You MUST have: (1) a clear intent, (2) a specific time, and (3) user consent — "
            "ask for any missing piece before calling. "
            "Pass the time as the user expressed it; the server resolves it to UTC. "
            "For conversation_outline, character_reminder, emotional_context, and completion_signal: "
            "generate these NOW using the full conversation context. "
            "These are your intake notes — be specific and personal, not generic."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_expression": {
                    "type": "string",
                    "description": (
                        "The time the user wants the check-in, as they expressed it "
                        "(e.g. 'in 5 minutes', 'tomorrow at 7am', 'Friday at 9pm', '8pm'). "
                        "Do NOT convert — pass the raw expression. "
                        "Do NOT include recurrence words like 'daily' or 'every day' here — "
                        "those belong in the recurrence field."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Short user-facing label for this reminder "
                        "(e.g. 'take vitamins', 'check in before exam', 'emotional support')."
                    ),
                },
                "type_hint": {
                    "type": "string",
                    "enum": ["alarm", "habit", "emotional", "event_prep", "check_in"],
                    "description": (
                        "Semantic type — choose the one that best matches the intent. "
                        "alarm=hard wake-up, habit=recurring behavior, emotional=support/check-in, "
                        "event_prep=before a deadline or event, check_in=general follow-up."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": (
                        "How important is this? critical=must not miss (e.g. medication), "
                        "low=nice-to-have. Drives follow-up intensity at delivery time."
                    ),
                },
                "recurrence": {
                    "type": "string",
                    "description": (
                        "Omit or pass 'once' for a one-time reminder. "
                        "'daily' for every day. 'weekly:Mon,Wed,Fri' for specific days."
                    ),
                },
                "conversation_outline": {
                    "type": "string",
                    "description": (
                        "3-part outline you are generating NOW for delivery time: "
                        "1. Opening framing — what you know going in, what tone to use. "
                        "2. 1-2 key beats — what this conversation needs to accomplish. "
                        "3. Completion signal — what done/snoozed/resisting looks like here. "
                        "Be SPECIFIC to this user and this situation — not generic."
                    ),
                },
                "character_reminder": {
                    "type": "string",
                    "description": (
                        "A specific, contextual note to yourself about staying in character "
                        "for THIS conversation. Not generic. "
                        "Example: 'Bakugou is checking on someone who said they were exhausted — "
                        "push without being harsh, this person needs support not scolding.'"
                    ),
                },
                "emotional_context": {
                    "type": "string",
                    "description": (
                        "The user's emotional state and relevant life context RIGHT NOW "
                        "as you understood it in this conversation. "
                        "This is what you will need to remember at delivery time."
                    ),
                },
                "completion_signal": {
                    "type": "string",
                    "description": (
                        "What each outcome looks like SPECIFICALLY for this reminder: "
                        "Done (what counts as mission accomplished), "
                        "Snoozed (what signals a delay request), "
                        "Resisting (what signals pushback or avoidance)."
                    ),
                },
                "delivery_preference": {
                    "type": "string",
                    "description": (
                        "Stated or inferred delivery preference. "
                        "e.g. 'user asked to be gentle', 'user responded well to directness'. "
                        "Omit if no preference was expressed."
                    ),
                },
            },
            "required": [
                "time_expression",
                "content",
                "type_hint",
                "priority",
                "conversation_outline",
                "character_reminder",
                "emotional_context",
                "completion_signal",
            ],
        },
    },
}


@register_function("schedule_conversation", SCHEDULE_CONVERSATION_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def schedule_conversation(
    conn,
    time_expression: str,
    content: str,
    type_hint: str,
    priority: str,
    conversation_outline: str,
    character_reminder: str,
    emotional_context: str,
    completion_signal: str,
    recurrence: str = None,
    delivery_preference: str = None,
) -> ActionResponse:
    # Resolve user's timezone
    tz_str = get_timezone_for_device(conn.device_id) or "UTC"
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)

    # Parse natural language time expression relative to now
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
        f"Scheduling conversation for device {conn.device_id}: '{content}' "
        f"at {resolved.isoformat()} (from '{time_expression}')"
    )

    uid = get_owner_phone_for_device(conn.device_id)
    if uid:
        try:
            reminder_id = create_scheduled_conversation(
                uid=uid,
                device_id=conn.device_id,
                resolved_dt=resolved,
                label=content,
                context=content,
                tz_str=tz_str,
                content=content,
                type_hint=type_hint,
                priority=priority,
                conversation_outline=conversation_outline,
                character_reminder=character_reminder,
                emotional_context=emotional_context,
                completion_signal=completion_signal,
                delivery_preference=delivery_preference,
            )
            logger.bind(tag=TAG).info(
                f"Scheduled conversation {reminder_id} written to Firestore for user {uid}"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to write scheduled conversation to Firestore: {e}")
            return ActionResponse(
                action=Action.RESPONSE,
                response="Something went wrong saving that reminder. Want to try again?",
            )
    else:
        logger.bind(tag=TAG).warning(
            f"Could not resolve uid for device {conn.device_id}; conversation not scheduled"
        )

    return ActionResponse(
        action=Action.REQLLM,
        result=f"Scheduled: '{content}' on {resolved.strftime('%A, %B %-d at %-I:%M %p')} ({tz_str}).",
        response=None,
    )
