from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

ALARM_TIMING = {
    "lookahead": timedelta(0),
    "session_ttl": timedelta(minutes=5),
    "one_time_session_ttl": timedelta(seconds=60),
}

# Default mode configuration for server-owned proactive sessions.
# These values are referenced by the runtime when no per-session overrides exist.
PRIORITY_FOLLOWUP_MAX: Dict[str, int] = {
    "critical": 3,  # must-not-miss reminders such as medication
    "high": 2,
    "medium": 1,
    "low": 0,
}

MODE_CONFIG: Dict[str, Dict[str, Any]] = {
    "morning_alarm": {
        "instructions_file": "services/alarms/mode_instructions/morning_alarm.txt",
        "server_initiate_chat": True,
        # Alarm persistence: 1 initial + 2 follow-ups = 3 total voice messages.
        "followup_enabled": True,
        "followup_delay": 10,
        "followup_max": 2,
        "use_separate_conversation": True,
    },
    "scheduled_conversation": {
        # Dynamic instructions are assembled from session_config fields at delivery.
        "server_initiate_chat": True,
        "followup_enabled": True,
        "followup_delay": 10,
        "followup_max": 1,
        "use_separate_conversation": True,
    },
}
