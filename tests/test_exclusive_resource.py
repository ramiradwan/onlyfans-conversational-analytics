"""Falsifiers for the cross-process claim that serializes fixed-resource tests."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

import exclusive_resource


TESTS_DIRECTORY = Path(__file__).resolve().parent
_HOLD_SECONDS = 1.5


def _hold_lock(name: str, seconds: float) -> subprocess.Popen[str]:
    """Start a separate process that owns `name` for `seconds`."""

    program = (
        f"import sys; sys.path.insert(0, {str(TESTS_DIRECTORY)!r})\n"
        "import time\n"
        "import exclusive_resource\n"
        f"with exclusive_resource.machine_wide_lock({name!r}):\n"
        "    print('held', flush=True)\n"
        f"    time.sleep({seconds!r})\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if holder.stdout is None or holder.stdout.readline().strip() != "held":
        holder.kill()
        stdout, stderr = holder.communicate(timeout=10)
        raise AssertionError(f"holder did not take the lock: {stdout!r} {stderr!r}")
    return holder


def _unique_lock_name() -> str:
    return "Local\\ofca-exclusive-resource-test-" + uuid4().hex


def test_a_concurrent_claim_waits_for_the_holder_and_then_runs() -> None:
    """A blocked claim queues behind the holder instead of failing or skipping."""

    name = _unique_lock_name()
    holder = _hold_lock(name, _HOLD_SECONDS)
    try:
        started_at = time.monotonic()
        with exclusive_resource.machine_wide_lock(name, timeout_seconds=60):
            waited_seconds = time.monotonic() - started_at
    finally:
        holder.wait(timeout=30)

    assert waited_seconds >= _HOLD_SECONDS * 0.8, (
        "the second claim entered the guarded section after "
        f"{waited_seconds:.2f}s while another process still held {name}"
    )


def test_an_exhausted_wait_reports_the_holder_rather_than_proceeding() -> None:
    """Waiting out the timeout raises; it never silently enters the section."""

    name = _unique_lock_name()
    holder = _hold_lock(name, _HOLD_SECONDS)
    try:
        with pytest.raises(TimeoutError, match=re.escape(name)):
            with exclusive_resource.machine_wide_lock(name, timeout_seconds=0.2):
                pytest.fail(f"the claim entered the section while {name} was held")
    finally:
        holder.wait(timeout=30)


def test_the_claim_is_released_for_the_next_holder() -> None:
    """Leaving the block hands the claim on rather than keeping it for the run."""

    name = _unique_lock_name()
    with exclusive_resource.machine_wide_lock(name):
        pass

    holder = _hold_lock(name, 0.1)
    holder.wait(timeout=30)
    assert holder.returncode == 0
