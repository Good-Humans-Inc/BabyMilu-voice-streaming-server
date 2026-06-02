from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import EnvironmentConfig


@dataclass
class ScenarioContext:
    environment: EnvironmentConfig
    args: Any
    artifact_writer: Any
    artifact_dir: Path
    _firestore: Any = None

    @property
    def firestore(self):
        if self._firestore is None:
            from google.cloud import firestore

            self._firestore = firestore.Client(project=self.environment.project)
        return self._firestore
