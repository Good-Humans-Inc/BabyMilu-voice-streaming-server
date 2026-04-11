from __future__ import annotations

from migrate_voice_alarms_to_reminders import (
    _build_normalized_reminder_doc,
    _looks_like_legacy_reminder_alarm,
)


def test_build_normalized_reminder_doc_adds_delivery_channel_and_date_local():
    source = {
        "label": "drink water",
        "schedule": {
            "repeat": "once",
            "timeLocal": "14:00",
            "days": ["2026-06-01"],
        },
        "targets": [{"deviceId": "aa:bb", "mode": "morning_alarm"}],
    }

    normalized = _build_normalized_reminder_doc(source)

    assert normalized["deliveryChannel"] == ["plushie"]
    assert normalized["schedule"]["repeat"] == "none"
    assert normalized["schedule"]["dateLocal"] == "2026-06-01"
    assert normalized["targets"][0]["mode"] == "reminder"


def test_looks_like_legacy_reminder_alarm_requires_voice_one_time():
    assert _looks_like_legacy_reminder_alarm(
        {
            "source": "voice",
            "schedule": {"repeat": "once", "timeLocal": "14:00", "days": ["2026-06-01"]},
            "targets": [{"deviceId": "aa:bb", "mode": "morning_alarm"}],
        }
    )
    assert not _looks_like_legacy_reminder_alarm(
        {
            "source": "voice",
            "schedule": {"repeat": "weekly", "timeLocal": "07:00", "days": ["Mon"]},
            "targets": [{"deviceId": "aa:bb", "mode": "morning_alarm"}],
        }
    )
    assert not _looks_like_legacy_reminder_alarm(
        {
            "source": "manual",
            "schedule": {"repeat": "once", "timeLocal": "14:00", "days": ["2026-06-01"]},
            "targets": [{"deviceId": "aa:bb", "mode": "morning_alarm"}],
        }
    )
