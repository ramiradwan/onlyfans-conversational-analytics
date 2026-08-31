from __future__ import annotations

from threading import Event, Thread

import pytest

from app.persistence import database as database_module


class ProbeLock:
    def __init__(self) -> None:
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.depth -= 1

    @property
    def entered(self) -> bool:
        return self.depth > 0


def test_native_open_and_close_use_same_transition_lock(tmp_path, monkeypatch) -> None:
    path = (tmp_path / "transition.sqlite3").resolve()
    probe = ProbeLock()
    with database_module._CONNECTION_COUNTS_LOCK:
        database_module._CONNECTION_TRANSITION_LOCKS[path] = probe

    original_connect = database_module.sqlite3.connect
    native_open_observed = False

    def checked_connect(*args, **kwargs):
        nonlocal native_open_observed
        assert probe.entered
        native_open_observed = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", checked_connect)
    database = database_module.ProjectionsSQLite(path, encryption_key=b"r" * 32)
    connection = database.connect()
    assert native_open_observed

    original_close_native = database_module._TrackedConnection._close_native
    native_close_observed = False

    def checked_close(self) -> None:
        nonlocal native_close_observed
        assert probe.entered
        assert database_module.LocalSQLite.open_connection_count(path) == 1
        native_close_observed = True
        original_close_native(self)

    monkeypatch.setattr(
        database_module._TrackedConnection,
        "_close_native",
        checked_close,
    )
    connection.close()

    assert native_close_observed
    assert database_module.LocalSQLite.open_connection_count(path) == 0


def test_blocked_native_close_remains_tracked_until_native_teardown_finishes(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "blocked-close.sqlite3"
    database = database_module.ProjectionsSQLite(path, encryption_key=b"r" * 32)
    connection = database.connect()
    close_started = Event()
    release_close = Event()
    errors: list[BaseException] = []
    original_close_native = database_module._TrackedConnection._close_native

    def blocked_close(self) -> None:
        close_started.set()
        release_close.wait()
        original_close_native(self)

    monkeypatch.setattr(
        database_module._TrackedConnection,
        "_close_native",
        blocked_close,
    )

    def close_connection() -> None:
        try:
            connection.close()
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    closer = Thread(target=close_connection)
    closer.start()
    assert close_started.wait(30), "native SQLite close did not begin"

    assert database_module.LocalSQLite.open_connection_count(path) == 1
    with pytest.raises(
        database_module.SQLiteConfigurationError,
        match="requires closed connections",
    ):
        with database_module.LocalSQLite.exclusive_lifecycle(path):
            pass

    release_close.set()
    closer.join(30)
    assert not closer.is_alive(), "native SQLite close did not complete"
    assert errors == []
    assert database_module.LocalSQLite.open_connection_count(path) == 0
