"""
Alarm service package.

This package intentionally keeps its dependencies limited to shared config/utils
modules so it can later be lifted out (e.g., into a Cloud Function repo) with
minimal changes.
"""

from . import models  # noqa: F401
from . import firestore_client  # noqa: F401
from . import scheduler  # noqa: F401
from . import tool_handlers  # noqa: F401

__all__ = [
    "models",
    "firestore_client",
    "scheduler",
    "tool_handlers",
]


