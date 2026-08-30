from __future__ import annotations

from threading import Event, Thread

from app.persistence import database as database_module


def test_tracked_connection_close_serializes_native_close_with_next_open(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "lifecycle.sqlite3"
    database = database_module.ProjectionsSQLite(
        path,
        encryption_key=b"r" * 32,
    )
    connection = database.connect()
    assert database_module.LocalSQLite.open_connection_count(path) == 1

    close_started = Event()
    release_close = Event()
    open_attempted = Event()
    open_completed = Event()
    errors: list[BaseException] = []
    opened: list[object] = []
    original_close_native = database_module._TrackedConnection._close_native

    def blocked_close_native(self) -> None:
        close_started.set()
        if not release_close.wait(5):
            raise AssertionError("test did not release blocked native SQLite close")
        original_close_native(self)

    monkeypatch.setattr(
        database_module._TrackedConnection,
        "_close_native",
        blocked_close_native,
    )

    def close_connection() -> None:
        try:
            connection.close()
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    def open_connection() -> None:
        try:
            open_attempted.set()
            opened.append(database.connect())
            open_completed.set()
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)
            open_completed.set()

    closer = Thread(target=close_connection)
    closer.start()
    assert close_started.wait(5)
    assert database_module.LocalSQLite.open_connection_count(path) == 1

    opener = Thread(target=open_connection)
    opener.start()
    assert open_attempted.wait(5)
    assert not open_completed.wait(0.1)
    assert database_module.LocalSQLite.open_connection_count(path) == 1

    release_close.set()
    closer.join(5)
    opener.join(5)
    assert not closer.is_alive()
    assert not opener.is_alive()
    assert errors == []
    assert len(opened) == 1
    assert database_module.LocalSQLite.open_connection_count(path) == 1

    opened[0].close()
    assert database_module.LocalSQLite.open_connection_count(path) == 0
