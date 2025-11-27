from __future__ import annotations

from datetime import datetime, timezone

from services.alarms import models, tasks
from services.session_context import models as session_models


def _make_alarm_and_target():
    schedule = models.AlarmSchedule(
        repeat=models.AlarmRepeat.WEEKLY,
        time_local="08:30",
        days=["Tue"],
    )
    alarm = models.AlarmDoc(
        alarm_id="alarm-42",
        user_id="user-99",
        uid="user-99",
        label="Test alarm",
        schedule=schedule,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=datetime.now(timezone.utc),
        targets=[],
        updated_at=None,
        raw={},
    )
    target = models.AlarmTarget(device_id="DEV999", mode="morning_alarm")
    return alarm, target


def test_wake_request_payload_contains_session_blob():
    alarm, target = _make_alarm_and_target()
    session = session_models.ModeSession(
        device_id="DEV999",
        session_type="alarm",
        triggered_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ttl_seconds=300,
        session_config={
            "mode": "morning_alarm",
            "alarmId": "alarm-42",
            "userId": "user-99",
            "label": "Test alarm",
        },
    )

    request = tasks.WakeRequest(alarm=alarm, target=target, session=session)
    payload = request.to_payload(ws_url="ws://server/xiaozhi/v1/", broker_url="mqtt://host")

    assert payload["deviceId"] == "DEV999"
    assert payload["wsUrl"] == "ws://server/xiaozhi/v1/"
    assert payload["broker"] == "mqtt://host"
    assert payload["sessionType"] == "alarm"
    assert payload["session"]["config"]["mode"] == "morning_alarm"
    assert payload["session"]["config"]["alarmId"] == "alarm-42"
    assert payload["session"]["config"]["userId"] == "user-99"
    assert payload["session"]["config"]["label"] == "Test alarm"

