from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

_DEVICE_ID: ContextVar[Optional[str]] = ContextVar("device_id", default=None)


def get_device_id() -> Optional[str]:
    return _DEVICE_ID.get()


def set_device_id(device_id: Optional[str]) -> Token[Optional[str]]:
    return _DEVICE_ID.set(device_id)


def clear_device_id() -> Token[Optional[str]]:
    return _DEVICE_ID.set(None)

def reset_device_id(token: Token[Optional[str]]) -> None:
    _DEVICE_ID.reset(token)


@contextmanager
def device_id_context(device_id: Optional[str]) -> Iterator[None]:
    token = set_device_id(device_id)
    try:
        yield
    finally:
        reset_device_id(token)

