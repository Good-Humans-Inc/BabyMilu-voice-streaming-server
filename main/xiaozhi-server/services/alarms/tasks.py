from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from services.alarms import models
from services.session_context import models as session_models


@dataclass
class WakeRequest:
    alarm: models.AlarmDoc
    target: models.AlarmTarget
    session: session_models.ModeSession

    def to_payload(self, ws_url: str, broker_url: Optional[str] = None) -> Dict[str, Any]:
        return {
            "deviceId": self.target.device_id,
            "wsUrl": ws_url,
            "broker": broker_url,
            "sessionType": self.session.session_type,
            "session": {
                "triggeredAt": self.session.triggered_at.isoformat(),
                "expiresAt": self.session.expires_at.isoformat() if self.session.expires_at else None,
                "config": self.session.session_config,
            },
        }


