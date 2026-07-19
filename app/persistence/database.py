"""Connection management for independently scoped local SQLite files."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    reject_path_aliases,
)


class SQLiteConfigurationError(RuntimeError):
    """Raised when SQLite cannot provide the required durability profile."""


_CONNECTION_COUNTS: dict[Path, int] = {}
_CONNECTION_COUNTS_LOCK = RLock()
_LIFECYCLE_LOCKS: dict[Path, RLock] = {}


class _TrackedConnection(sqlite3.Connection):
    _tracked_path: Path | None = None
    _tracking_closed: bool = False

    def close(self) -> None:
        if not self._tracking_closed and self._tracked_path is not None:
            with _CONNECTION_COUNTS_LOCK:
                remaining = _CONNECTION_COUNTS.get(self._tracked_path, 1) - 1
                if remaining > 0:
                    _CONNECTION_COUNTS[self._tracked_path] = remaining
                else:
                    _CONNECTION_COUNTS.pop(self._tracked_path, None)
            self._tracking_closed = True
        super().close()


class LocalSQLite:
    """Open independently scoped connections with the accepted SQLite profile."""

    store_name = "local"

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        try:
            self.path = reject_path_aliases(path)
        except PrivateFileSecurityError as error:
            raise SQLiteConfigurationError("SQLite path is not safe") from error
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        with _CONNECTION_COUNTS_LOCK:
            lifecycle = _LIFECYCLE_LOCKS.setdefault(self.path, RLock())
        with lifecycle:
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
                factory=_TrackedConnection,
            )
            connection._tracked_path = self.path
            with _CONNECTION_COUNTS_LOCK:
                _CONNECTION_COUNTS[self.path] = (
                    _CONNECTION_COUNTS.get(self.path, 0) + 1
                )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise SQLiteConfigurationError(
                    f"{self.store_name} SQLite did not enter WAL mode: {journal_mode!r}"
                )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise SQLiteConfigurationError(
                    f"{self.store_name} SQLite is not synchronous=FULL"
                )
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise SQLiteConfigurationError(
                    f"{self.store_name} SQLite foreign keys are disabled"
                )
            self._restrict_permissions()
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            # BEGIN IMMEDIATE forces WAL/SHM creation before caller-controlled
            # values are written, so their DACL/mode can be verified first.
            self._restrict_permissions()
            yield connection
            connection.commit()
            self._restrict_permissions()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def validate_integrity(self) -> None:
        with self.read() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise SQLiteConfigurationError(
                    f"{self.store_name} SQLite integrity check failed: {result}"
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise SQLiteConfigurationError(
                    f"{self.store_name} SQLite foreign-key check failed: {violations!r}"
                )

    def _restrict_permissions(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                try:
                    apply_private_file_security(candidate)
                except PrivateFileSecurityError as error:
                    raise SQLiteConfigurationError(
                        f"{self.store_name} SQLite private-file security failed"
                    ) from error

    @staticmethod
    def open_connection_count(path: str | Path) -> int:
        try:
            target = reject_path_aliases(path)
        except PrivateFileSecurityError:
            return 0
        with _CONNECTION_COUNTS_LOCK:
            return _CONNECTION_COUNTS.get(target, 0)

    @staticmethod
    @contextmanager
    def exclusive_lifecycle(path: str | Path) -> Iterator[Path]:
        try:
            target = reject_path_aliases(path)
        except PrivateFileSecurityError as error:
            raise SQLiteConfigurationError("SQLite path is not safe") from error
        with _CONNECTION_COUNTS_LOCK:
            lock = _LIFECYCLE_LOCKS.setdefault(target, RLock())
        with lock:
            with _CONNECTION_COUNTS_LOCK:
                if _CONNECTION_COUNTS.get(target, 0):
                    raise SQLiteConfigurationError(
                        "SQLite lifecycle operation requires closed connections"
                    )
            yield target


class CanonicalSQLite(LocalSQLite):
    """Authoritative canonical database using full durability settings."""

    store_name = "canonical"


class ProjectionsSQLite(LocalSQLite):
    """Disposable projections database using the accepted full-sync topology."""

    store_name = "projections"
