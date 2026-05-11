from __future__ import annotations

import os
from typing import Optional

from google.cloud import firestore


def _resolve_credentials_path() -> str:
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        if os.path.isfile(path):
            return path
        if os.path.isdir(path):
            sa_file = os.path.join(path, "sa.json")
            if os.path.isfile(sa_file):
                return sa_file
            try:
                for name in os.listdir(path):
                    candidate = os.path.join(path, name)
                    if name.endswith(".json") and os.path.isfile(candidate):
                        return candidate
            except Exception:
                pass

    docker_secret_dir = "/opt/secrets/gcp"
    if os.path.isdir(docker_secret_dir):
        try:
            for name in os.listdir(docker_secret_dir):
                candidate = os.path.join(docker_secret_dir, name)
                if name.endswith(".json") and os.path.isfile(candidate):
                    return candidate
        except Exception:
            pass

    local_default = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", ".gcp", "sa.json")
    )
    if os.path.isfile(local_default):
        return local_default

    return ""


def _normalize_database_id(value: Optional[str]) -> Optional[str]:
    database_id = (value or "").strip()
    if not database_id or database_id == "(default)":
        return None
    return database_id


def build_firestore_client(*, project: Optional[str] = None) -> firestore.Client:
    creds_path = _resolve_credentials_path()
    if creds_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    else:
        env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if env_creds and os.path.isdir(env_creds):
            del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]

    project_id = (project or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    database_id = _normalize_database_id(os.environ.get("FIRESTORE_DATABASE_ID"))

    kwargs = {}
    if project_id:
        kwargs["project"] = project_id
    if database_id:
        kwargs["database"] = database_id
    return firestore.Client(**kwargs)


def firestore_database_label() -> str:
    return _normalize_database_id(os.environ.get("FIRESTORE_DATABASE_ID")) or "(default)"
