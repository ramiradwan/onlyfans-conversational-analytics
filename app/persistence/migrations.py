"""Forward-only, checksummed, locked, restart-safe SQLite migrations."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.persistence.database import CanonicalSQLite


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
TRANSACTION_CONTROL = re.compile(
    r"\A\s*(?:(?:--[^\r\n]*)(?:\r?\n|\Z)\s*)*"
    r"(?:BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """Base class for canonical migration failures."""


class MigrationChecksumError(MigrationError):
    """An applied migration no longer matches the signed release catalog."""


class SchemaCompatibilityError(MigrationError):
    """The database ledger is missing, inconsistent, or newer than this binary."""


class MigrationLockError(MigrationError):
    """Another Brain or updater owns the installation migration lock."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


class InstallationMigrationLock:
    """A small cross-platform advisory lock held for the complete migration run."""

    def __init__(self, path: Path, *, timeout_seconds: float = 0.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "InstallationMigrationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    lock_flags = (
                        fcntl.LOCK_EX  # type: ignore[attr-defined]
                        | fcntl.LOCK_NB  # type: ignore[attr-defined]
                    )
                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(), lock_flags
                    )
                self._handle = handle
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                return self
            except OSError as error:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise MigrationLockError(
                        f"migration lock is already held: {self.path}"
                    ) from error
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def __exit__(self, exc_type, exc, traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        handle.close()


class MigrationRunner:
    """Validate and apply an ordered SQL catalog to one canonical database."""

    def __init__(
        self,
        database: CanonicalSQLite,
        *,
        migrations_dir: str | Path | None = None,
        lock_path: str | Path | None = None,
        backups_dir: str | Path | None = None,
        lock_timeout_seconds: float = 0.0,
    ) -> None:
        self.database = database
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("sql"))
        self.lock_path = Path(
            lock_path or database.path.parent / ".bridge-installation-migration.lock"
        )
        self.backups_dir = Path(backups_dir or database.path.parent / "backups")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.last_backup_path: Path | None = None

    def run(self) -> list[int]:
        catalog = self._load_catalog()
        with InstallationMigrationLock(
            self.lock_path, timeout_seconds=self.lock_timeout_seconds
        ):
            with self.database.read() as connection:
                self._ensure_ledger(connection)
                applied = self._validate_applied(connection, catalog)
                pending = [item for item in catalog if item.version not in applied]
                if not pending:
                    self._validate_database(connection)
                    return []
                self.last_backup_path = self._backup(connection, applied, catalog[-1].version)
                completed: list[int] = []
                for migration in pending:
                    self._apply(connection, migration)
                    completed.append(migration.version)
                self._validate_applied(connection, catalog)
                self._validate_database(connection)
                return completed

    def _load_catalog(self) -> list[Migration]:
        migrations: list[Migration] = []
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"migration directory is missing: {self.migrations_dir}")
        for path in sorted(self.migrations_dir.iterdir()):
            if not path.is_file():
                continue
            match = MIGRATION_NAME.fullmatch(path.name)
            if match is None:
                if path.suffix == ".sql":
                    raise MigrationError(f"invalid migration filename: {path.name}")
                continue
            raw = path.read_bytes()
            sql = raw.decode("utf-8")
            if TRANSACTION_CONTROL.search(sql):
                raise MigrationError(
                    f"migration {path.name} must not manage its own transaction"
                )
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=match.group("name"),
                    sql=sql,
                    checksum=hashlib.sha256(raw).hexdigest(),
                )
            )
        if not migrations:
            raise MigrationError("the canonical migration catalog is empty")
        versions = [item.version for item in migrations]
        expected = list(range(1, len(migrations) + 1))
        if versions != expected:
            raise MigrationError(
                f"migration versions must be contiguous from 0001: {versions!r}"
            )
        return migrations

    @staticmethod
    def _ensure_ledger(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _validate_applied(
        connection: sqlite3.Connection, catalog: list[Migration]
    ) -> dict[int, sqlite3.Row]:
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        by_version = {item.version: item for item in catalog}
        applied = {int(row["version"]): row for row in rows}
        versions = list(applied)
        if versions and versions != list(range(1, max(versions) + 1)):
            raise SchemaCompatibilityError(
                f"migration ledger is not contiguous: {versions!r}"
            )
        for version, row in applied.items():
            migration = by_version.get(version)
            if migration is None:
                raise SchemaCompatibilityError(
                    f"database schema version {version} is newer or missing from this binary"
                )
            if row["name"] != migration.name or row["checksum"] != migration.checksum:
                raise MigrationChecksumError(
                    f"applied migration {version:04d}_{row['name']} checksum/name mismatch"
                )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        ledger_version = max(versions, default=0)
        if user_version != ledger_version:
            raise SchemaCompatibilityError(
                f"PRAGMA user_version {user_version} conflicts with ledger {ledger_version}"
            )
        return applied

    def _backup(
        self,
        source: sqlite3.Connection,
        applied: dict[int, sqlite3.Row],
        target_version: int,
    ) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        current_version = max(applied, default=0)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backups_dir / (
            f"{self.database.path.name}.schema-v{current_version}-to-v{target_version}."
            f"{timestamp}-{uuid4().hex[:8]}.bak"
        )
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            backup = sqlite3.connect(temporary)
            try:
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                source.backup(backup)
            finally:
                backup.close()
            verification = sqlite3.connect(temporary)
            try:
                result = verification.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise MigrationError(
                        f"pre-migration backup verification failed: {result}"
                    )
            finally:
                verification.close()
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        return destination

    @staticmethod
    def _apply(connection: sqlite3.Connection, migration: Migration) -> None:
        name = migration.name.replace("'", "''")
        checksum = migration.checksum.replace("'", "''")
        applied_at = datetime.now(timezone.utc).isoformat().replace("'", "''")
        script = f"""
        BEGIN IMMEDIATE;
        {migration.sql}
        INSERT INTO schema_migrations(version, name, checksum, applied_at)
        VALUES ({migration.version}, '{name}', '{checksum}', '{applied_at}');
        PRAGMA user_version = {migration.version};
        COMMIT;
        """
        try:
            connection.executescript(script)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _validate_database(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError(f"post-migration integrity check failed: {integrity}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(
                f"post-migration foreign-key check failed: {violations!r}"
            )
