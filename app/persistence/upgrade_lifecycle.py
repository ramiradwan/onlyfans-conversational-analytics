"""Coordinated retained-schema maintenance before application construction.

The composition owner holds the process-lifetime root lease. Catalog paths are
inputs, so this persistence owner never imports the analytics application.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
from typing import Callable
from uuid import uuid4

from app.persistence.database import LocalSQLite, open_encrypted_sqlite
from app.persistence import sqlite_api as sqlite3
from app.persistence.migrations import (MigrationRunner, SchemaInspection, SchemaCompatibilityError,
                                       MigrationCancelled, load_migration_catalog, resolve_migration_catalog)
from app.persistence.private_files import (private_file_identity, reject_path_aliases,
    apply_private_file_security, apply_private_directory_security, file_sha256, sync_file, sync_directory)
from app.security.local_data_key import (database_key, MASTER_KEY_FILENAME,
                                        protect_local_secret, unprotect_local_secret)

JOURNAL_FILENAME = ".ofca-upgrade-state.dpapi"
CHECKPOINT_DIRECTORY = "upgrade-checkpoint"
_MANIFEST = "checkpoint.dpapi"
_MATERIAL = "material.dpapi"
_MATERIAL_FILES = ("runtime.env", MASTER_KEY_FILENAME)
_DATABASES = {"auth.sqlite3", "canonical.sqlite3", "projections.sqlite3", "analytics-projections.sqlite3"}
_AUTHORITATIVE = {"auth.sqlite3", "canonical.sqlite3"}
_MEMBERS = {_MANIFEST, _MATERIAL, "auth.sqlite3.bak", "canonical.sqlite3.bak"}
_TEMPORARY = re.compile(r"^\.(?:checkpoint\.dpapi|material\.dpapi|auth\.sqlite3\.bak|canonical\.sqlite3\.bak)\.[0-9a-f]{32}\.tmp(?:-wal|-shm|-journal)?$")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_METADATA = 262144
_SECRET_FILE_LIMIT = 65536
_RESERVE_BYTES = 64 * 1024 * 1024
_PROTECTION_PURPOSE = "desktop-upgrade:v1"


class UpgradeRecoveryRequired(RuntimeError):
    """Closed, nonsecret refusal; no automatic restoration is available."""


@dataclass(frozen=True, slots=True)
class UpgradeStore:
    filename: str
    key_scope: str
    catalog: Path


@contextmanager
def _existing_snapshot(root: Path, store: UpgradeStore, should_cancel: Callable[[], bool]):
    _check_cancel(should_cancel)
    path = reject_path_aliases(root / store.filename)
    identity = private_file_identity(path, repair_permissions=False)
    wal, shm = (path.with_name(path.name + suffix) for suffix in ("-wal", "-shm"))
    for sidecar in (wal, shm):
        reject_path_aliases(sidecar)
        if sidecar.exists():
            private_file_identity(sidecar, repair_permissions=False)
    if wal.exists() and not shm.is_file():
        raise SchemaCompatibilityError("retained WAL requires explicit index recovery")
    key = database_key(path, store.key_scope, create_if_missing=False)
    with closing(open_encrypted_sqlite(path, key, read_only=True, immutable=not wal.exists())) as connection:
        connection.set_progress_handler(lambda: int(should_cancel()), 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            yield connection, key
        except sqlite3.DatabaseError:
            _check_cancel(should_cancel)
            raise
    if private_file_identity(path, repair_permissions=False) != identity:
        raise SchemaCompatibilityError("retained database changed during inspection")


def inspect_existing_stores(root: Path, stores: tuple[UpgradeStore, ...], *,
                            should_cancel: Callable[[], bool] = lambda: False) -> dict[str, SchemaInspection]:
    """Open all retained stores with existing keys, no SQL or ACL repair.

    Immutable opening is limited to a closed database without a WAL, under the
    root lease. It would hide committed WAL rows. WAL inspection uses ordinary
    read-only opening and requires the existing WAL index; missing-index
    recovery is explicit rather than creating files during admission. SQLite
    may update its existing scratch WAL index, but cannot mutate authority.
    """
    result: dict[str, SchemaInspection] = {}
    for store in stores:
        path = reject_path_aliases(root / store.filename)
        if not path.exists():
            continue
        with _existing_snapshot(root, store, should_cancel) as (connection, _):
            result[store.filename] = MigrationRunner.inspect(connection, migrations_dir=store.catalog)
    return result


def _check_cancel(should_cancel: Callable[[], bool]) -> None:
    if should_cancel():
        raise MigrationCancelled("startup maintenance cancelled")


def _available_disk_bytes(root: Path) -> int:
    if os.name != "nt":
        return shutil.disk_usage(root).free
    # Python 3.11's Windows disk_usage returns volume-wide free space. The
    # first Win32 output accounts for the current user's enforced quota.
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetDiskFreeSpaceExW
    counter = ctypes.c_ulonglong
    query.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(counter),
                      ctypes.POINTER(counter), ctypes.POINTER(counter))
    query.restype = ctypes.c_int
    available = counter()
    if not query(os.fspath(root), ctypes.byref(available), None, None):
        raise ctypes.WinError(ctypes.get_last_error())
    return available.value


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpgradeRecoveryRequired("upgrade_metadata_invalid")
        result[key] = value
    return result


def _read_private(path: Path, limit: int) -> bytes:
    reject_path_aliases(path)
    before = private_file_identity(path, repair_permissions=False)
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit or private_file_identity(path, repair_permissions=False) != before:
        raise UpgradeRecoveryRequired("upgrade_file_changed_or_unbounded")
    return payload


def _read_protected(path: Path):
    plain = unprotect_local_secret(_read_private(path, _MAX_METADATA * 2), purpose=_PROTECTION_PURPOSE)
    if len(plain) > _MAX_METADATA:
        raise UpgradeRecoveryRequired("upgrade_metadata_invalid")
    try:
        return json.loads(plain, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError):
        raise UpgradeRecoveryRequired("upgrade_metadata_invalid") from None


def _atomic_protected(path: Path, value) -> None:
    plain = _json(value)
    if len(plain) > _MAX_METADATA:
        raise UpgradeRecoveryRequired("upgrade_metadata_invalid")
    payload = protect_local_secret(plain, purpose=_PROTECTION_PURPOSE)
    reject_path_aliases(path)
    if path.exists():
        private_file_identity(path, repair_permissions=False)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            apply_private_file_security(temporary)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        reject_path_aliases(path)
        os.replace(temporary, path)
        private_file_identity(path, repair_permissions=False)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _materials(root: Path) -> dict[str, str | None]:
    return {name: (base64.b64encode(_read_private(root / name, _SECRET_FILE_LIMIT)).decode("ascii")
                   if (root / name).exists() else None) for name in _MATERIAL_FILES}


def _material_digests(materials) -> dict[str, str | None]:
    return {name: hashlib.sha256(base64.b64decode(value, validate=True)).hexdigest()
            if value is not None else None for name, value in materials.items()}


def _file_record(path: Path, should_cancel: Callable[[], bool] = lambda: False) -> dict:
    reject_path_aliases(path)
    before = private_file_identity(path, repair_permissions=False)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            _check_cancel(should_cancel)
            digest.update(block)
    record = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    if private_file_identity(path, repair_permissions=False) != before:
        raise UpgradeRecoveryRequired("upgrade_checkpoint_changed")
    return record


def _version(value, *, packaged: bool) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ValueError()
    parts = value.split(".")
    if len(parts) != (4 if packaged else 3) or any(not p.isascii() or not p.isdecimal() for p in parts):
        raise ValueError()
    numbers = tuple(int(p) for p in parts)
    if any(str(n) != p or n > 65535 for n, p in zip(numbers, parts)) or (packaged and numbers[0] == 0):
        raise ValueError()
    return numbers


def _validate_release(value):
    if (not isinstance(value, dict) or set(value) != {"id", "kind", "identity", "version"}
            or not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 16384
            or value["kind"] not in {"package", "legacy"}):
        raise ValueError()
    identity = value["identity"]
    if value["kind"] == "legacy":
        if identity != {"product": "brain-bridge/v1"}:
            raise ValueError()
    else:
        keys = {"purpose", "name", "publisher", "architecture", "application_id", "update_owner", "app_installer_uri"}
        if (not isinstance(identity, dict) or set(identity) != keys
                or identity["purpose"] not in {"production", "qualification"}
                or identity["architecture"] != "x64"
                or identity["update_owner"] not in {"direct-appinstaller", "microsoft-store"}):
            raise ValueError()
        for name in keys - {"app_installer_uri"}:
            if (not isinstance(identity[name], str) or not 1 <= len(identity[name]) <= 8192
                    or any(ord(character) < 32 for character in identity[name])):
                raise ValueError()
        uri = identity["app_installer_uri"]
        if ((identity["update_owner"] == "microsoft-store" and uri is not None)
                or (identity["update_owner"] == "direct-appinstaller" and
                    (not isinstance(uri, str) or not uri.startswith("https://") or len(uri) > 2048))):
            raise ValueError()
    _version(value["version"], packaged=value["kind"] == "package")


def _validate_catalogs(catalogs, schemas):
    if not isinstance(catalogs, dict) or set(catalogs) != set(schemas):
        raise ValueError()
    for name, entries in catalogs.items():
        if not isinstance(entries, list) or len(entries) != schemas[name]["target"]:
            raise ValueError()
        for version, entry in enumerate(entries, 1):
            if (not isinstance(entry, list) or len(entry) != 3
                    or type(entry[0]) is not int or entry[0] != version
                    or not isinstance(entry[1], str) or not 1 <= len(entry[1]) <= 256
                    or any(ord(character) < 32 for character in entry[1])
                    or not isinstance(entry[2], str) or not _DIGEST.fullmatch(entry[2])):
                raise ValueError()


def _target(release, catalogs) -> str:
    return hashlib.sha256(_json({"release": release, "catalogs": catalogs})).hexdigest()


def _catalog_proofs(root, stores, inspections, should_cancel):
    result = {}
    for store in stores:
        if store.filename in inspections:
            with _existing_snapshot(root, store, should_cancel) as (connection, _):
                catalog = resolve_migration_catalog(connection, store.catalog)
            result[store.filename] = [[m.version, m.name, m.checksum] for m in catalog]
    return result


def _effective(metadata):
    return metadata["resume"] if metadata["schema"] == "ofca-upgrade/v2" else metadata


def _can_advance(previous, current) -> bool:
    old, new = previous["release"], current["release"]
    if old["kind"] != new["kind"] or old["identity"] != new["identity"]:
        return False
    old_version = _version(old["version"], packaged=old["kind"] == "package")
    new_version = _version(new["version"], packaged=new["kind"] == "package")
    if new_version <= old_version:
        return False
    if set(previous["catalogs"]) != set(current["catalogs"]):
        return False
    return all(current["catalogs"][name][:len(entries)] == entries
               for name, entries in previous["catalogs"].items())


def _validate_metadata(value, *, checkpoint: bool):
    prefix = "ofca-upgrade-checkpoint" if checkpoint else "ofca-upgrade"
    v2 = isinstance(value, dict) and value.get("schema") == prefix + "/v2"
    schema = prefix + ("/v2" if v2 else "/v1")
    fields = {"schema", "phase", "target", "root", "schemas", "materials", "files"}
    if v2:
        fields |= {"release", "catalogs"}
        if not checkpoint:
            fields.add("resume")
    phases = {"building", "prepared"} if checkpoint else {"intent", "complete"}
    try:
        if (not isinstance(value, dict) or set(value) != fields or value["schema"] != schema
                or value["phase"] not in phases):
            raise ValueError()
        if any(not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]) for key in ("target", "root")):
            raise ValueError()
        schemas = value["schemas"]
        if not isinstance(schemas, dict) or not schemas or not set(schemas) <= _DATABASES:
            raise ValueError()
        for versions in schemas.values():
            if (not isinstance(versions, dict) or set(versions) != {"source", "target"}
                    or any(type(v) is not int or not 0 <= v <= 9999 for v in versions.values())
                    or versions["source"] > versions["target"]):
                raise ValueError()
        if v2:
            _validate_release(value["release"])
            _validate_catalogs(value["catalogs"], schemas)
            if value["target"] != _target(value["release"], value["catalogs"]):
                raise ValueError()
            if not checkpoint:
                resume = value["resume"]
                if (not isinstance(resume, dict) or set(resume) != {"target", "release", "catalogs", "schemas"}
                        or not isinstance(resume["schemas"], dict) or set(resume["schemas"]) != set(schemas)):
                    raise ValueError()
                for name, versions in resume["schemas"].items():
                    if (not isinstance(versions, dict) or set(versions) != {"source", "target"}
                            or versions["source"] != schemas[name]["source"]
                            or type(versions["source"]) is not int
                            or type(versions["target"]) is not int
                            or not schemas[name]["target"] <= versions["target"] <= 9999):
                        raise ValueError()
                _validate_release(resume["release"])
                _validate_catalogs(resume["catalogs"], resume["schemas"])
                if resume["target"] != _target(resume["release"], resume["catalogs"]):
                    raise ValueError()
                if resume["target"] != value["target"] and not _can_advance(value, resume):
                    raise ValueError()
        material = value["materials"]
        if not isinstance(material, dict) or set(material) != set(_MATERIAL_FILES):
            raise ValueError()
        if any(v is not None and (not isinstance(v, str) or not _DIGEST.fullmatch(v)) for v in material.values()):
            raise ValueError()
        files = value["files"]
        if not isinstance(files, dict) or not set(files) <= _MEMBERS:
            raise ValueError()
        for record in files.values():
            if (not isinstance(record, dict) or set(record) != {"sha256", "bytes"}
                    or not isinstance(record["sha256"], str) or not _DIGEST.fullmatch(record["sha256"])
                    or type(record["bytes"]) is not int or not 0 < record["bytes"] < 2**63):
                raise ValueError()
        required = {_MATERIAL} | {name + ".bak" for name in set(schemas) & _AUTHORITATIVE}
        if checkpoint:
            if _MANIFEST in files or (value["phase"] == "prepared" and set(files) != required):
                raise ValueError()
        elif set(files) != required | {_MANIFEST}:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise UpgradeRecoveryRequired("upgrade_metadata_invalid") from None
    return value


def _purge_checkpoint(cohort: Path, files: dict | None, *, unfinished: bool = False,
                      should_cancel: Callable[[], bool] = lambda: False) -> None:
    """Delete only flat, named Product-owned members; never follow directories."""
    reject_path_aliases(cohort)
    if not cohort.exists():
        return
    entries = list(cohort.iterdir())
    for entry in entries:
        reject_path_aliases(entry)
        if not entry.is_file():
            raise UpgradeRecoveryRequired("upgrade_checkpoint_layout_invalid")
        if _TEMPORARY.fullmatch(entry.name):
            private_file_identity(entry)  # Harden owned, possibly incomplete scratch files.
        elif entry.name not in (files or {}) and not (unfinished and entry.name in _MEMBERS):
            raise UpgradeRecoveryRequired("upgrade_checkpoint_layout_invalid")
        elif not unfinished and _file_record(entry, should_cancel) != files[entry.name]:
            raise UpgradeRecoveryRequired("upgrade_checkpoint_changed")
        else:
            private_file_identity(entry, repair_permissions=False)
    # Keep the authenticated manifest until all other members are gone.
    for entry in sorted(entries, key=lambda p: p.name == _MANIFEST):
        entry.unlink()
    cohort.rmdir()
    sync_directory(cohort.parent)


def _snapshot_database(root: Path, store: UpgradeStore, path: Path, inspection: SchemaInspection,
                       should_cancel: Callable[[], bool]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    apply_private_file_security(temporary)
    with _existing_snapshot(root, store, should_cancel) as (source, key):
        if MigrationRunner.inspect(source, migrations_dir=store.catalog) != inspection:
            raise UpgradeRecoveryRequired("upgrade_source_changed")
        with closing(open_encrypted_sqlite(temporary, key)) as destination:
            source.backup(destination, pages=1024, progress=lambda *_: _check_cancel(should_cancel))
    apply_private_file_security(temporary)
    with closing(open_encrypted_sqlite(temporary, key, read_only=True, immutable=True)) as verification:
        if MigrationRunner.inspect(verification, migrations_dir=store.catalog) != inspection:
            raise UpgradeRecoveryRequired("upgrade_checkpoint_invalid")
    sync_file(temporary)
    os.replace(temporary, path)
    sync_directory(path.parent)


def _verify_checkpoint(root: Path, cohort: Path, metadata: dict, stores, *,
                       should_cancel: Callable[[], bool]) -> None:
    if {entry.name for entry in cohort.iterdir()} != set(metadata["files"]) | {_MANIFEST}:
        raise UpgradeRecoveryRequired("upgrade_checkpoint_layout_invalid")
    for name, record in metadata["files"].items():
        _check_cancel(should_cancel)
        if _file_record(cohort / name, should_cancel) != record:
            raise UpgradeRecoveryRequired("upgrade_checkpoint_changed")
    materials = _read_protected(cohort / _MATERIAL)
    if (not isinstance(materials, dict) or set(materials) != set(_MATERIAL_FILES)
            or materials != _materials(root) or _material_digests(materials) != metadata["materials"]):
        raise UpgradeRecoveryRequired("upgrade_material_changed")
    for store in stores:
        if store.filename not in _AUTHORITATIVE or store.filename not in metadata["schemas"]:
            continue
        key = database_key(root / store.filename, store.key_scope, create_if_missing=False)
        with closing(open_encrypted_sqlite(cohort / (store.filename + ".bak"), key,
                                          read_only=True, immutable=True)) as connection:
            connection.set_progress_handler(lambda: int(should_cancel()), 1000)
            try:
                inspected = MigrationRunner.inspect(connection, migrations_dir=store.catalog)
            except sqlite3.DatabaseError:
                _check_cancel(should_cancel)
                raise
        if inspected.current_version != metadata["schemas"][store.filename]["source"]:
            raise UpgradeRecoveryRequired("upgrade_checkpoint_invalid")


def _run_pending(root: Path, stores, inspections, should_cancel, on_phase):
    for store in stores:
        if store.filename not in inspections or not inspections[store.filename].pending_versions:
            continue
        _check_cancel(should_cancel)
        on_phase("migrating")
        database = LocalSQLite(root / store.filename, key_scope=store.key_scope,
            encryption_key=database_key(root / store.filename, store.key_scope, create_if_missing=False))
        lock = {"auth.sqlite3": ".auth-migration.lock", "projections.sqlite3": ".projection-migration.lock"}.get(
            store.filename, ".bridge-installation-migration.lock")
        backups = "projection-backups" if store.filename == "projections.sqlite3" else "backups"
        MigrationRunner(database, migrations_dir=store.catalog, lock_path=root / lock,
                        backups_dir=root / backups, should_cancel=should_cancel).run()


def _remove_journal_temporaries(root: Path) -> None:
    pattern = re.compile(r"^\." + re.escape(JOURNAL_FILENAME) + r"\.[0-9a-f]{32}\.tmp$")
    for entry in root.iterdir():
        if pattern.fullmatch(entry.name):
            reject_path_aliases(entry)
            if not entry.is_file():
                raise UpgradeRecoveryRequired("upgrade_metadata_invalid")
            private_file_identity(entry)
            entry.unlink()
    sync_directory(root)


def maintain_existing_stores(root: Path, stores: tuple[UpgradeStore, ...], *, release_id: str,
                            release_coordinate: dict | None = None,
                            should_cancel: Callable[[], bool] = lambda: False,
                            on_phase: Callable[[str], None] = lambda _: None) -> bool:
    """Checkpoint, migrate forward and complete before the application graph.

    Caller owns the process-lifetime root lease. No restore path is provided.
    At most one local checkpoint exists. Completion removes it before normal
    mutations, while an interrupted checkpoint is never subject to age pruning.
    """
    root = reject_path_aliases(root)
    if not isinstance(release_id, str) or not 1 <= len(release_id) <= 16384:
        raise ValueError("upgrade release identity is required")
    if len({s.filename for s in stores}) != len(stores) or not {s.filename for s in stores} <= _DATABASES:
        raise ValueError("upgrade stores are invalid")
    on_phase("checking")
    inspections = inspect_existing_stores(root, stores, should_cancel=should_cancel)
    _remove_journal_temporaries(root)
    journal = root / JOURNAL_FILENAME
    cohort = root / CHECKPOINT_DIRECTORY
    reject_path_aliases(journal)
    reject_path_aliases(cohort)
    root_id = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    catalogs = {s.filename: [(m.version, m.name, m.checksum) for m in load_migration_catalog(s.catalog)] for s in stores}
    legacy_target = _target(release_id, catalogs)
    release, proofs = None, None
    if release_coordinate is not None:
        if not isinstance(release_coordinate, dict) or set(release_coordinate) != {"kind", "identity", "version"}:
            raise ValueError("upgrade release coordinate is invalid")
        release = {"id": release_id, **release_coordinate}
        _validate_release(release)
        proofs = _catalog_proofs(root, stores, inspections, should_cancel)
    target = _target(release, proofs) if release is not None else legacy_target
    prior = _validate_metadata(_read_protected(journal), checkpoint=False) if journal.exists() else None
    retarget = None
    if prior is not None:
        if prior["root"] != root_id or not set(prior["schemas"]) <= set(inspections):
            raise UpgradeRecoveryRequired("upgrade_retained_state_changed")
        if prior["phase"] == "complete":
            if any(inspections[name].current_version < versions["target"] for name, versions in _effective(prior)["schemas"].items()):
                raise UpgradeRecoveryRequired("upgrade_schema_regressed")
            _purge_checkpoint(cohort, prior["files"], should_cancel=should_cancel)
            if not any(i.pending_versions for i in inspections.values()):
                return False
            journal.unlink()
            sync_directory(root)
            prior = None
        else:
            effective = _effective(prior)
            if (set(prior["schemas"]) != set(inspections)
                    or any(not versions["source"] <= inspections[name].current_version <= versions["target"]
                           for name, versions in effective["schemas"].items())):
                raise UpgradeRecoveryRequired("upgrade_target_conflict")
            if prior["schema"] == "ofca-upgrade/v1":
                # V1 never recorded unapplied history or delivery identity.
                # Do not infer that missing proof from a newer binary.
                if prior["target"] != legacy_target:
                    raise UpgradeRecoveryRequired("upgrade_target_conflict")
            elif release is None:
                raise UpgradeRecoveryRequired("upgrade_target_conflict")
            elif effective["target"] != target:
                candidate = {"target": target, "release": release, "catalogs": proofs,
                             "schemas": {name: {"source": prior["schemas"][name]["source"],
                                                "target": i.target_version} for name, i in inspections.items()}}
                if not _can_advance(effective, candidate):
                    raise UpgradeRecoveryRequired("upgrade_target_conflict")
                retarget = candidate
    pending_authority = any(i.pending_versions for name, i in inspections.items() if name in _AUTHORITATIVE)
    if prior is None and not pending_authority and not cohort.exists():
        if not any(i.pending_versions for i in inspections.values()):
            return False
        _run_pending(root, stores, inspections, should_cancel, on_phase)
        on_phase("validating")
        if any(i.pending_versions for i in inspect_existing_stores(root, stores, should_cancel=should_cancel).values()):
            raise UpgradeRecoveryRequired("upgrade_incomplete")
        return True
    _check_cancel(should_cancel)
    if prior is None:
        schemas = {name: {"source": i.current_version, "target": i.target_version} for name, i in inspections.items()}
        materials = _materials(root)
        metadata = {"schema": "ofca-upgrade-checkpoint/v2" if release is not None else "ofca-upgrade-checkpoint/v1",
                    "phase": "building", "target": target,
                    "root": root_id, "schemas": schemas, "materials": _material_digests(materials), "files": {}}
        if release is not None:
            metadata.update(release=release, catalogs=proofs)
        if cohort.exists():
            if (cohort / _MANIFEST).exists():
                previous = _validate_metadata(_read_protected(cohort / _MANIFEST), checkpoint=True)
                if any(previous[key] != metadata[key] for key in ("root", "materials")):
                    raise UpgradeRecoveryRequired("upgrade_target_conflict")
                if any(previous["schemas"].get(name, {}).get("source") != i.current_version
                       for name, i in inspections.items()) or set(previous["schemas"]) != set(inspections):
                    raise UpgradeRecoveryRequired("upgrade_target_conflict")
                if previous["target"] != target:
                    if (previous["schema"] != "ofca-upgrade-checkpoint/v2" or release is None
                            or not _can_advance(previous, metadata)):
                        raise UpgradeRecoveryRequired("upgrade_target_conflict")
                _purge_checkpoint(cohort, previous["files"], unfinished=True, should_cancel=should_cancel)
            else:
                _purge_checkpoint(cohort, None, should_cancel=should_cancel)
        size = sum((root / name).stat().st_size + ((root / (name + "-wal")).stat().st_size
                   if (root / (name + "-wal")).exists() else 0) for name in inspections)
        if _available_disk_bytes(root) < 3 * size + _RESERVE_BYTES:
            raise UpgradeRecoveryRequired("upgrade_space_insufficient")
        on_phase("checkpointing")
        cohort.mkdir()
        apply_private_directory_security(cohort)
        _atomic_protected(cohort / _MANIFEST, metadata)
        _atomic_protected(cohort / _MATERIAL, materials)
        metadata["files"][_MATERIAL] = _file_record(cohort / _MATERIAL, should_cancel)
        for store in stores:
            if store.filename in _AUTHORITATIVE and store.filename in inspections:
                _snapshot_database(root, store, cohort / (store.filename + ".bak"), inspections[store.filename], should_cancel)
                metadata["files"][store.filename + ".bak"] = _file_record(cohort / (store.filename + ".bak"), should_cancel)
                on_phase("checkpointing")
        metadata["phase"] = "prepared"
        _atomic_protected(cohort / _MANIFEST, metadata)
        _verify_checkpoint(root, cohort, metadata, stores, should_cancel=should_cancel)
        prior = {**metadata, "schema": "ofca-upgrade/v2" if release is not None else "ofca-upgrade/v1", "phase": "intent",
                 "files": {**metadata["files"], _MANIFEST: _file_record(cohort / _MANIFEST, should_cancel)}}
        if release is not None:
            prior["resume"] = {key: prior[key] for key in ("target", "release", "catalogs", "schemas")}
        _atomic_protected(journal, prior)
    else:
        metadata = _validate_metadata(_read_protected(cohort / _MANIFEST), checkpoint=True)
        if (metadata["phase"] != "prepared" or any(metadata[key] != prior[key]
                for key in ("target", "root", "schemas", "materials"))
                or {**metadata["files"], _MANIFEST: _file_record(cohort / _MANIFEST, should_cancel)} != prior["files"]):
            raise UpgradeRecoveryRequired("upgrade_checkpoint_changed")
        _verify_checkpoint(root, cohort, metadata, stores, should_cancel=should_cancel)
        if retarget is not None:
            # The checkpoint remains immutable. Persist the verified successor
            # intent in one journal replacement before any successor SQL runs.
            _check_cancel(should_cancel)
            prior["resume"] = retarget
            _atomic_protected(journal, prior)
    _check_cancel(should_cancel)
    _run_pending(root, stores, inspections, should_cancel, on_phase)
    on_phase("validating")
    final = inspect_existing_stores(root, stores, should_cancel=should_cancel)
    if (set(final) != set(inspections) or any(i.pending_versions for i in final.values())
            or _material_digests(_materials(root)) != prior["materials"]):
        raise UpgradeRecoveryRequired("upgrade_incomplete")
    _check_cancel(should_cancel)
    prior["phase"] = "complete"
    _atomic_protected(journal, prior)
    _purge_checkpoint(cohort, prior["files"], should_cancel=should_cancel)
    on_phase("complete")
    return True
