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


def context_max_days() -> int:
    return int(os.environ.get("JOURNAL_CONTEXT_MAX_DAYS", "7"))


def context_max_sessions() -> int:
    return int(os.environ.get("JOURNAL_CONTEXT_MAX_SESSIONS", "20"))


def context_max_user_turns() -> int:
    return int(os.environ.get("JOURNAL_CONTEXT_MAX_USER_TURNS", "80"))


def context_max_total_turns() -> int:
    return int(os.environ.get("JOURNAL_CONTEXT_MAX_TOTAL_TURNS", "160"))


def context_max_chars() -> int:
    return int(os.environ.get("JOURNAL_CONTEXT_MAX_CHARS", "20000"))
