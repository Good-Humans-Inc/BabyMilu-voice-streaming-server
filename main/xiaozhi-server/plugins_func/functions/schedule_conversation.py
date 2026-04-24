import re
import dateparser
from zoneinfo import ZoneInfo
from datetime import datetime

from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device
from services.alarms.firestore_client import create_scheduled_conversation
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# Matches bare times like "8pm", "9:30am", "8 pm", "9:30 AM"
_BARE_TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?\s*[ap]m)\b', re.IGNORECASE)

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
            "These are your intake notes — be specific and personal, not generic. "
            "Do NOT call this to update or change an existing reminder — use modify_reminder for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_expression": {
                    "type": "string",
                    "description": (
                        "Extract ONLY the one-time trigger moment using one of these exact templates: "
                        "'in N minutes', 'in N hours', 'in N days', 'in N weeks', "
                        "'today at H:MMam/pm', 'tomorrow at H:MMam/pm', "
                        "'[Weekday] at H:MMam/pm' (e.g. 'Monday at 8pm'), "
                        "'next [Weekday] at H:MMam/pm', "
                        "'[Month] [D] at H:MMam/pm' (e.g. 'March 30 at 9pm'), "
                        "or a bare time like '8pm' or '9:30am'. "
                        "Do NOT include recurrence or frequency words such as "
                        "'every day', 'daily', 'each morning', 'weekly', 'every night', etc. — "
                        "those belong in the recurrence field. "
                        "Example: user says 'every day at 8pm' → "
                        "time_expression='8pm', recurrence='daily'."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Concise title for this reminder, maximum 10 words. "
                        "Examples: 'Take vitamins', 'Morning run', 'Check in before exam'."
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
                        "Controls how this reminder repeats. "
                        "— Omit entirely if the user wants a one-time reminder. "
                        "  One-time includes: 'remind me once', 'just this time', "
                        "  'on [specific date]', no mention of repeating. "
                        "— Pass 'daily' if the user says 'every day', 'daily', "
                        "  'each morning', 'every night', or any phrase meaning every day. "
                        "— Pass 'weekly:Mon,Wed,Fri' (comma-separated 3-letter "
                        "  abbreviations) if the user names specific weekdays. "
                        "  Use: Mon Tue Wed Thu Fri Sat Sun. "
                        "  Examples: 'every Monday' → 'weekly:Mon', "
                        "  'weekdays' → 'weekly:Mon,Tue,Wed,Thu,Fri', "
                        "  'weekends' → 'weekly:Sat,Sun'. "
                        "  Never pass 'weekly' without specifying the days after the colon. "
                        "— Pass 'monthly:N' (day of month, 1–31) if the user wants a "
                        "  reminder on a specific day each month. "
                        "  Examples: 'every month on the 22nd' → 'monthly:22', "
                        "  'first of every month' → 'monthly:1'. "
                        "  Short months clamp automatically (e.g. monthly:31 fires on the 30th in April)."
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
                        "Look back at your character definition and identify the specific traits — "
                        "abilities, catchphrases, personal codes, or quirks — "
                        "that are relevant to what the user is being reminded about. "
                        "Write a note on how to weave these into the conversation outline "
                        "so the reminder feels authentically like you, not a generic assistant."
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
                        "Resisting (what signals pushback or avoidance), "
                        "Ignored (user never responds — write what your character should say "
                        "in the final follow-up before giving up, e.g. a character-appropriate "
                        "closing that leaves the door open)."
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
                "label",
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
    label: str,
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

    _parse_settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now,
        "TIMEZONE": tz_str,
        "RETURN_AS_TIMEZONE_AWARE": True,
    }

    # Pass 1: parse as-is (handles relative and anchored expressions).
    # Pass 2: prepend "today at" for bare times like "8pm" or "9:30am".
    # Pass 3: regex-extract the time token and retry — catches mixed expressions
    #          like "8pm every day" where the LLM included recurrence words.
    resolved = dateparser.parse(time_expression, languages=["en"], settings=_parse_settings)
    if resolved is None:
        resolved = dateparser.parse(f"today at {time_expression}", languages=["en"], settings=_parse_settings)
    if resolved is None:
        m = _BARE_TIME_RE.search(time_expression)
        if m:
            resolved = dateparser.parse(f"today at {m.group(1)}", languages=["en"], settings=_parse_settings)

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

    # If called during a scheduled_conversation delivery, the user is snoozing.
    # Write the snoozed outcome to the original alarm before creating the new one.
    _maybe_write_snoozed_outcome(conn)

    uid = get_owner_phone_for_device(conn.device_id)
    if uid:
        try:
            reminder_id = create_scheduled_conversation(
                uid=uid,
                device_id=conn.device_id,
                resolved_dt=resolved,
                label=label,
                context=content,
                tz_str=tz_str,
                recurrence=recurrence,
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
        result=(
            f"Scheduled: '{label}' on {resolved.strftime('%A, %B %-d at %-I:%M %p')} ({tz_str}). "
            f"reminder_id={reminder_id if uid else 'unavailable'}"
        ),
        response=None,
    )


def _maybe_write_snoozed_outcome(conn) -> None:
    """If the tool is called during a delivery session, write lastOutcome='snoozed'."""
    try:
        mode_session = getattr(conn, "mode_session", None)
        session_config = getattr(mode_session, "session_config", None) or {}
        if session_config.get("mode") != "scheduled_conversation":
            return
        alarm_id = session_config.get("alarmId")
        uid = session_config.get("userId")
        if not alarm_id or not uid:
            return
        from services.alarms.firestore_client import write_alarm_outcome
        write_alarm_outcome(uid, alarm_id, "snoozed")
        logger.bind(tag=TAG).info(
            f"Wrote outcome='snoozed' for alarm {alarm_id} (device={conn.device_id})"
        )
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Could not write snoozed outcome: {exc}")
