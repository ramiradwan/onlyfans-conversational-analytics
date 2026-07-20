"""Read-only command-line rebuild of deterministic analytics artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.errors import AnalyticsError, CanonicalAccountNotFound
from app.analytics.pipeline import AnalyticsPipeline
from app.persistence.history import HistoryRepository
from app.persistence.migrations import (
    Migration,
    MigrationError,
    MigrationRunner,
    load_migration_catalog,
)


class RebuildFailure(RuntimeError):
    """Stable rebuild failure that contains no canonical values or paths."""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    mechanism: str
    volume: int
    file_id: int


@dataclass(frozen=True, slots=True)
class WindowsAclEvidence:
    protected: bool
    ace_count: int
    owner_sid: str
    only_owner_has_access: bool


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as error:
            raise RebuildFailure(
                "canonical_source_unsafe",
                "The canonical database path cannot be validated safely.",
            ) from error
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            raise RebuildFailure(
                "canonical_source_unsafe",
                "The canonical database path cannot use links or reparse points.",
            )


def _safe_absolute_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if ".." in candidate.parts:
        raise RebuildFailure(
            "canonical_source_unsafe",
            "The canonical database path cannot use parent aliases.",
        )
    try:
        absolute = Path(os.path.abspath(os.fspath(candidate)))
    except (OSError, TypeError, ValueError) as error:
        raise RebuildFailure(
            "canonical_source_unsafe",
            "The canonical database path cannot be validated safely.",
        ) from error
    _reject_link_components(absolute)
    return absolute


def _windows_file_identity(path: Path) -> FileIdentity:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0001 | 0x0002 | 0x0004,  # share read, write, and delete
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_id = (int(information.nFileIndexHigh) << 32) | int(
            information.nFileIndexLow
        )
        return FileIdentity(
            mechanism="windows_file_id",
            volume=int(information.dwVolumeSerialNumber),
            file_id=file_id,
        )
    finally:
        close_handle(handle)


def _file_identity(path: Path, *, platform_name: str | None = None) -> FileIdentity:
    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "nt":
        return _windows_file_identity(path)
    metadata = os.stat(path, follow_symlinks=False)
    return FileIdentity(
        mechanism="stat",
        volume=int(metadata.st_dev),
        file_id=int(metadata.st_ino),
    )


def _normalize_schema_sql(value: str) -> str:
    return " ".join(value.strip().rstrip(";").split())


def _schema_signature(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE type IN ('table', 'index', 'trigger')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    return {
        (str(row[0]), str(row[1])): (
            str(row[2]),
            _normalize_schema_sql(str(row[3])),
        )
        for row in rows
    }


def _expected_schema_signature(
    migrations: list[Migration],
) -> dict[tuple[str, str], tuple[str, str]]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        MigrationRunner._ensure_ledger(connection)
        for migration in migrations:
            connection.executescript(migration.sql)
        return _schema_signature(connection)
    finally:
        connection.close()


class ReadOnlyCanonicalDatabase:
    """One identity-pinned SQLite snapshot used for validation and replay."""

    def __init__(self, path: str | Path) -> None:
        self.path = _safe_absolute_path(path)
        if not self.path.is_file():
            raise RebuildFailure(
                "canonical_database_missing",
                "The canonical database does not exist.",
            )
        self.identity: FileIdentity | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> "ReadOnlyCanonicalDatabase":
        try:
            before_open = _file_identity(self.path)
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise sqlite3.OperationalError("query-only mode was not enabled")
            after_open = _file_identity(self.path)
            if after_open != before_open:
                connection.close()
                raise RebuildFailure(
                    "canonical_source_changed",
                    "The canonical database identity changed during rebuild.",
                )
            connection.execute("BEGIN")
            self.identity = before_open
            self._connection = connection
            return self
        except RebuildFailure:
            raise
        except (OSError, sqlite3.Error) as error:
            raise RebuildFailure(
                "canonical_database_invalid",
                "The canonical database could not be opened read-only.",
            ) from error

    def __exit__(self, exc_type, exc, traceback) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RebuildFailure(
                "canonical_database_invalid",
                "The canonical database is not open.",
            )
        return self._connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Yield the pinned transaction; never reopen the source pathname."""

        yield self.connection

    def verify_identity(self) -> None:
        expected = self.identity
        if expected is None:
            raise RebuildFailure(
                "canonical_database_invalid",
                "The canonical database is not open.",
            )
        try:
            _reject_link_components(self.path)
            current = _file_identity(self.path)
        except (OSError, RebuildFailure) as error:
            raise RebuildFailure(
                "canonical_source_changed",
                "The canonical database identity changed during rebuild.",
            ) from error
        if current != expected:
            raise RebuildFailure(
                "canonical_source_changed",
                "The canonical database identity changed during rebuild.",
            )

    def validate_schema(self) -> None:
        connection = self.connection
        try:
            integrity_rows = connection.execute(
                "PRAGMA integrity_check"
            ).fetchall()
            if (
                len(integrity_rows) != 1
                or str(integrity_rows[0][0]) != "ok"
            ):
                raise RebuildFailure(
                    "canonical_database_invalid",
                    "The canonical database failed integrity validation.",
                )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RebuildFailure(
                    "canonical_database_invalid",
                    "The canonical database failed integrity validation.",
                )

            catalog = load_migration_catalog()
            rows = connection.execute(
                """
                SELECT version, name, checksum
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()
            versions = [int(row["version"]) for row in rows]
            if (
                not versions
                or versions != list(range(1, len(versions) + 1))
                or len(versions) > len(catalog)
            ):
                raise RebuildFailure(
                    "canonical_schema_incompatible",
                    "The canonical database schema is incompatible.",
                )
            for row, migration in zip(rows, catalog, strict=False):
                if (
                    int(row["version"]) != migration.version
                    or str(row["name"]) != migration.name
                    or str(row["checksum"]) != migration.checksum
                ):
                    raise RebuildFailure(
                        "canonical_schema_incompatible",
                        "The canonical database schema is incompatible.",
                    )
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if user_version != versions[-1]:
                raise RebuildFailure(
                    "canonical_schema_incompatible",
                    "The canonical database schema is incompatible.",
                )
            expected = _expected_schema_signature(catalog[: len(versions)])
            if _schema_signature(connection) != expected:
                raise RebuildFailure(
                    "canonical_schema_incompatible",
                    "The canonical database schema is incompatible.",
                )
        except RebuildFailure:
            raise
        except (MigrationError, OSError, sqlite3.Error, UnicodeError) as error:
            raise RebuildFailure(
                "canonical_schema_incompatible",
                "The canonical database schema is incompatible.",
            ) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild derived analytics from an existing canonical database."
    )
    parser.add_argument("--canonical-path", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Atomically write stable JSON here; stdout is used when omitted.",
    )
    return parser


def _account_id(arguments: argparse.Namespace) -> str:
    account_id = getattr(arguments, "account_id", "")
    if not isinstance(account_id, str) or not account_id.strip():
        raise RebuildFailure(
            "canonical_account_invalid",
            "A canonical account binding is required.",
        )
    return account_id


@contextmanager
def _open_source(
    arguments: argparse.Namespace,
) -> Iterator[tuple[HistoryAnalyticsSource, ReadOnlyCanonicalDatabase]]:
    if getattr(arguments, "backend", "sqlite") != "sqlite":
        raise RebuildFailure(
            "canonical_backend_invalid",
            "Rebuild requires an existing canonical SQLite database.",
        )
    canonical_path = getattr(arguments, "canonical_path", None)
    if canonical_path is None:
        raise RebuildFailure(
            "canonical_database_missing",
            "The canonical database path is required.",
        )
    database = ReadOnlyCanonicalDatabase(canonical_path)
    with database:
        database.validate_schema()
        # The pinned read-only connection is bound directly; HistoryRepository
        # itself is never touched (see HistoryAnalyticsSource._read), so an
        # uninitialized instance is safe here.
        source = HistoryAnalyticsSource(
            HistoryRepository.__new__(HistoryRepository),
            connection=database.connection,
        )
        yield source, database


def _serialized_projection(
    source: HistoryAnalyticsSource,
    account_id: str,
) -> str:
    try:
        if not source.account_exists(account_id):
            raise RebuildFailure(
                "canonical_account_not_found",
                "The canonical account does not exist.",
            )
        artifact = AnalyticsPipeline(source).rebuild_account(account_id).artifact
        return json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except RebuildFailure:
        raise
    except CanonicalAccountNotFound as error:
        raise RebuildFailure(
            "canonical_account_not_found",
            "The canonical account does not exist.",
        ) from error
    except AnalyticsError as error:
        raise RebuildFailure(
            "analytics_rebuild_unavailable",
            "Analytics could not be rebuilt from canonical state.",
        ) from error
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise RebuildFailure(
            "canonical_data_invalid",
            "Canonical data is not valid for analytics rebuild.",
        ) from error
    except Exception as error:
        raise RebuildFailure(
            "analytics_rebuild_failed",
            "Analytics rebuild failed safely.",
        ) from error


def rebuild_from_args(arguments: argparse.Namespace) -> str:
    account_id = _account_id(arguments)
    with _open_source(arguments) as (source, database):
        serialized = _serialized_projection(source, account_id)
        database.verify_identity()
        return serialized


def _sid_string(sid: object, advapi32: object, kernel32: object) -> str:
    import ctypes

    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_int
    value = ctypes.c_wchar_p()
    if not convert(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    try:
        return str(value.value)
    finally:
        local_free(ctypes.cast(value, ctypes.c_void_p))


def _windows_acl_evidence(path: Path) -> WindowsAclEvidence:
    import ctypes

    class Acl(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort),
            ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_control.restype = ctypes.c_int
    get_ace = advapi32.GetAce
    get_ace.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_ace.restype = ctypes.c_int

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = get_security(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER and DACL
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        owner_sid = _sid_string(owner, advapi32, kernel32)
        control = ctypes.c_ushort()
        revision = ctypes.c_uint32()
        if not get_control(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl.value:
            return WindowsAclEvidence(False, 0, owner_sid, False)
        acl = ctypes.cast(dacl, ctypes.POINTER(Acl)).contents
        only_owner = acl.AceCount == 1
        if only_owner:
            ace = ctypes.c_void_p()
            if not get_ace(dacl, 0, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            ace_sid = ctypes.c_void_p(ace.value + 8)
            only_owner = (
                header.AceType == 0
                and _sid_string(ace_sid, advapi32, kernel32) == owner_sid
                and mask & 0x001F01FF == 0x001F01FF
            )
        return WindowsAclEvidence(
            protected=bool(control.value & 0x1000),
            ace_count=int(acl.AceCount),
            owner_sid=owner_sid,
            only_owner_has_access=only_owner,
        )
    finally:
        local_free(descriptor)


def _set_windows_owner_only_acl(path: Path) -> None:
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    owner = ctypes.c_void_p()
    original_descriptor = ctypes.c_void_p()
    result = get_security(
        str(path),
        1,
        0x00000001,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(original_descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        owner_sid = _sid_string(owner, advapi32, kernel32)
    finally:
        local_free(original_descriptor)

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    convert.restype = ctypes.c_int
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    get_dacl.restype = ctypes.c_int
    set_security = advapi32.SetNamedSecurityInfoW
    set_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = ctypes.c_uint32

    descriptor = ctypes.c_void_p()
    if not convert(
        f"D:P(A;;FA;;;{owner_sid})",
        1,
        ctypes.byref(descriptor),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = ctypes.c_int()
        defaulted = ctypes.c_int()
        dacl = ctypes.c_void_p()
        if not get_dacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise OSError("owner-only DACL was not created")
        result = set_security(
            str(path),
            1,
            0x00000004 | 0x80000000,  # DACL and protected DACL
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ctypes.WinError(result)
    finally:
        local_free(descriptor)


def _verify_private_permissions(
    path: Path,
    *,
    platform_name: str | None = None,
) -> None:
    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "nt":
        evidence = _windows_acl_evidence(path)
        if (
            not evidence.protected
            or evidence.ace_count != 1
            or not evidence.only_owner_has_access
        ):
            raise OSError("output DACL is not owner-only")
        return
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise OSError("output mode is not owner-only")


def _apply_private_permissions(
    path: Path,
    *,
    platform_name: str | None = None,
) -> None:
    selected_platform = os.name if platform_name is None else platform_name
    try:
        if selected_platform == "nt":
            _set_windows_owner_only_acl(path)
        else:
            os.chmod(path, 0o600)
        _verify_private_permissions(path, platform_name=selected_platform)
    except OSError as error:
        raise RebuildFailure(
            "rebuild_output_security_failed",
            "Private output permissions could not be established.",
        ) from error


def _atomic_private_write(
    output: Path,
    serialized: str,
    *,
    canonical_path: Path,
    canonical_identity: FileIdentity,
    before_publish: Callable[[], None],
) -> None:
    destination = _safe_absolute_path(output)
    source_path = _safe_absolute_path(canonical_path)
    if destination == source_path:
        raise RebuildFailure(
            "rebuild_output_conflicts_with_source",
            "The rebuild output cannot replace the canonical database.",
        )
    if destination.exists():
        try:
            if _file_identity(destination) == canonical_identity:
                raise RebuildFailure(
                    "rebuild_output_conflicts_with_source",
                    "The rebuild output cannot alias the canonical database.",
                )
        except RebuildFailure:
            raise
        except OSError as error:
            raise RebuildFailure(
                "rebuild_output_write_failed",
                "The rebuild output path could not be validated.",
            ) from error
    if not destination.parent.is_dir():
        raise RebuildFailure(
            "rebuild_output_directory_missing",
            "The rebuild output directory does not exist.",
        )

    descriptor = -1
    temporary: Path | None = None
    published = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        _apply_private_permissions(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        before_publish()
        os.replace(temporary, destination)
        published = True
        _apply_private_permissions(destination)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            directory_descriptor = os.open(
                destination.parent,
                os.O_RDONLY | directory_flag,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except RebuildFailure as error:
        if published and error.code == "rebuild_output_security_failed":
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except OSError as error:
        raise RebuildFailure(
            "rebuild_output_write_failed",
            "The rebuild output could not be written safely.",
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run(arguments: argparse.Namespace) -> None:
    account_id = _account_id(arguments)
    with _open_source(arguments) as (source, database):
        serialized = _serialized_projection(source, account_id)
        if arguments.output is None:
            database.verify_identity()
            print(serialized, end="")
            return
        identity = database.identity
        if identity is None:
            raise RebuildFailure(
                "canonical_database_invalid",
                "The canonical database is not open.",
            )
        _atomic_private_write(
            arguments.output,
            serialized,
            canonical_path=database.path,
            canonical_identity=identity,
            before_publish=database.verify_identity,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        _run(arguments)
    except RebuildFailure as error:
        raise SystemExit(f"{error.code}: {error.public_message}") from error
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
