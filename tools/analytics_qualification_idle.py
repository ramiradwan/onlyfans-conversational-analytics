"""Wait for the measured idle minimum, including early timer wake-ups."""
from __future__ import annotations

import asyncio
import time


async def async_wait_at_least(seconds: float) -> float:
    started, elapsed = time.monotonic(), 0.0
    resolution = time.get_clock_info("monotonic").resolution
    while elapsed < seconds:
        await asyncio.sleep(max(seconds - elapsed, resolution))
        elapsed = time.monotonic() - started
    return elapsed


def wait_at_least(seconds: float) -> float:
    started, elapsed = time.monotonic(), 0.0
    resolution = time.get_clock_info("monotonic").resolution
    while elapsed < seconds:
        time.sleep(max(seconds - elapsed, resolution))
        elapsed = time.monotonic() - started
    return elapsed
