from __future__ import annotations

from abc import ABC, abstractmethod

from .context import ScenarioContext
from .models import ScenarioResult


class BaseScenario(ABC):
    name: str
    description: str

    @abstractmethod
    async def run(self, context: ScenarioContext) -> ScenarioResult:
        raise NotImplementedError
