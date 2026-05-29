import json

from services.messaging import mqtt


def test_publish_rtc_alarm_sends_expected_payload(monkeypatch):
    captured = {}

    class _FakeResult:
        def wait_for_publish(self, timeout):
            captured["wait_timeout"] = timeout

    class _FakeClient:
        def __init__(self, client_id, clean_session):
            captured["client_id"] = client_id
            captured["clean_session"] = clean_session

        def connect(self, host, port, keepalive):
            captured["connect"] = (host, port, keepalive)

        def loop_start(self):
            captured["loop_start"] = True

        def publish(self, topic, payload, qos):
            captured["publish"] = (topic, json.loads(payload), qos)
            return _FakeResult()

        def loop_stop(self):
            captured["loop_stop"] = True

        def disconnect(self):
            captured["disconnect"] = True

    monkeypatch.setattr(mqtt.mqtt_client, "Client", _FakeClient)
    monkeypatch.setattr(mqtt.time, "time", lambda: 1770000000.123)

    ok = mqtt.publish_rtc_alarm(
        "mqtt://broker.test:1884",
        "AA:BB:CC:DD:EE:FF",
        1770000600,
        offline_wav_url="https://example.test/reminder.wav",
        custom_mode=True,
        reminder_id="rem-1",
        priority=2,
        replay_if_no_mic=False,
    )

    assert ok is True
    assert captured["connect"] == ("broker.test", 1884, 30)
    assert captured["publish"] == (
        "xiaozhi/aa:bb:cc:dd:ee:ff/down",
        {
            "type": "rtc_alarm",
            "epoch": 1770000600,
            "delay_seconds": 600,
            "delay_ms": 600000,
            "software_fallback": True,
            "custom_mode": True,
            "offline_wav_url": "https://example.test/reminder.wav",
            "reminder_id": "rem-1",
            "priority": 2,
            "replay_if_no_mic": False,
        },
        0,
    )
    assert captured["wait_timeout"] == 1.0


def test_publish_rtc_alarm_can_be_rtc_only(monkeypatch):
    captured = {}

    class _FakeResult:
        def wait_for_publish(self, timeout):
            captured["wait_timeout"] = timeout

    class _FakeClient:
        def __init__(self, client_id, clean_session):
            captured["client_id"] = client_id
            captured["clean_session"] = clean_session

        def connect(self, host, port, keepalive):
            captured["connect"] = (host, port, keepalive)

        def loop_start(self):
            captured["loop_start"] = True

        def publish(self, topic, payload, qos):
            captured["publish"] = (topic, json.loads(payload), qos)
            return _FakeResult()

        def loop_stop(self):
            captured["loop_stop"] = True

        def disconnect(self):
            captured["disconnect"] = True

    monkeypatch.setattr(mqtt.mqtt_client, "Client", _FakeClient)

    ok = mqtt.publish_rtc_alarm(
        "mqtt://broker.test:1884",
        "AA:BB:CC:DD:EE:FF",
        1770000600,
        offline_wav_url="https://example.test/reminder.wav",
        custom_mode=True,
        reminder_id="rem-rtc",
        software_fallback=False,
    )

    assert ok is True
    assert captured["publish"] == (
        "xiaozhi/aa:bb:cc:dd:ee:ff/down",
        {
            "type": "rtc_alarm",
            "epoch": 1770000600,
            "custom_mode": True,
            "offline_wav_url": "https://example.test/reminder.wav",
            "reminder_id": "rem-rtc",
            "priority": 0,
            "replay_if_no_mic": True,
            "software_fallback": False,
            "rtc_only": True,
        },
        0,
    )
