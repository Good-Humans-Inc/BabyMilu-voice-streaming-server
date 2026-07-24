from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SMOKE_ROOT = Path(__file__).resolve().parents[1]
if str(SMOKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SMOKE_ROOT))

from harness.scenarios.timezone_recalculation import (  # noqa: E402
    _as_utc,
    _next_daily_call_occurrence_utc,
    _next_weekly_occurrence_utc,
    _recalculation_state,
    _validate_local_isolated_environment,
)


class FakeAdapter:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents

    def get_document(self, path: str) -> dict | None:
        return self.documents.get(path)


def test_next_weekly_occurrence_rebases_wall_clock_to_new_timezone() -> None:
    schedule = {"repeat": "weekly", "days": ["Tue"], "timeLocal": "09:30"}
    after = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    actual = _next_weekly_occurrence_utc(
        schedule,
        "America/New_York",
        after_utc=after,
    )

    assert actual == datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)


def test_next_weekly_occurrence_uses_post_dst_offset() -> None:
    schedule = {"repeat": "weekly", "days": ["Sun"], "timeLocal": "09:00"}
    after = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)

    actual = _next_weekly_occurrence_utc(
        schedule,
        "America/New_York",
        after_utc=after,
    )

    assert actual == datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc)


def test_next_daily_call_occurrence_rebases_wall_clock() -> None:
    times = {
        day: "09:17"
        for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    after = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)

    actual = _next_daily_call_occurrence_utc(
        times,
        "America/New_York",
        after_utc=after,
    )

    assert actual == datetime(2026, 7, 23, 13, 17, tzinfo=timezone.utc)


def test_recalculation_state_accepts_equivalent_firestore_datetime_and_iso_values() -> None:
    expected = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    adapter = FakeAdapter(
        {
            "users/test/reminders/one": {
                "status": "on",
                "nextOccurrenceUTC": "2026-07-21T13:30:00Z",
            },
            "users/test/alarms/two": {
                "status": "on",
                "nextOccurrenceUTC": expected,
            },
        }
    )

    state = _recalculation_state(
        adapter,
        {
            "users/test/reminders/one": expected,
            "users/test/alarms/two": expected,
        },
    )

    assert state is not None
    assert set(state) == {
        "users/test/reminders/one",
        "users/test/alarms/two",
    }
    assert _as_utc(
        state["users/test/reminders/one"]["nextOccurrenceUTC"]
    ) == expected


def test_recalculation_state_waits_when_one_cursor_is_still_stale() -> None:
    expected = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    adapter = FakeAdapter(
        {
            "users/test/reminders/one": {
                "status": "on",
                "nextOccurrenceUTC": "2026-07-21T13:30:00Z",
            },
            "users/test/alarms/two": {
                "status": "on",
                "nextOccurrenceUTC": "2026-07-21T16:30:00Z",
            },
        }
    )

    assert (
        _recalculation_state(
            adapter,
            {
                "users/test/reminders/one": expected,
                "users/test/alarms/two": expected,
            },
        )
        is None
    )


def test_recalculation_state_rejects_changed_wall_clock_schedule() -> None:
    expected = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    expected_schedule = {
        "repeat": "weekly",
        "days": ["Tue"],
        "timeLocal": "09:30",
    }
    adapter = FakeAdapter(
        {
            "users/test/reminders/one": {
                "status": "on",
                "nextOccurrenceUTC": expected,
                "schedule": {**expected_schedule, "timeLocal": "10:30"},
            }
        }
    )

    assert (
        _recalculation_state(
            adapter,
            {"users/test/reminders/one": expected},
            {"users/test/reminders/one": expected_schedule},
        )
        is None
    )


def test_recalculation_state_requires_rebased_trigger_cursor() -> None:
    expected = datetime(2026, 7, 21, 13, 30, tzinfo=timezone.utc)
    expected_trigger = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    adapter = FakeAdapter(
        {
            "users/test/reminders/one": {
                "status": "on",
                "nextOccurrenceUTC": expected,
                "nextTriggerUTC": "2026-07-21T16:00:00Z",
            }
        }
    )

    assert (
        _recalculation_state(
            adapter,
            {"users/test/reminders/one": expected},
            expected_triggers={
                "users/test/reminders/one": expected_trigger,
            },
        )
        is None
    )

    adapter.documents["users/test/reminders/one"]["nextTriggerUTC"] = (
        expected_trigger
    )
    assert _recalculation_state(
        adapter,
        {"users/test/reminders/one": expected},
        expected_triggers={
            "users/test/reminders/one": expected_trigger,
        },
    )


@pytest.mark.parametrize(
    ("environment_type", "data_mode", "project", "emulator_host", "message"),
    [
        (
            "cloud",
            "isolated",
            "demo-babymilu",
            "127.0.0.1:8080",
            "local-only",
        ),
        (
            "local-compose",
            "live-shape",
            "demo-babymilu",
            "127.0.0.1:8080",
            "local-only",
        ),
        (
            "local-compose",
            "isolated",
            "demo-babymilu",
            "",
            "FIRESTORE_EMULATOR_HOST",
        ),
        (
            "local-compose",
            "isolated",
            "real-project",
            "127.0.0.1:8080",
            "demo-*",
        ),
    ],
)
def test_scenario_refuses_unsafe_environment(
    monkeypatch,
    environment_type: str,
    data_mode: str,
    project: str,
    emulator_host: str,
    message: str,
) -> None:
    if emulator_host:
        monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", emulator_host)
    else:
        monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    context = SimpleNamespace(
        environment=SimpleNamespace(
            environment_type=environment_type,
            data_mode=data_mode,
            project=project,
        )
    )

    with pytest.raises(RuntimeError, match=message):
        _validate_local_isolated_environment(context)


def test_scenario_accepts_local_demo_emulator(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    context = SimpleNamespace(
        environment=SimpleNamespace(
            environment_type="local-compose",
            data_mode="isolated",
            project="demo-babymilu",
        )
    )

    _validate_local_isolated_environment(context)
