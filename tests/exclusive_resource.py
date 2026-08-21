"""Cross-process exclusion for tests that claim machine-wide fixed resources.

Port 17871 is architectural: ``extension/manifest.json`` pins it in the
extension connect-src policy, so the product cannot move to a per-run port.
Tests that exercise the real provisioning listener therefore share one port with
every other suite run on the machine, as do the packaging-smoke falsifiers that
plant a repository marker under the system-drive root.  A named mutex makes
contending runs queue for those resources instead of reading one another's
listeners and markers as failures.
"""

from __future__ import annotations

import ctypes
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager


PROVISIONING_PORT = 17871

# One name covers the fixed port and the system-drive scan root together.  A
# single claim keeps acquisition order trivial and cannot deadlock.
PROVISIONING_RESOURCE_MUTEX = r"Global\ofca-packaging-provisioning-resources"

_ACQUIRE_TIMEOUT_SECONDS = 1800.0
_PORT_DRAIN_TIMEOUT_SECONDS = 30.0

_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


def _kernel32() -> "ctypes.WinDLL":
    """Bind the mutex entry points lazily so import stays portable."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


@contextmanager
def machine_wide_lock(
    name: str, *, timeout_seconds: float = _ACQUIRE_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Hold a system-wide named mutex, waiting for a concurrent holder.

    Windows releases an abandoned mutex when its owner dies, so a crashed run
    hands the claim to the next waiter rather than wedging it.
    """

    kernel32 = _kernel32()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"could not create the {name} mutex")
    try:
        state = kernel32.WaitForSingleObject(handle, 0)
        if state == _WAIT_TIMEOUT:
            print(
                f"[exclusive-resource] waiting for {name}; another run holds it",
                file=sys.stderr,
                flush=True,
            )
            state = kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
        if state == _WAIT_TIMEOUT:
            raise TimeoutError(
                f"{name} was still held by another process after "
                f"{timeout_seconds:.0f}s"
            )
        if state not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            raise OSError(ctypes.get_last_error(), f"waiting on {name} failed ({state})")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


def port_is_free(port: int = PROVISIONING_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_for_port_release(
    port: int = PROVISIONING_PORT,
    *,
    timeout_seconds: float = _PORT_DRAIN_TIMEOUT_SECONDS,
) -> bool:
    """Poll until the port stops accepting connections, tolerating teardown lag."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        if port_is_free(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


@contextmanager
def exclusive_provisioning_resources() -> Iterator[None]:
    """Claim the fixed port and system-drive scan root for one test."""

    with machine_wide_lock(PROVISIONING_RESOURCE_MUTEX):
        if not wait_for_port_release():
            raise RuntimeError(
                f"port {PROVISIONING_PORT} is held by a process outside this suite; "
                f"it did not drain within {_PORT_DRAIN_TIMEOUT_SECONDS:.0f}s"
            )
        yield
