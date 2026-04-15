from __future__ import annotations

import importlib
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _import_or_skip(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        pytest.skip(f"{module_name} is not available on this branch yet")


def _pr14_models_or_skip():
    models = _import_or_skip("services.alarms.models")
    fields = getattr(models.AlarmDoc, "__dataclass_fields__", {})
    if "content" not in fields or not hasattr(models.AlarmRepeat, "DAILY"):
        pytest.skip("PR #14 scheduled-conversation alarm fields are not merged yet")
    return models


def test_alarm_stack_imports_smoke():
    firestore_client = importlib.import_module("services.alarms.firestore_client")
    scheduler = importlib.import_module("services.alarms.scheduler")
    session_store = importlib.import_module("services.session_context.store")

    assert firestore_client is not None
    assert scheduler is not None
    assert session_store is not None


def test_pr14_schedule_conversation_tool_descriptor_smoke():
    schedule_module = _import_or_skip("plugins_func.functions.schedule_conversation")

    desc = schedule_module.SCHEDULE_CONVERSATION_FUNCTION_DESC["function"]

    assert desc["name"] == "schedule_conversation"
    assert "time_expression" in desc["parameters"]["properties"]
    assert "completion_signal" in desc["parameters"]["properties"]
    assert "type_hint" in desc["parameters"]["required"]


def test_pr14_reminder_management_tool_descriptors_smoke():
    cancel_module = _import_or_skip("plugins_func.functions.cancel_reminder")
    modify_module = _import_or_skip("plugins_func.functions.modify_reminder")

    assert cancel_module.LIST_REMINDERS_FUNCTION_DESC["function"]["name"] == "list_reminders"
    assert cancel_module.CANCEL_REMINDER_FUNCTION_DESC["function"]["name"] == "cancel_reminder"
    assert modify_module.MODIFY_REMINDER_FUNCTION_DESC["function"]["name"] == "modify_reminder"


def test_pr14_models_support_daily_repeat():
    models = _pr14_models_or_skip()

    assert models.AlarmRepeat.DAILY.value == "daily"


def test_pr14_scheduler_carries_scheduled_conversation_session_fields(monkeypatch):
    models = _pr14_models_or_skip()
    scheduler = importlib.import_module("services.alarms.scheduler")
    session_models = importlib.import_module("services.session_context.models")

    class _FakeSessionStore:
        def __init__(self):
            self.sessions = {}

        def get_session(self, device_id: str, now: datetime | None = None):
            return self.sessions.get(device_id)

        def create_session(
            self,
            *,
            device_id: str,
            session_type: str,
            ttl: timedelta,
            triggered_at: datetime,
            session_config: dict,
        ):
            session = session_models.ModeSession(
                device_id=device_id,
                session_type=session_type,
                triggered_at=triggered_at,
                ttl_seconds=int(ttl.total_seconds()),
                session_config=session_config,
            )
            self.sessions[device_id] = session
            return session

        def delete_session(self, device_id: str):
            self.sessions.pop(device_id, None)

    fake_store = _FakeSessionStore()
    monkeypatch.setattr(scheduler, "session_context_store", fake_store)

    def fake_fetch(now, lookahead):
        return [
            models.AlarmDoc(
                alarm_id="alarm-123",
                user_id="user-xyz",
                uid="user-xyz",
                label="Check in",
                schedule=models.AlarmSchedule(
                    repeat=models.AlarmRepeat.DAILY,
                    time_local="08:30",
                    days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                ),
                status=models.AlarmStatus.ON,
                next_occurrence_utc=datetime(2026, 4, 15, 15, 30, tzinfo=timezone.utc),
                targets=[
                    models.AlarmTarget(
                        device_id="DEV123",
                        mode="scheduled_conversation",
                    )
                ],
                raw={"timezone": "America/Los_Angeles"},
                context="check in before the exam",
                content="Exam check-in",
                type_hint="emotional",
                priority="high",
                conversation_outline="Open warm, ask how prep is going, end when user confirms plan.",
                character_reminder="Stay direct but caring.",
                emotional_context="User sounded stressed and underslept.",
                completion_signal="Done: user confirms they are ready. Snoozed: asks for later.",
                delivery_preference="be direct but warm",
            )
        ]

    monkeypatch.setattr(scheduler.firestore_client, "fetch_due_alarms", fake_fetch)

    wake_requests = scheduler.prepare_wake_requests(
        datetime(2026, 4, 15, 15, 29, tzinfo=timezone.utc),
        lookahead=timedelta(minutes=2),
    )

    assert len(wake_requests) == 1
    session_config = wake_requests[0].session.session_config
    assert session_config["mode"] == "scheduled_conversation"
    assert session_config["content"] == "Exam check-in"
    assert session_config["typeHint"] == "emotional"
    assert session_config["priority"] == "high"
    assert session_config["conversationOutline"].startswith("Open warm")
    assert session_config["characterReminder"] == "Stay direct but caring."
    assert session_config["emotionalContext"] == "User sounded stressed and underslept."
    assert session_config["completionSignal"].startswith("Done:")
    assert session_config["deliveryPreference"] == "be direct but warm"
