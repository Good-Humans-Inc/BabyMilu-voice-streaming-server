from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

DEFAULT_SESSION_TTL_SECONDS = 300


@dataclass
class ModeSession:
    """Server-owned session metadata for proactive experiences."""

    device_id: str
    session_type: str
    triggered_at: datetime
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    expires_at: Optional[datetime] = None
    session_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.expires_at is None and self.ttl_seconds:
            self.expires_at = self.triggered_at + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def to_payload(self) -> Dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "sessionType": self.session_type,
            "triggeredAt": self.triggered_at.isoformat(),
            "ttlSeconds": self.ttl_seconds,
            "expiresAt": self.expires_at.isoformat() if self.expires_at else None,
            "sessionConfig": self.session_config,
        }


def ttl_seconds_from_delta(ttl: Optional[timedelta]) -> int:
    if not ttl:
        return DEFAULT_SESSION_TTL_SECONDS
    return int(ttl.total_seconds())

