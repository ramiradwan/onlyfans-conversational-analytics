"""Connection management for the authoritative canonical SQLite file."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class SQLiteConfigurationError(RuntimeError):
    """Raised when SQLite cannot provide the authoritative durability profile."""


class CanonicalSQLite:
    """Open independently scoped connections with the canonical durability PRAGMAs."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.path = Path(path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise SQLiteConfigurationError(
                    f"canonical SQLite did not enter WAL mode: {journal_mode!r}"
                )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise SQLiteConfigurationError("canonical SQLite is not synchronous=FULL")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise SQLiteConfigurationError("canonical SQLite foreign keys are disabled")
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
            yield connection
            connection.commit()
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
                    f"canonical SQLite integrity check failed: {result}"
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise SQLiteConfigurationError(
                    f"canonical SQLite foreign-key check failed: {violations!r}"
                )

    def _restrict_permissions(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    # Windows ACLs are installer-owned; chmod is still useful on POSIX.
                    pass
