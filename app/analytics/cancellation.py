"""Cooperative cancellation boundary shared by analytics build stages."""

from __future__ import annotations

from typing import Callable, TypeAlias

from app.analytics.errors import ProjectionBuildCancelled


CancellationCheck: TypeAlias = Callable[[], bool]


def check_cancelled(cancellation_check: CancellationCheck | None) -> None:
    """Raise a stable failure at explicit pipeline stage boundaries."""

    if cancellation_check is not None and cancellation_check():
        raise ProjectionBuildCancelled()
