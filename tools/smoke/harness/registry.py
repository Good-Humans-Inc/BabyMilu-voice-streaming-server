from __future__ import annotations

from dataclasses import dataclass

from .scenario import BaseScenario
from .scenarios.scheduled import ScheduledAlarmScenario, ScheduledReminderScenario


@dataclass
class ScenarioDescriptor:
    name: str
    description: str
    cls: type[BaseScenario]


SCENARIOS = {
    "scheduled.reminder": ScenarioDescriptor(
        name="scheduled.reminder",
        description="Create an app-shaped reminder, trigger the scheduler, and verify Firestore plus plushie/app side effects.",
        cls=ScheduledReminderScenario,
    ),
    "scheduled.alarm": ScenarioDescriptor(
        name="scheduled.alarm",
        description="Create an app-shaped alarm, trigger the scheduler, and verify wake session plus recurring advancement.",
        cls=ScheduledAlarmScenario,
    ),
}


def list_scenarios() -> list[ScenarioDescriptor]:
    return [SCENARIOS[key] for key in sorted(SCENARIOS)]


def make_scenario(name: str) -> BaseScenario:
    try:
        return SCENARIOS[name].cls()
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        raise SystemExit(f"Unknown scenario {name!r}. Known scenarios: {known}") from exc
