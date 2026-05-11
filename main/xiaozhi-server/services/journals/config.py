from __future__ import annotations

import os


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def journals_enabled() -> bool:
    return env_bool("JOURNALS_ENABLED", False)


def processing_enabled() -> bool:
    return journals_enabled() and env_bool("JOURNAL_PROCESSING_ENABLED", False)


def generation_enabled() -> bool:
    return journals_enabled() and env_bool("JOURNAL_GENERATION_ENABLED", False)


def publish_enabled() -> bool:
    return journals_enabled() and env_bool("JOURNAL_PUBLISH_ENABLED", False)


def push_enabled() -> bool:
    return journals_enabled() and env_bool("JOURNAL_PUSH_ENABLED", False)


def openai_model() -> str:
    return os.environ.get("JOURNAL_OPENAI_MODEL", "gpt-4o-mini")


def max_ready_sessions() -> int:
    return int(os.environ.get("JOURNAL_READY_SESSION_LIMIT", "50"))


def max_generation_queues() -> int:
    return int(os.environ.get("JOURNAL_GENERATION_LIMIT", "50"))


def max_publish_entries() -> int:
    return int(os.environ.get("JOURNAL_PUBLISH_LIMIT", "50"))
