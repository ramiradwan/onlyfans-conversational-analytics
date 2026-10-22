"""Pre-constructor retained-state inspection and process-lifetime ownership.

Lock order is the runtime root lease, then the existing installation migration
lock, then individual database connections. SQL remains owned by the existing
forward migrator; this composition creates no replacement customer authority.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import re
import sys
import threading
from typing import Callable, Literal

from dotenv import dotenv_values

from app.core.runtime_paths import runtime_data_directory
from app.persistence.migrations import InstallationMigrationLock, MigrationLockError
from app.persistence.private_files import PrivateFileSecurityError, private_file_identity, reject_path_aliases
from app.security.local_data_key import MASTER_KEY_FILENAME, LocalDataKeyError, database_key

WRITER_LOCK_FILENAME = ".ofca-runtime-writer.lock"
DATABASES = {
    "auth.sqlite3": "auth", "canonical.sqlite3": "canonical",
    "projections.sqlite3": "projection", "analytics-projections.sqlite3": "analytics-projection",
}
_held_lease = None
_lease_guard = threading.Lock()


class StartupRefused(RuntimeError):
    """A bounded, nonsecret recovery reason before the application graph."""


@dataclass(frozen=True, slots=True)
class RetainedState:
    root: Path
    mode: Literal["fresh", "provisioning", "configured"]
    databases: tuple[str, ...]


def _validate_configuration(root: Path) -> None:
    path = root / "runtime.env"
    if path.stat().st_size > 65536:
        raise StartupRefused("retained_configuration_invalid")
    before = private_file_identity(path, repair_permissions=False)
    values = dotenv_values(path, interpolate=False)
    if private_file_identity(path, repair_permissions=False) != before:
        raise StartupRefused("retained_configuration_changed")
    expected = {
        "ENVIRONMENT": "production", "WEBSOCKET_AUTH_MODE": "local_session",
        "WEBSOCKET_BIND_HOST": "127.0.0.1", "IDENTITY_BINDING_SOURCE": "verified_grants",
        "CANONICAL_PERSISTENCE_BACKEND": "sqlite", "BROADCAST_URL": "memory://",
    }
    for variable, name in (
        ("AUTH_DATABASE_PATH", "auth.sqlite3"), ("CANONICAL_DATABASE_PATH", "canonical.sqlite3"),
        ("PROJECTION_DATABASE_PATH", "projections.sqlite3"),
        ("ANALYTICS_PROJECTION_DATABASE_PATH", "analytics-projections.sqlite3"),
    ):
        configured = values.get(variable)
        if not configured or not Path(configured).is_absolute() or reject_path_aliases(configured) != root / name:
            raise StartupRefused("retained_configuration_root_conflict")
    principal = values.get("LOCAL_PRINCIPAL_ID") or ""
    if (any(values.get(key) != value for key, value in expected.items())
            or not principal or principal.startswith("replace-with-")
            or values.get("LOCAL_BRIDGE_ROLE") not in {"creator", "operator"}
            or re.fullmatch(r"[a-p]{32}", values.get("EXTENSION_ID") or "") is None):
        raise StartupRefused("retained_configuration_invalid")
    for variable, reason in (("LOCAL_SESSION_BOOTSTRAP_TOKEN", "retained_bootstrap_token_invalid"),
                             ("SECURITY_SIGNING_SECRET", "retained_signing_secret_invalid")):
        secret = values.get(variable) or ""
        if len(secret) < 32 or secret.startswith("replace-with-"):
            raise StartupRefused(reason)


def inspect_retained_state(root: Path) -> RetainedState:
    """Read paths and reopen existing key material; create or repair nothing."""
    try:
        root = reject_path_aliases(root)
        if not root.exists():
            return RetainedState(root, "fresh", ())
        if not root.is_dir():
            raise StartupRefused("retained_root_invalid")
        entries = list(root.iterdir())
        names = {entry.name for entry in entries}
        if len({name.casefold() for name in names}) != len(names):
            raise StartupRefused("retained_root_alias")
        present = tuple(name for name in DATABASES if name in names)
        for entry in entries:
            reject_path_aliases(entry)
            base = entry.name.removesuffix("-wal").removesuffix("-shm")
            if base in DATABASES:
                if not entry.is_file() or base not in present:
                    raise StartupRefused("retained_database_layout_invalid")
            elif entry.name.endswith((".sqlite3", ".sqlite", ".db", "-wal", "-shm")):
                raise StartupRefused("retained_database_layout_invalid")
        configured = "runtime.env" in names
        key_present = MASTER_KEY_FILENAME in names
        if configured or present or key_present:
            # The test key is explicitly permitted by the existing test-only
            # crypto port. Production requires the original DPAPI sidecar.
            database_key(root / "auth.sqlite3", "auth", create_if_missing=False)
        if configured:
            if "auth.sqlite3" not in present:
                raise StartupRefused("retained_authentication_missing")
            _validate_configuration(root)
            return RetainedState(root, "configured", present)
        if any(name != "auth.sqlite3" for name in present):
            raise StartupRefused("retained_configuration_missing")
        if present or key_present:
            return RetainedState(root, "provisioning", present)
        # Only empty/non-authoritative runtime-owned containers may precede
        # first setup. Unknown files are retained evidence, never "first run".
        allowed = {WRITER_LOCK_FILENAME, ".bridge-installation-migration.lock", "webview", "temp"}
        if names - allowed:
            raise StartupRefused("retained_root_unrecognized")
        return RetainedState(root, "fresh", ())
    except (OSError, ValueError, PrivateFileSecurityError, LocalDataKeyError) as error:
        raise StartupRefused("retained_state_unavailable") from error


def acquire_runtime_ownership(data_directory: str | Path | None = None) -> RetainedState:
    """Hold ownership until the OS closes the process's descriptors.

    Reconfiguration exits the process; it does not release this lease early.
    Tests use independent processes to prove release after actual termination.
    """
    global _held_lease
    root = runtime_data_directory(data_directory)
    with _lease_guard:
        if _held_lease is not None:
            raise StartupRefused("runtime_already_initialized")
        inspect_retained_state(root)
        lease = InstallationMigrationLock(root / WRITER_LOCK_FILENAME)
        try:
            lease.__enter__()
        except (OSError, MigrationLockError) as error:
            raise StartupRefused("runtime_writer_conflict") from error
        try:
            state = inspect_retained_state(root)
        except BaseException:
            lease.__exit__(None, None, None)
            raise
        _held_lease = lease
        return state


def admit_retained_schemas(state: RetainedState, *,
                          should_cancel: Callable[[], bool] = lambda: False,
                          on_phase: Callable[[str], None] = lambda _: None) -> None:
    """Complete coordinated maintenance before a migrating constructor."""
    from app.persistence.database import SQLiteConfigurationError
    from app.persistence.migrations import MigrationError, MigrationCancelled
    from app.persistence import sqlite_api as sqlite3
    from app.persistence.upgrade_lifecycle import UpgradeStore, maintain_existing_stores, UpgradeRecoveryRequired

    application = Path(__file__).resolve().parents[1]
    stores = (
        UpgradeStore("auth.sqlite3", "auth", application / "persistence" / "auth_sql"),
        UpgradeStore("canonical.sqlite3", "canonical", application / "persistence" / "sql"),
        UpgradeStore("projections.sqlite3", "projection", application / "persistence" / "projection_sql"),
        UpgradeStore("analytics-projections.sqlite3", "analytics-projection", application / "analytics" / "sql"),
    )
    try:
        release_id = "source-catalog/v1"
        release_coordinate = None
        if getattr(sys, "frozen", False):
            from app.core.desktop_release import current_desktop_release
            from app.persistence.private_files import file_sha256
            release = current_desktop_release()
            release_id = (json.dumps(asdict(release), sort_keys=True, separators=(",", ":"))
                          if release is not None else "legacy-binary:" + file_sha256(sys.executable))
            if release is not None:
                identity = {"name": release.identity.name, "publisher": release.identity.publisher,
                            "architecture": release.identity.architecture}
                release_coordinate = {"kind": "package", "version": release.identity.version,
                    "identity": {**identity, "purpose": release.purpose,
                        "application_id": release.application_id, "update_owner": release.update_owner,
                        "app_installer_uri": release.app_installer_uri}}
            else:
                # Ordinary Inno/common-stage releases have a build-owned
                # version manifest. Missing evidence retains strict V1-style
                # exact-target recovery rather than inventing provenance.
                manifest = Path(sys.executable).parent / "release-manifest.json"
                if manifest.exists():
                    if not manifest.is_file() or manifest.stat().st_size > 65536:
                        raise ValueError("release manifest invalid")
                    def unique(pairs):
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise ValueError("release manifest invalid")
                            value[key] = item
                        return value
                    value = json.loads(manifest.read_bytes(), object_pairs_hook=unique)
                    if (not isinstance(value, dict) or value.get("schema") != "ofca-release-manifest/v1"
                            or value.get("architecture") != "x64"):
                        raise ValueError("release manifest invalid")
                    release_coordinate = {"kind": "legacy", "version": value.get("version"),
                                          "identity": {"product": "brain-bridge/v1"}}
        maintain_existing_stores(state.root, stores, release_id=release_id,
                                 release_coordinate=release_coordinate,
                                 should_cancel=should_cancel, on_phase=on_phase)
        if state.mode == "configured":
            _validate_configuration(state.root)
    except MigrationCancelled:
        raise StartupRefused("startup_cancelled") from None
    except UpgradeRecoveryRequired as error:
        raise StartupRefused(str(error)) from None
    except (OSError, ValueError, MigrationError, SQLiteConfigurationError,
            sqlite3.DatabaseError, PrivateFileSecurityError, LocalDataKeyError):
        # Ledger names and database error details are untrusted retained data.
        # The process boundary reports only the closed recovery reason.
        raise StartupRefused("retained_schema_incompatible") from None
