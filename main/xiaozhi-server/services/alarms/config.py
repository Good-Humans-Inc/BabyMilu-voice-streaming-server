from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

ALARM_TIMING = {
    "lookahead": timedelta(minutes=2),
    "session_ttl": timedelta(minutes=5),
}

# Default mode configuration for server-owned proactive sessions.
# These values are referenced by the runtime when no per-session overrides exist.
MODE_CONFIG: Dict[str, Dict[str, Any]] = {
    "morning_alarm": {
        "instructions_file": "services/alarms/mode_instructions/morning_alarm.txt",
        "server_initiate_chat": True,
        # Alarm persistence: keep trying to wake user up.
        "followup_enabled": True,
        "followup_delay": 10,  # seconds between follow-ups
        "followup_max": 5,  # max number of follow-ups
        "use_separate_conversation": True,
    },
}

