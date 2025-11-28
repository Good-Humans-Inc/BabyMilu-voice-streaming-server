from __future__ import annotations

from datetime import datetime, timezone

from services.alarms.cloud import functions as cloud_functions
from services.alarms import scheduler, tasks
from services.session_context import models as session_models


class _DummyWakeRequest(tasks.WakeRequest):
    def __init__(self, device_id: str):
        alarm = scheduler.models.AlarmDoc(
            alarm_id="alarm-1",
            user_id="user-1",
            uid="user-1",
            label="Alarm",
            schedule=scheduler.models.AlarmSchedule(
                repeat=scheduler.models.AlarmRepeat.WEEKLY,
                time_local="07:00",
                days=["Mon"],
            ),
            status=scheduler.models.AlarmStatus.ON,
            next_occurrence_utc=datetime.now(timezone.utc),
            targets=[],
            updated_at=None,
            raw={"timezone": "UTC"},
        )
        target = scheduler.models.AlarmTarget(device_id=device_id, mode="morning_alarm")
        session = session_models.ModeSession(
            device_id=device_id,
            session_type="alarm",
            triggered_at=datetime.now(timezone.utc),
            ttl_seconds=60,
            session_config={"mode": "morning_alarm"},
        )
        super().__init__(alarm=alarm, target=target, session=session)


def test_scan_due_alarms_triggers_publish(monkeypatch):
    fake_requests = [_DummyWakeRequest("DEV1"), _DummyWakeRequest("DEV2")]
    monkeypatch.setattr(
        cloud_functions.scheduler,
        "prepare_wake_requests",
        lambda now, lookahead: fake_requests,
    )

    published = []

    def fake_publish(broker, device_id, ws_url, version=3):
        published.append((broker, device_id, ws_url, version))
        return True

    monkeypatch.setattr(cloud_functions, "publish_ws_start", fake_publish)
    monkeypatch.setenv("ALARM_WS_URL", "ws://fake")
    monkeypatch.setenv("ALARM_MQTT_URL", "mqtt://fake")

    response = cloud_functions.scan_due_alarms(request={})  # type: ignore[arg-type]

    assert response["ok"] is True
    assert response["count"] == 2
    assert response["triggered"] == 2
    assert [item["deviceId"] for item in response["results"]] == ["DEV1", "DEV2"]
    assert len(published) == 2
    assert published[0][1] == "DEV1"
    assert published[1][1] == "DEV2"

