"""Falsifiers for the cross-process claim that serializes fixed-resource tests."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

import exclusive_resource


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="machine_wide_lock is a Win32 named-mutex claim"
)

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


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0"])
@pytest.mark.parametrize("reuse_address", [False, True])
def test_a_listener_remains_occupied_until_it_closes(
    address: str, reuse_address: bool
) -> None:
    """A same-user wildcard or reusable listener cannot be reserved over."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if reuse_address:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, 0))
        port = listener.getsockname()[1]
        listener.listen()
        assert not exclusive_resource.port_is_free(port)
        assert not exclusive_resource.wait_for_port_release(port, timeout_seconds=0)

    assert exclusive_resource.wait_for_port_release(port, timeout_seconds=0)


@pytest.mark.skipif(not socket.has_dualstack_ipv6(), reason="dual-stack IPv6 is unavailable")
@pytest.mark.parametrize("reservation_supported", [False, True])
def test_a_dual_stack_listener_is_not_reported_free(
    monkeypatch: pytest.MonkeyPatch, reservation_supported: bool
) -> None:
    """An IPv6 wildcard listener can also own the IPv4 provisioning port."""

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(("::", 0))
        port = listener.getsockname()[1]
        listener.listen()
        monkeypatch.setattr(socket, "has_dualstack_ipv6", lambda: reservation_supported)
        assert not exclusive_resource.port_is_free(port)

    assert exclusive_resource.port_is_free(port)


def test_a_failed_reservation_still_checks_whether_the_listener_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reservation error neither skips the listener check nor pins the port busy."""

    def cannot_reserve(_sock: socket.socket, _address: tuple[str, int]) -> None:
        raise OSError("the port cannot be reserved")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        monkeypatch.setattr(socket.socket, "bind", cannot_reserve)
        assert not exclusive_resource.port_is_free(port)

    assert exclusive_resource.port_is_free(port)


@pytest.mark.parametrize(
    "fixture_name", ["non_listening_installer", "healthy_listener_installer"]
)
def test_shared_installer_builds_wait_for_other_resource_holders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    fixture_name: str,
) -> None:
    """Module fixtures must claim shared build outputs before invoking the builder."""

    import test_packaging_smoke

    name = _unique_lock_name()
    monkeypatch.setattr(exclusive_resource, "PROVISIONING_RESOURCE_MUTEX", name)
    build_started_at: list[float] = []

    def build_installer(root: Path, *, serves_health: bool = False) -> Path:
        build_started_at.append(time.monotonic())
        return root / ("healthy.exe" if serves_health else "non-listening.exe")

    monkeypatch.setattr(test_packaging_smoke, "_build_real_installer", build_installer)
    fixture = getattr(test_packaging_smoke, fixture_name)
    holder = _hold_lock(name, _HOLD_SECONDS)
    try:
        started_at = time.monotonic()
        installer = fixture.__wrapped__(tmp_path_factory)
    finally:
        holder.wait(timeout=30)

    assert len(build_started_at) == 1
    waited_seconds = build_started_at[0] - started_at
    assert waited_seconds >= _HOLD_SECONDS * 0.8, (
        f"{fixture_name} started building after {waited_seconds:.2f}s "
        f"while another process still held {name}"
    )
    assert installer.name == (
        "healthy.exe" if fixture_name == "healthy_listener_installer" else "non-listening.exe"
    )
