from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

ALARM_TIMING = {
    "lookahead": timedelta(minutes=2),
    "session_ttl": timedelta(minutes=5),
    "one_time_session_ttl": timedelta(seconds=60),
}

# Default mode configuration for server-owned proactive sessions.
# These values are referenced by the runtime when no per-session overrides exist.
MODE_CONFIG: Dict[str, Dict[str, Any]] = {
    "morning_alarm": {
        "instructions_file": "services/alarms/mode_instructions/morning_alarm.txt",
        "server_initiate_chat": True,
        # Alarm persistence: keep trying to wake user up.
        # 1 initial + 2 follow-ups = 3 total voice messages.
        "followup_enabled": True,
        "followup_delay": 10,  # seconds between follow-ups
        "followup_max": 2,
        "use_separate_conversation": True,
    },
    "reminder": {
        # Inline instructions — no separate .txt file needed.
        # The context block ("The user asked to be reminded about: X") is appended
        # automatically by _apply_mode_session_settings() from session_config["context"].
        "instructions": (
            "You have one job: deliver the reminder stated in the context below, "
            "clearly and warmly, in a single short sentence. "
            "After delivering it, wish the user well and end naturally. "
            "Do not ask follow-up questions or continue the conversation."
        ),
        "server_initiate_chat": True,
        # Only 1 voice message for reminders — no follow-ups.
        "followup_enabled": False,
        "use_separate_conversation": True,
    },
}

