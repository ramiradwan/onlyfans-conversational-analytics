"""WebSocket transport and replaceable lifecycle state."""

from __future__ import annotations

from typing import Any


__all__ = ["DEV_ACCOUNT_ID", "DEV_AUTH_TICKET", "transport_manager"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import manager

        return getattr(manager, name)
    raise AttributeError(name)
