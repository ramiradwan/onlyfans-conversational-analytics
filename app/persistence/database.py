"""Connection management for independently scoped local SQLite files."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from app.persistence import sqlite_api as sqlite3
from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    reject_path_aliases,
)
from app.security.local_data_key import (
    LocalDataKeyError,
    database_key,
    protect_local_secret,
)


class SQLiteConfigurationError(RuntimeError):
    """Raised when SQLite cannot provide the required durability profile."""


def open_encrypted_sqlite(
    path: str | Path,
    encryption_key: bytes,
    *,
    read_only: bool = False,
    isolation_level: str | None = None,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Open a SQLCipher database with explicit key material.

    This narrow escape hatch exists for encrypted backup verification and
    re-encryption during restore. Normal runtime code uses ``LocalSQLite``.
    """

    memory_database = str(path) == ":memory:"
    target = Path(path).expanduser().resolve() if not memory_database else None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    location = (
        ":memory:"
        if target is None
        else f"{target.as_uri()}?mode=ro"
        if read_only
        else target
    )
    connection = sqlite3.connect(
        location,
        uri=read_only,
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
    )
    try:
        _configure_connection_cipher(connection, encryption_key)
        connection.row_factory = sqlite3.Row
        return connection
    except Exception:
        connection.close()
        raise


def _configure_connection_cipher(
    connection: sqlite3.Connection, encryption_key: bytes
) -> None:
    if len(encryption_key) != 32:
        raise SQLiteConfigurationError("SQLite encryption key must contain 32 bytes")
    key_hex = encryption_key.hex()
    connection.execute(f'PRAGMA key = "x\'{key_hex}\'"')
    sqlite3.require_cipher(connection)
    # The packaged Windows community build cannot reliably VirtualLock memory
    # and emits an unbounded warning stream when this optional hardening mode
    # is forced on. At-rest encryption remains mandatory.
    connection.execute("PRAGMA cipher_memory_security = OFF")
    connection.execute("PRAGMA cipher_plaintext_header_size = 0")


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

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        key_scope: str | None = None,
        encryption_key: bytes | None = None,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        try:
            self.path = reject_path_aliases(path)
        except PrivateFileSecurityError as error:
            raise SQLiteConfigurationError("SQLite path is not safe") from error
        self.busy_timeout_ms = busy_timeout_ms
        self.key_scope = key_scope or self.store_name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._encryption_key = bytes(
                encryption_key
                if encryption_key is not None
                else database_key(self.path, self.key_scope)
            )
        except LocalDataKeyError as error:
            raise SQLiteConfigurationError(
                f"{self.store_name} database key is unavailable"
            ) from error
        if len(self._encryption_key) != 32:
            raise SQLiteConfigurationError("SQLite encryption key must contain 32 bytes")

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
        try:
            self._configure_cipher(connection)
            connection.row_factory = sqlite3.Row
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

    def open_detached(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        isolation_level: str | None = None,
        query_only: bool = False,
    ) -> sqlite3.Connection:
        """Open an encrypted auxiliary copy under this database's key.

        Backup and migration code must use this method so SQLCipher never
        emits an unkeyed temporary database.
        """

        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        location = f"{target.as_uri()}?mode=ro" if read_only else target
        connection = sqlite3.connect(
            location,
            uri=read_only,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=isolation_level,
            check_same_thread=False,
        )
        try:
            self._configure_cipher(connection)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            if query_only:
                connection.execute("PRAGMA query_only = ON")
                if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                    raise SQLiteConfigurationError("SQLite query-only mode was refused")
            return connection
        except Exception:
            connection.close()
            raise

    def protected_encryption_key(self, *, purpose: str) -> bytes:
        """Return a current-user-wrapped copy for an encrypted backup sidecar."""

        try:
            return protect_local_secret(self._encryption_key, purpose=purpose)
        except LocalDataKeyError as error:
            raise SQLiteConfigurationError("SQLite backup key protection failed") from error

    def _configure_cipher(self, connection: sqlite3.Connection) -> None:
        _configure_connection_cipher(connection, self._encryption_key)

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


class AuthSQLite(LocalSQLite):
    """Authoritative local authentication database."""

    store_name = "auth"


class ProjectionsSQLite(LocalSQLite):
    """Disposable projections database using the accepted full-sync topology."""

    store_name = "projections"


# One class serves two derivation scopes, so its callers name the scope
# explicitly instead of relying on ``store_name``. Readers outside the runtime
# import these rather than repeating the literals.
PROJECTION_KEY_SCOPE = "projection"
ANALYTICS_PROJECTION_KEY_SCOPE = "analytics-projection"
