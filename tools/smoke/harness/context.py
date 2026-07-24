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
    _firestore_by_database: Any = None

    @property
    def firestore(self):
        if self._firestore is None:
            from google.cloud import firestore

            self._firestore = firestore.Client(
                project=self.environment.project,
                database=self.environment.firestore_database,
            )
        return self._firestore

    def firestore_for(self, database_id: str):
        from google.cloud import firestore

        if self._firestore_by_database is None:
            self._firestore_by_database = {}
        if database_id not in self._firestore_by_database:
            self._firestore_by_database[database_id] = firestore.Client(
                project=self.environment.project,
                database=database_id,
            )
        return self._firestore_by_database[database_id]
