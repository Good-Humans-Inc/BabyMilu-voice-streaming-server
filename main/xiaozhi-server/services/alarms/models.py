from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence

from services.logging import setup_logging

logger = setup_logging()


class AlarmRepeat(str, Enum):
    WEEKLY = "weekly"


class AlarmStatus(str, Enum):
    ON = "on"
    OFF = "off"


DAY_NAMES: Sequence[str] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class AlarmSchedule:
    repeat: AlarmRepeat
    time_local: str
    days: List[str] = field(default_factory=list)

    def __post_init__(self):
        normalized_days: List[str] = []
        for day in self.days:
            if day in DAY_NAMES:
                normalized_days.append(day)
            else:
                logger.warning(f"Invalid alarm day '{day}' encountered; dropping")
        self.days = normalized_days


@dataclass
class AlarmTarget:
    device_id: str
    mode: str = "morning_alarm"


@dataclass
class AlarmDoc:
    alarm_id: str
    user_id: str
    uid: Optional[str]
    label: Optional[str]
    schedule: AlarmSchedule
    status: AlarmStatus
    next_occurrence_utc: datetime
    targets: List[AlarmTarget] = field(default_factory=list)
    updated_at: Optional[datetime] = None
    raw: Dict = field(default_factory=dict)


@dataclass
class AlarmLog:
    alarm_id: str
    user_id: str
    actor_type: str
    source: str
    request_id: str
    timestamp: datetime
    changes: Dict[str, Dict[str, Optional[str]]]


