"""Evaluation time for the checked-in July analytics golden fixtures."""

from datetime import datetime, timezone


def fixture_clock() -> datetime:
    """Keep fixture derivations inside the unchanged 90-day source-time bound."""

    return datetime(2026, 7, 20, tzinfo=timezone.utc)
