"""Backend test process configuration.

The application defaults to durable SQLite in production.  The shared test
application is deliberately disposable; restart/durability tests construct
explicit temporary SQLite repositories themselves.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any


os.environ.setdefault("CANONICAL_PERSISTENCE_BACKEND", "memory")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("WEBSOCKET_AUTH_MODE", "development_stub")
os.environ.setdefault(
    "OFCA_TEST_DATABASE_MASTER_KEY_HEX",
    "4d7d278b49d90e4d747ac36d4f65661ec089df85a8f6f22851a628c880f7a4e2",
)

# Application modules construct some stores while the test collection graph is
# imported. Keep those stores in one isolated process-lifetime directory so a
# test run can never open or modify database files in the source checkout.
_TEST_DATABASE_DIRECTORY = tempfile.TemporaryDirectory(prefix="ofca-tests-")
_TEST_DATABASE_ROOT = Path(_TEST_DATABASE_DIRECTORY.name)
for _environment_name, _filename in (
    ("AUTH_DATABASE_PATH", "auth.sqlite3"),
    ("CANONICAL_DATABASE_PATH", "canonical.sqlite3"),
    ("PROJECTION_DATABASE_PATH", "projections.sqlite3"),
    ("ANALYTICS_PROJECTION_DATABASE_PATH", "analytics-projections.sqlite3"),
):
    os.environ[_environment_name] = str(_TEST_DATABASE_ROOT / _filename)


# Diagnostic branch only: measure the synchronization added to prevent native
# SQLCipher open/close overlap. This deliberately changes no production code or
# lock semantics; it only wraps the existing per-path transition locks and
# native open/close calls while pytest is running.
_SQLITE_DIAGNOSTICS_LOCK = RLock()
_SQLITE_DIAGNOSTICS: dict[str, float | int] = {
    "transition_acquisitions": 0,
    "transition_wait_seconds": 0.0,
    "transition_wait_max_seconds": 0.0,
    "transition_hold_seconds": 0.0,
    "transition_hold_max_seconds": 0.0,
    "native_open_calls": 0,
    "native_open_seconds": 0.0,
    "native_open_max_seconds": 0.0,
    "native_close_calls": 0,
    "native_close_seconds": 0.0,
    "native_close_max_seconds": 0.0,
}
_SQLITE_TRANSITION_WRAPPERS: dict[int, "_MeasuredTransitionLock"] = {}


def _record_duration(prefix: str, elapsed: float) -> None:
    with _SQLITE_DIAGNOSTICS_LOCK:
        count_key = f"{prefix}_calls"
        total_key = f"{prefix}_seconds"
        max_key = f"{prefix}_max_seconds"
        _SQLITE_DIAGNOSTICS[count_key] = int(_SQLITE_DIAGNOSTICS[count_key]) + 1
        _SQLITE_DIAGNOSTICS[total_key] = float(_SQLITE_DIAGNOSTICS[total_key]) + elapsed
        _SQLITE_DIAGNOSTICS[max_key] = max(
            float(_SQLITE_DIAGNOSTICS[max_key]), elapsed
        )


class _MeasuredTransitionLock:
    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._entered_at: dict[int, list[float]] = {}

    def __enter__(self):
        import threading

        started = time.perf_counter()
        result = self._lock.__enter__()
        acquired = time.perf_counter()
        wait = acquired - started
        thread_id = threading.get_ident()
        with _SQLITE_DIAGNOSTICS_LOCK:
            _SQLITE_DIAGNOSTICS["transition_acquisitions"] = (
                int(_SQLITE_DIAGNOSTICS["transition_acquisitions"]) + 1
            )
            _SQLITE_DIAGNOSTICS["transition_wait_seconds"] = (
                float(_SQLITE_DIAGNOSTICS["transition_wait_seconds"]) + wait
            )
            _SQLITE_DIAGNOSTICS["transition_wait_max_seconds"] = max(
                float(_SQLITE_DIAGNOSTICS["transition_wait_max_seconds"]), wait
            )
            self._entered_at.setdefault(thread_id, []).append(acquired)
        return result

    def __exit__(self, exc_type, exc, traceback):
        import threading

        ended = time.perf_counter()
        thread_id = threading.get_ident()
        with _SQLITE_DIAGNOSTICS_LOCK:
            stack = self._entered_at.get(thread_id, [])
            entered = stack.pop() if stack else ended
            if not stack:
                self._entered_at.pop(thread_id, None)
            held = ended - entered
            _SQLITE_DIAGNOSTICS["transition_hold_seconds"] = (
                float(_SQLITE_DIAGNOSTICS["transition_hold_seconds"]) + held
            )
            _SQLITE_DIAGNOSTICS["transition_hold_max_seconds"] = max(
                float(_SQLITE_DIAGNOSTICS["transition_hold_max_seconds"]), held
            )
        return self._lock.__exit__(exc_type, exc, traceback)


def pytest_configure(config) -> None:
    from app.persistence import database as database_module

    original_connection_locks = database_module._connection_locks
    original_connect = database_module.sqlite3.connect
    original_close_native = database_module._TrackedConnection._close_native

    def measured_connection_locks(path):
        lifecycle, transition = original_connection_locks(path)
        key = id(transition)
        with _SQLITE_DIAGNOSTICS_LOCK:
            wrapper = _SQLITE_TRANSITION_WRAPPERS.get(key)
            if wrapper is None or wrapper._lock is not transition:
                wrapper = _MeasuredTransitionLock(transition)
                _SQLITE_TRANSITION_WRAPPERS[key] = wrapper
        return lifecycle, wrapper

    def measured_connect(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_connect(*args, **kwargs)
        finally:
            _record_duration("native_open", time.perf_counter() - started)

    def measured_close_native(self) -> None:
        started = time.perf_counter()
        try:
            original_close_native(self)
        finally:
            _record_duration("native_close", time.perf_counter() - started)

    database_module._connection_locks = measured_connection_locks
    database_module.sqlite3.connect = measured_connect
    database_module._TrackedConnection._close_native = measured_close_native


def pytest_sessionfinish(session, exitstatus) -> None:
    with _SQLITE_DIAGNOSTICS_LOCK:
        snapshot = dict(_SQLITE_DIAGNOSTICS)
    print(
        "\nSQLITE_TRANSITION_DIAGNOSTICS "
        + " ".join(f"{key}={value}" for key, value in sorted(snapshot.items())),
        flush=True,
    )
