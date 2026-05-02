from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from .scenario import BaseScenario


@dataclass
class ScenarioDescriptor:
    name: str
    description: str
    module: str
    cls_name: str


SCENARIOS = {
    "scheduled.reminder": ScenarioDescriptor(
        name="scheduled.reminder",
        description="Create an app-shaped reminder, trigger the scheduler, and verify Firestore plus plushie/app side effects.",
        module="harness.scenarios.scheduled",
        cls_name="ScheduledReminderScenario",
    ),
    "scheduled.alarm": ScenarioDescriptor(
        name="scheduled.alarm",
        description="Create an app-shaped alarm, trigger the scheduler, and verify wake session plus recurring advancement.",
        module="harness.scenarios.scheduled",
        cls_name="ScheduledAlarmScenario",
    ),
    "interaction.next_starter": ScenarioDescriptor(
        name="interaction.next_starter",
        description="Run the validated next_starter latency-hack e2e path through the shared smoke framework.",
        module="harness.scenarios.interaction",
        cls_name="NextStarterScenario",
    ),
}


def list_scenarios() -> list[ScenarioDescriptor]:
    return [SCENARIOS[key] for key in sorted(SCENARIOS)]


def make_scenario(name: str) -> BaseScenario:
    try:
        descriptor = SCENARIOS[name]
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        raise SystemExit(f"Unknown scenario {name!r}. Known scenarios: {known}") from exc
    module = import_module(descriptor.module)
    cls = getattr(module, descriptor.cls_name)
    return cls()
