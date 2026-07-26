from __future__ import annotations

from dataclasses import dataclass

from .scenario import BaseScenario
from .scenarios.interaction import MagicCameraPhotoScenario
from .scenarios.daycare import DaycareFoodGiftScenario
from .scenarios.scheduled import ScheduledAlarmScenario, ScheduledReminderScenario
from .scenarios.cloud_timezone_recalculation import (
    ScheduledCloudTimezoneWorkerRecalculationScenario,
)
from .scenarios.timezone_recalculation import (
    ScheduledDailyCallTimezoneRecalculationScenario,
    ScheduledDefaultDailyCallTimezoneRecalculationScenario,
    ScheduledDefaultTimezoneRecalculationScenario,
    ScheduledTimezoneRecalculationScenario,
)


@dataclass
class ScenarioDescriptor:
    name: str
    description: str
    cls: type[BaseScenario]


SCENARIOS = {
    "scheduled.reminder": ScenarioDescriptor(
        name="scheduled.reminder",
        description=(
            "Create an app-shaped reminder, trigger the scheduler, and verify "
            "Firestore plus plushie/app side effects."
        ),
        cls=ScheduledReminderScenario,
    ),
    "scheduled.alarm": ScenarioDescriptor(
        name="scheduled.alarm",
        description=(
            "Create an app-shaped alarm, trigger the scheduler, and verify wake "
            "session plus recurring advancement."
        ),
        cls=ScheduledAlarmScenario,
    ),
    "scheduled.timezone_recalculation": ScenarioDescriptor(
        name="scheduled.timezone_recalculation",
        description=(
            "On a local Firestore emulator, change the user timezone and verify "
            "recurring reminder/alarm UTC cursors are rebased."
        ),
        cls=ScheduledTimezoneRecalculationScenario,
    ),
    "scheduled.daily_call_timezone_recalculation": ScenarioDescriptor(
        name="scheduled.daily_call_timezone_recalculation",
        description=(
            "On a local Firestore emulator, change a development phone-keyed "
            "user timezone and verify the Daily Call UTC cursor is rebased "
            "without dispatch."
        ),
        cls=ScheduledDailyCallTimezoneRecalculationScenario,
    ),
    "scheduled.default_timezone_recalculation": ScenarioDescriptor(
        name="scheduled.default_timezone_recalculation",
        description=(
            "On a local Firestore emulator, change a (default) user timezone "
            "and verify recurring reminder/alarm UTC cursors are rebased."
        ),
        cls=ScheduledDefaultTimezoneRecalculationScenario,
    ),
    "scheduled.default_daily_call_timezone_recalculation": ScenarioDescriptor(
        name="scheduled.default_daily_call_timezone_recalculation",
        description=(
            "On a local Firestore emulator, change a (default) phone-keyed user "
            "timezone and verify the Daily Call UTC cursor is rebased without "
            "dispatch."
        ),
        cls=ScheduledDefaultDailyCallTimezoneRecalculationScenario,
    ),
    "scheduled.cloud_timezone_worker_recalculation": ScenarioDescriptor(
        name="scheduled.cloud_timezone_worker_recalculation",
        description=(
            "Against the deployed cloud workers, verify development schedule "
            "recalculation, the guarded default bridge, direct default Daily "
            "Call recalculation, private IAM, and exact fixture cleanup."
        ),
        cls=ScheduledCloudTimezoneWorkerRecalculationScenario,
    ),
    "interaction.magic_camera_photo": ScenarioDescriptor(
        name="interaction.magic_camera_photo",
        description=(
            "Run a Magic Camera websocket prompt, verify a recent photo exists, "
            "and assert the assistant uses the inspection path instead of the "
            "fallback 'can't see it' response."
        ),
        cls=MagicCameraPhotoScenario,
    ),
    "interaction.daycare_food_gift": ScenarioDescriptor(
        name="interaction.daycare_food_gift",
        description=(
            "Create isolated authenticated Food and Gift actions, verify "
            "private image generation and send completion, then clean up."
        ),
        cls=DaycareFoodGiftScenario,
    ),
}


def list_scenarios() -> list[ScenarioDescriptor]:
    return [SCENARIOS[key] for key in sorted(SCENARIOS)]


def make_scenario(name: str) -> BaseScenario:
    try:
        return SCENARIOS[name].cls()
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        raise SystemExit(
            f"Unknown scenario {name!r}. Known scenarios: {known}"
        ) from exc
