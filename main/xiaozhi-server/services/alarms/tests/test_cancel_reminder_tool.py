from __future__ import annotations

from unittest.mock import MagicMock

from plugins_func.functions.cancel_reminder import cancel_reminder, list_reminders
from plugins_func.register import Action
from services.alarms import models


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn(device_id="aa:bb:cc:dd:ee:ff"):
    conn = MagicMock()
    conn.device_id = device_id
    return conn


def _make_alarm(alarm_id="alarm-42", content="gym", time_local="20:00"):
    schedule = models.AlarmSchedule(
        repeat=models.AlarmRepeat.NONE,
        time_local=time_local,
        days=[],
    )
    target = models.AlarmTarget(device_id="aa:bb:cc:dd:ee:ff", mode="scheduled_conversation")
    return models.AlarmDoc(
        alarm_id=alarm_id,
        user_id="user-1",
        uid="user-1",
        label=content,
        content=content,
        context=content,
        schedule=schedule,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=None,
        targets=[target],
        updated_at=None,
        raw={},
        doc_path=f"users/user-1/alarms/{alarm_id}",
        last_processed_utc=None,
    )


# ---------------------------------------------------------------------------
# list_reminders tests
# ---------------------------------------------------------------------------

def test_list_reminders_returns_formatted_list(monkeypatch):
    alarms = [
        _make_alarm("alarm-1", "gym", "20:00"),
        _make_alarm("alarm-2", "take vitamins", "09:00"),
    ]
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: "user-1",
    )
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.fetch_active_alarms_for_user",
        lambda uid: alarms,
    )

    response = list_reminders(_make_conn())

    assert response.action == Action.REQLLM
    assert "alarm-1" in response.result
    assert "alarm-2" in response.result
    assert "gym" in response.result
    assert "take vitamins" in response.result


def test_list_reminders_returns_empty_message_when_none(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: "user-1",
    )
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.fetch_active_alarms_for_user",
        lambda uid: [],
    )

    response = list_reminders(_make_conn())

    assert response.action == Action.REQLLM
    assert "No active reminders" in response.result


def test_list_reminders_fetch_error_returns_response(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: "user-1",
    )
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.fetch_active_alarms_for_user",
        lambda uid: (_ for _ in ()).throw(RuntimeError("Firestore unavailable")),
    )

    response = list_reminders(_make_conn())

    assert response.action == Action.RESPONSE
    assert response.response is not None


def test_list_reminders_missing_uid_returns_response(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: None,
    )

    response = list_reminders(_make_conn())

    assert response.action == Action.RESPONSE
    assert response.response is not None


# ---------------------------------------------------------------------------
# cancel_reminder tests
# ---------------------------------------------------------------------------

def test_cancel_reminder_calls_cancel_scheduled_conversation(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: "user-1",
    )
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.cancel_scheduled_conversation",
        lambda uid, alarm_id: calls.append((uid, alarm_id)),
    )

    response = cancel_reminder(_make_conn(), alarm_id="alarm-42")

    assert response.action == Action.REQLLM
    assert "alarm-42" in response.result
    assert calls == [("user-1", "alarm-42")]


def test_cancel_reminder_firestore_error_returns_response(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: "user-1",
    )
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.cancel_scheduled_conversation",
        lambda uid, alarm_id: (_ for _ in ()).throw(RuntimeError("Firestore unavailable")),
    )

    response = cancel_reminder(_make_conn(), alarm_id="alarm-42")

    assert response.action == Action.RESPONSE
    assert response.response is not None


def test_cancel_reminder_missing_uid_returns_response(monkeypatch):
    monkeypatch.setattr(
        "plugins_func.functions.cancel_reminder.get_owner_phone_for_device",
        lambda device_id: None,
    )

    response = cancel_reminder(_make_conn(), alarm_id="alarm-42")

    assert response.action == Action.RESPONSE
    assert response.response is not None
