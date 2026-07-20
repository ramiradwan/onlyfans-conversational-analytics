"""Verified SQLite backups with external hashes and fail-closed restore."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.identity import pipeline_identity_digest
from app.analytics.sqlite_projection_store import (
    ProjectionValidationError,
    recompute_generation,
)
from app.models.analytics import GraphCentralityResult, GraphCommunityResult
from app.persistence.database import LocalSQLite, SQLiteConfigurationError
from app.persistence.migrations import load_migration_catalog
from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    file_sha256,
    reject_path_aliases,
    sync_directory,
    sync_file,
)
from app.persistence.projection_activation import _sqlite_identity


HighWaterReader = Callable[[sqlite3.Connection], dict[str, Any]]


class SQLiteBackupError(RuntimeError):
    """Raised when backup or restore validation fails without exposing content."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    store_name: str
    schema_version: int
    high_water: dict[str, Any]
    canonical_witnesses: list[dict[str, Any]]
    created_at: datetime
    file_sha256: str


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    path: Path
    normalized_path: str
    file_identity: tuple[int, int] | None


def _path_identity(path: str | Path) -> _PathIdentity:
    try:
        safe = reject_path_aliases(path)
    except PrivateFileSecurityError as error:
        raise SQLiteBackupError("backup_path_unsafe") from error
    normalized = os.path.normcase(os.path.normpath(os.fspath(safe)))
    try:
        metadata = os.stat(safe, follow_symlinks=False)
    except FileNotFoundError:
        file_identity = None
    except OSError as error:
        raise SQLiteBackupError("backup_path_identity_unavailable") from error
    else:
        file_identity = (int(metadata.st_dev), int(metadata.st_ino))
    return _PathIdentity(safe, normalized, file_identity)


def _identity_preflight(**roles: str | Path) -> dict[str, Path]:
    """Resolve every role before mutation and reject path/file-ID collisions."""

    identities = {name: _path_identity(path) for name, path in roles.items()}
    values = list(identities.items())
    for index, (_, left) in enumerate(values):
        for _, right in values[index + 1 :]:
            same_path = left.normalized_path == right.normalized_path
            same_file = (
                left.file_identity is not None
                and right.file_identity is not None
                and left.file_identity == right.file_identity
            )
            if same_path or same_file:
                raise SQLiteBackupError("backup_path_alias")
    return {name: identity.path for name, identity in identities.items()}


def backup_canonical_database(
    database: LocalSQLite,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> BackupManifest:
    return create_online_backup(
        database,
        destination,
        store_name="canonical",
        high_water_reader=_canonical_high_water,
        overwrite=overwrite,
    )


def backup_projections_database(
    database: LocalSQLite,
    destination: str | Path,
    *,
    overwrite: bool = False,
) -> BackupManifest:
    return create_online_backup(
        database,
        destination,
        store_name="projections",
        high_water_reader=_projections_high_water,
        overwrite=overwrite,
    )


def create_online_backup(
    database: LocalSQLite,
    destination: str | Path,
    *,
    store_name: str,
    high_water_reader: HighWaterReader,
    overwrite: bool = False,
) -> BackupManifest:
    destination_candidate = _path_identity(destination).path
    temporary_candidate = destination_candidate.with_name(
        f".{destination_candidate.name}.{uuid4().hex}.tmp"
    )
    paths = _identity_preflight(
        live_database=database.path,
        live_database_wal=Path(f"{database.path}-wal"),
        live_database_shm=Path(f"{database.path}-shm"),
        backup_destination=destination_candidate,
        backup_manifest=_manifest_path(destination_candidate),
        backup_temporary=temporary_candidate,
        backup_temporary_manifest=_manifest_path(temporary_candidate),
    )
    destination_path = paths["backup_destination"]
    manifest_path = paths["backup_manifest"]
    temporary = paths["backup_temporary"]
    temporary_manifest = paths["backup_temporary_manifest"]
    if temporary.exists() or temporary_manifest.exists():
        raise SQLiteBackupError("backup_temporary_exists")
    if (destination_path.exists() or manifest_path.exists()) and not overwrite:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LocalSQLite.exclusive_lifecycle(destination_path):
            _require_exclusive(destination_path)
            with database.read() as source:
                source_high_water = high_water_reader(source)
                witnesses = _witnesses(source) if store_name == "canonical" else []
                schema_version = int(source.execute("PRAGMA user_version").fetchone()[0])
                target = sqlite3.connect(temporary)
                target.row_factory = sqlite3.Row
                try:
                    apply_private_file_security(temporary)
                    source.backup(target)
                    target.commit()
                finally:
                    target.close()
            apply_private_file_security(temporary)
            verification = sqlite3.connect(temporary)
            verification.row_factory = sqlite3.Row
            try:
                _verify_database(verification, store_name)
                copied_high_water = high_water_reader(verification)
                copied_witnesses = (
                    _witnesses(verification) if store_name == "canonical" else []
                )
                copied_schema = int(verification.execute("PRAGMA user_version").fetchone()[0])
                if copied_high_water != source_high_water or copied_witnesses != witnesses:
                    raise SQLiteBackupError("backup row identity verification failed")
                if copied_schema != schema_version:
                    raise SQLiteBackupError("backup schema verification failed")
                created_at = datetime.now(timezone.utc)
                verification.execute(
                    """
                    CREATE TABLE __backup_metadata (
                        metadata_key TEXT PRIMARY KEY,
                        metadata_value TEXT NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                verification.executemany(
                    "INSERT INTO __backup_metadata VALUES (?, ?)",
                    (
                        ("store_name", store_name),
                        ("schema_version", str(schema_version)),
                        ("high_water_json", _json(source_high_water)),
                        ("canonical_witnesses_json", _json(witnesses)),
                        ("created_at", created_at.isoformat()),
                    ),
                )
                verification.commit()
                _verify_database(verification, store_name)
            finally:
                verification.close()
            sync_file(temporary)
            digest = file_sha256(temporary)
            manifest = BackupManifest(
                store_name=store_name,
                schema_version=schema_version,
                high_water=source_high_water,
                canonical_witnesses=witnesses,
                created_at=created_at,
                file_sha256=digest,
            )
            _write_external_manifest(temporary_manifest, manifest)
            os.replace(temporary, destination_path)
            os.replace(temporary_manifest, manifest_path)
            apply_private_file_security(destination_path)
            apply_private_file_security(manifest_path)
            sync_directory(destination_path.parent)
            return manifest
    except (
        PrivateFileSecurityError,
        SQLiteConfigurationError,
        OSError,
        sqlite3.Error,
    ) as error:
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        if isinstance(error, SQLiteBackupError):
            raise
        raise SQLiteBackupError("backup publication failed") from error
    except BaseException:
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        raise


def verify_backup(
    backup_path: str | Path, *, expected_store: str | None = None
) -> BackupManifest:
    try:
        path = _path_identity(backup_path).path
        manifest_path = _path_identity(_manifest_path(path)).path
        external = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = BackupManifest(
            store_name=str(external["store_name"]),
            schema_version=int(external["schema_version"]),
            high_water=dict(external["high_water"]),
            canonical_witnesses=list(external["canonical_witnesses"]),
            created_at=datetime.fromisoformat(external["created_at"]),
            file_sha256=str(external["file_sha256"]),
        )
        if set(external) != {
            "store_name",
            "schema_version",
            "high_water",
            "canonical_witnesses",
            "created_at",
            "file_sha256",
            "integrity_limitation",
        }:
            raise SQLiteBackupError("external backup manifest is invalid")
        if external["integrity_limitation"] != (
            "SHA-256 detects accidental or uncoordinated changes; it is not authenticity."
        ):
            raise SQLiteBackupError("backup integrity limitation is missing")
        if file_sha256(path) != manifest.file_sha256:
            raise SQLiteBackupError("external backup hash differs")
        if expected_store is not None and manifest.store_name != expected_store:
            raise SQLiteBackupError("backup store type differs")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            _verify_database(connection, manifest.store_name)
            metadata = {
                row["metadata_key"]: row["metadata_value"]
                for row in connection.execute(
                    "SELECT metadata_key, metadata_value FROM __backup_metadata"
                )
            }
            if set(metadata) != {
                "store_name",
                "schema_version",
                "high_water_json",
                "canonical_witnesses_json",
                "created_at",
            }:
                raise SQLiteBackupError("internal backup metadata is invalid")
            reader = _reader_for_store(manifest.store_name)
            recomputed_high_water = reader(connection)
            recomputed_witnesses = (
                _witnesses(connection) if manifest.store_name == "canonical" else []
            )
            if (
                metadata["store_name"] != manifest.store_name
                or int(metadata["schema_version"]) != manifest.schema_version
                or json.loads(metadata["high_water_json"]) != manifest.high_water
                or json.loads(metadata["canonical_witnesses_json"])
                != manifest.canonical_witnesses
                or datetime.fromisoformat(metadata["created_at"])
                != manifest.created_at
                or recomputed_high_water != manifest.high_water
                or recomputed_witnesses != manifest.canonical_witnesses
            ):
                raise SQLiteBackupError("backup manifest does not match rows")
            return manifest
        finally:
            connection.close()
    except SQLiteBackupError:
        raise
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SQLiteBackupError("backup verification failed") from error


def restore_backup(
    backup_path: str | Path,
    destination: str | Path,
    *,
    expected_store: str,
    overwrite: bool = False,
    discard_projections_path: str | Path | None = None,
) -> BackupManifest:
    source_candidate = _path_identity(backup_path).path
    destination_candidate = _path_identity(destination).path
    temporary_candidate = destination_candidate.with_name(
        f".{destination_candidate.name}.{uuid4().hex}.restore.tmp"
    )
    roles: dict[str, str | Path] = {
        "restore_source": source_candidate,
        "restore_source_manifest": _manifest_path(source_candidate),
        "restore_destination": destination_candidate,
        "restore_destination_wal": Path(f"{destination_candidate}-wal"),
        "restore_destination_shm": Path(f"{destination_candidate}-shm"),
        "restore_temporary": temporary_candidate,
    }
    if expected_store == "canonical" and discard_projections_path is not None:
        discard_candidate = _path_identity(discard_projections_path).path
        roles.update(
            {
                "projection_discard": discard_candidate,
                "projection_discard_wal": Path(f"{discard_candidate}-wal"),
                "projection_discard_shm": Path(f"{discard_candidate}-shm"),
            }
        )
    paths = _identity_preflight(**roles)
    source_path = paths["restore_source"]
    destination_path = paths["restore_destination"]
    temporary = paths["restore_temporary"]
    projection_target = paths.get("projection_discard")
    manifest = verify_backup(source_path, expected_store=expected_store)
    if destination_path.exists() and not overwrite:
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if projection_target is not None:
        projection_target.parent.mkdir(parents=True, exist_ok=True)
    targets = [destination_path]
    if projection_target is not None:
        targets.append(projection_target)
    try:
        with _exclusive_targets(targets):
            if destination_path.exists() and not overwrite:
                raise FileExistsError(destination_path)
            _stage_verified_file_unlocked(
                source_path, temporary, manifest
            )
            if projection_target is not None:
                _discard_projection_file_unlocked(projection_target)
            _publish_staged_file(temporary, destination_path)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup_pair(
    canonical_backup: str | Path,
    canonical_destination: str | Path,
    *,
    projections_backup: str | Path | None = None,
    projections_destination: str | Path | None = None,
    overwrite: bool = True,
) -> tuple[BackupManifest, BackupManifest | None]:
    if projections_backup is not None and projections_destination is None:
        raise SQLiteBackupError("projection_restore_destination_required")
    canonical_source = _path_identity(canonical_backup).path
    canonical_target_candidate = _path_identity(canonical_destination).path
    canonical_temporary_candidate = canonical_target_candidate.with_name(
        f".{canonical_target_candidate.name}.{uuid4().hex}.restore.tmp"
    )
    roles: dict[str, str | Path] = {
        "canonical_source": canonical_source,
        "canonical_source_manifest": _manifest_path(canonical_source),
        "canonical_destination": canonical_target_candidate,
        "canonical_destination_wal": Path(f"{canonical_target_candidate}-wal"),
        "canonical_destination_shm": Path(f"{canonical_target_candidate}-shm"),
        "canonical_temporary": canonical_temporary_candidate,
    }
    if projections_backup is not None:
        projection_source_candidate = _path_identity(projections_backup).path
        roles.update(
            {
                "projection_source": projection_source_candidate,
                "projection_source_manifest": _manifest_path(
                    projection_source_candidate
                ),
            }
        )
    if projections_destination is not None:
        projection_target_candidate = _path_identity(projections_destination).path
        projection_temporary_candidate = projection_target_candidate.with_name(
            f".{projection_target_candidate.name}.{uuid4().hex}.restore.tmp"
        )
        roles.update(
            {
                "projection_destination": projection_target_candidate,
                "projection_destination_wal": Path(
                    f"{projection_target_candidate}-wal"
                ),
                "projection_destination_shm": Path(
                    f"{projection_target_candidate}-shm"
                ),
                "projection_temporary": projection_temporary_candidate,
            }
        )
    paths = _identity_preflight(**roles)
    canonical_target = paths["canonical_destination"]
    canonical_temporary = paths["canonical_temporary"]
    projection_source = paths.get("projection_source")
    projection_target = paths.get("projection_destination")
    projection_temporary = paths.get("projection_temporary")
    canonical = verify_backup(paths["canonical_source"], expected_store="canonical")
    projection = (
        None
        if projection_source is None
        else verify_backup(projection_source, expected_store="projections")
    )
    if canonical_target.exists() and not overwrite:
        raise FileExistsError(canonical_target)
    if projection_target is not None and projection_target.exists() and not overwrite:
        raise FileExistsError(projection_target)
    canonical_target.parent.mkdir(parents=True, exist_ok=True)
    if projection_target is not None:
        projection_target.parent.mkdir(parents=True, exist_ok=True)
    compatible = projection is not None and _paired_witnesses_match(
        canonical, projection
    )
    targets = [canonical_target]
    if projection_target is not None:
        targets.append(projection_target)
    try:
        with _exclusive_targets(targets):
            if canonical_target.exists() and not overwrite:
                raise FileExistsError(canonical_target)
            if (
                projection_target is not None
                and projection_target.exists()
                and not overwrite
            ):
                raise FileExistsError(projection_target)
            _stage_verified_file_unlocked(
                paths["canonical_source"], canonical_temporary, canonical
            )
            if (
                projection_source is not None
                and projection_temporary is not None
                and projection is not None
            ):
                _stage_verified_file_unlocked(
                    projection_source, projection_temporary, projection
                )
            # Remove the old disposable side before publishing the authority.
            # Any later failure therefore leaves a valid canonical DB and no
            # potentially mismatched projection DB.
            if projection_target is not None:
                _discard_projection_file_unlocked(projection_target)
            _publish_staged_file(canonical_temporary, canonical_target)
            if (
                compatible
                and projection_target is not None
                and projection_temporary is not None
            ):
                try:
                    _publish_staged_file(
                        projection_temporary, projection_target
                    )
                except BaseException:
                    _discard_projection_file_unlocked(projection_target)
                    raise
        return canonical, projection if compatible else None
    finally:
        canonical_temporary.unlink(missing_ok=True)
        if projection_temporary is not None:
            projection_temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_targets(paths: list[Path]):
    ordered = sorted(set(paths), key=lambda item: os.path.normcase(str(item)))
    try:
        with ExitStack() as stack:
            for path in ordered:
                stack.enter_context(LocalSQLite.exclusive_lifecycle(path))
            for path in ordered:
                _require_exclusive(path)
            yield
    except SQLiteConfigurationError as error:
        raise SQLiteBackupError("restore target is in use") from error


def _stage_verified_file_unlocked(
    backup_path: str | Path,
    temporary: Path,
    manifest: BackupManifest,
) -> None:
    if temporary.exists():
        raise SQLiteBackupError("restore_temporary_exists")
    source_path = _path_identity(backup_path).path
    try:
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        target.row_factory = sqlite3.Row
        try:
            apply_private_file_security(temporary)
            source.backup(target)
            target.execute("DROP TABLE __backup_metadata")
            target.commit()
            _verify_database(target, manifest.store_name)
            if _reader_for_store(manifest.store_name)(target) != manifest.high_water:
                raise SQLiteBackupError("restored_rows_differ")
        finally:
            target.close()
            source.close()
        apply_private_file_security(temporary)
        sync_file(temporary)
    except SQLiteBackupError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error, PrivateFileSecurityError) as error:
        temporary.unlink(missing_ok=True)
        raise SQLiteBackupError("restore_staging_failed") from error


def _publish_staged_file(temporary: Path, destination: Path) -> None:
    try:
        os.replace(temporary, destination)
        apply_private_file_security(destination)
        sync_file(destination)
        sync_directory(destination.parent)
    except (OSError, PrivateFileSecurityError) as error:
        raise SQLiteBackupError("restore_publication_failed") from error


def _require_exclusive(path: Path) -> None:
    if LocalSQLite.open_connection_count(path):
        raise SQLiteBackupError("restore target has open application connections")
    if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise SQLiteBackupError("restore target has SQLite sidecars")


def _discard_projection_file_unlocked(target: Path) -> None:
    for candidate in (
        target,
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
    ):
        candidate.unlink(missing_ok=True)
    sync_directory(target.parent)


def _verify_database(connection: sqlite3.Connection, store_name: str) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise SQLiteBackupError("backup integrity verification failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SQLiteBackupError("backup foreign-key verification failed")
    directory = (
        Path(__file__).parents[1] / "analytics" / "sql"
        if store_name == "projections"
        else None
    )
    catalog = load_migration_catalog(directory)
    ledger = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    if len(ledger) != len(catalog):
        raise SQLiteBackupError("backup migration catalog differs")
    for row, expected in zip(ledger, catalog, strict=True):
        if (
            int(row[0]) != expected.version
            or row[1] != expected.name
            or row[2] != expected.checksum
        ):
            raise SQLiteBackupError("backup migration checksum differs")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != len(catalog):
        raise SQLiteBackupError("backup migration version differs")
    if store_name == "projections":
        try:
            for row in connection.execute(
                "SELECT * FROM projection_generations ORDER BY generation_id"
            ):
                values = recompute_generation(connection, row["generation_id"])
                projection = values["projection"]
                if (
                    projection.account_ref != row["creator_account_id"]
                    or projection.source_revision != int(row["canonical_revision"])
                    or projection.pipeline_revision != row["pipeline_revision"]
                    or projection.pipeline_config_digest
                    != row["pipeline_config_digest"]
                    or pipeline_identity_digest(projection)
                    != row["pipeline_identity_digest"]
                ):
                    raise ProjectionValidationError(
                        "backup generation identity metadata differs"
                    )
                if row["status"] != "building" and (
                    values["projection_digest"] != row["projection_digest"]
                    or values["graph_digest"] != row["graph_digest"]
                    or values["node_count"] != int(row["node_count"])
                    or values["edge_count"] != int(row["edge_count"])
                ):
                    raise ProjectionValidationError(
                        "backup generation metadata differs from rows"
                    )
            for row in connection.execute(
                """
                SELECT m.*, g.activation_intent_id AS generation_intent_id,
                       g.witness_sequence AS generation_witness_sequence,
                       g.publication_epoch AS generation_publication_epoch
                FROM graph_algorithm_metrics AS m
                JOIN projection_generations AS g
                  ON g.generation_id=m.generation_id
                 AND g.creator_account_id=m.creator_account_id
                ORDER BY m.generation_id, m.creator_account_id, m.metric_kind,
                         m.algorithm, m.parameter_hash
                """
            ):
                model = (
                    GraphCentralityResult
                    if row["metric_kind"] == "centrality"
                    else GraphCommunityResult
                ).model_validate_json(row["result_json"])
                if (
                    model.algorithm != row["algorithm"]
                    or model.parameter_hash != row["parameter_hash"]
                    or row["activation_intent_id"] != row["generation_intent_id"]
                    or row["witness_sequence"]
                    != row["generation_witness_sequence"]
                    or row["publication_epoch"]
                    != row["generation_publication_epoch"]
                ):
                    raise ProjectionValidationError(
                        "backup algorithm metric identity differs"
                    )
        except (
            ProjectionValidationError,
            GraphReferentialIntegrityError,
            ValueError,
        ) as error:
            raise SQLiteBackupError("backup projection rows do not match digests") from error


def _canonical_high_water(connection: sqlite3.Connection) -> dict[str, Any]:
    accounts: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT creator_account_id FROM account_heads ORDER BY creator_account_id"
    ):
        identity = _sqlite_identity(connection, row[0])
        if identity is None:
            raise SQLiteBackupError("canonical identity is missing")
        accounts[row[0]] = {
            "revision": identity.revision,
            "content_digest": identity.content_digest,
        }
    checkpoints = [
        [row[0], row[1], row[2], int(row[3])]
        for row in connection.execute(
            """
            SELECT creator_account_id, agent_installation_id, agent_stream_id,
                   committed_source_seq
            FROM ingest_checkpoints
            ORDER BY creator_account_id, agent_installation_id, agent_stream_id
            """
        )
    ]
    return {"account_identities": accounts, "stream_checkpoints": checkpoints}


def _projections_high_water(connection: sqlite3.Connection) -> dict[str, Any]:
    # The publication-epoch scheduler/capability bookkeeping is authoritative
    # in the canonical database (see SQLiteProjectionActivationRepository,
    # which is bound to the canonical plane). A projections-only backup
    # connection cannot reach it and does not need to: each store is
    # verified against its own embedded high-water manifest, and every
    # field read here already lives on projection_generations itself.
    active: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT * FROM projection_generations
        WHERE status='active' ORDER BY creator_account_id
        """
    ):
        values = recompute_generation(connection, row["generation_id"])
        active.append(
            {
                "account_ref": row["creator_account_id"],
                "canonical_revision": int(row["canonical_revision"]),
                "canonical_content_digest": row["canonical_content_digest"],
                "generation_id": row["generation_id"],
                "intent_id": row["activation_intent_id"],
                "witness_sequence": row["witness_sequence"],
                "projection_digest": values["projection_digest"],
                "graph_digest": values["graph_digest"],
                "pipeline_revision": row["pipeline_revision"],
                "pipeline_config_digest": row["pipeline_config_digest"],
                "pipeline_identity_digest": row["pipeline_identity_digest"],
                "expected_previous_generation_id": row[
                    "expected_active_generation_id"
                ],
                "expected_previous_revision": (
                    None
                    if row["expected_active_revision"] is None
                    else int(row["expected_active_revision"])
                ),
                "publication_epoch": row["publication_epoch"],
                "writer_owner_id": row["owner_id"],
                "writer_owner_pid": int(row["owner_pid"]),
                "writer_process_started_at": row["owner_process_started_at"],
                "writer_instance_nonce": row["owner_instance_nonce"],
                "writer_capability_digest": row["owner_capability_digest"],
            }
        )
    return {
        "active_generations": active,
        "algorithm_metrics_digest": _algorithm_metrics_digest(connection),
    }


def _algorithm_metrics_digest(connection: sqlite3.Connection) -> str:
    records = [
        {
            "generation_id": row["generation_id"],
            "account_ref": row["creator_account_id"],
            "metric_kind": row["metric_kind"],
            "algorithm": row["algorithm"],
            "parameter_hash": row["parameter_hash"],
            "result": json.loads(row["result_json"]),
            "computed_at": row["computed_at"],
            "activation_intent_id": row["activation_intent_id"],
            "witness_sequence": row["witness_sequence"],
            "publication_epoch": row["publication_epoch"],
        }
        for row in connection.execute(
            """
            SELECT * FROM graph_algorithm_metrics
            ORDER BY generation_id, creator_account_id, metric_kind,
                     algorithm, parameter_hash
            """
        )
    ]
    return "sha256:" + hashlib.sha256(_json(records).encode("utf-8")).hexdigest()


def _witnesses(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "analytics_projection_activation_intents" not in tables:
        return []
    return [
        {
            "intent_id": row["intent_id"],
            "creator_account_id": row["creator_account_id"],
            "account_ref": row["account_ref"],
            "generation_id": row["generation_id"],
            "canonical_revision": int(row["canonical_revision"]),
            "canonical_content_digest": row["canonical_content_digest"],
            "projection_digest": row["projection_digest"],
            "graph_digest": row["graph_digest"],
            "pipeline_revision": row["pipeline_revision"],
            "pipeline_config_digest": row["pipeline_config_digest"],
            "pipeline_identity_digest": row["pipeline_identity_digest"],
            "expected_previous_generation_id": row[
                "expected_previous_generation_id"
            ],
            "expected_previous_revision": (
                None
                if row["expected_previous_revision"] is None
                else int(row["expected_previous_revision"])
            ),
            "publication_epoch": row["publication_epoch"],
            "publication_scheduler_owner_id": row[
                "publication_scheduler_owner_id"
            ],
            "publication_epoch_state": row["publication_epoch_state"],
            "writer_owner_id": row["writer_owner_id"],
            "writer_owner_pid": int(row["writer_owner_pid"]),
            "writer_process_started_at": row["writer_process_started_at"],
            "writer_instance_nonce": row["writer_instance_nonce"],
            "writer_capability_digest": row["writer_capability_digest"],
            "publication_capability_digest": row[
                "publication_capability_digest"
            ],
            "witness_sequence": int(row["witness_sequence"]),
            "state": row["state"],
        }
        for row in connection.execute(
            """
            SELECT intent.*, epoch.scheduler_owner_id
                AS publication_scheduler_owner_id,
                   epoch.state AS publication_epoch_state
            FROM analytics_projection_activation_intents AS intent
            JOIN analytics_projection_publication_epochs AS epoch
              ON epoch.publication_epoch=intent.publication_epoch
             AND epoch.scheduler_capability_digest=
                 intent.publication_capability_digest
            WHERE intent.state='completed'
            ORDER BY intent.creator_account_id, intent.witness_sequence
            """
        )
    ]


def _paired_witnesses_match(
    canonical: BackupManifest, projections: BackupManifest
) -> bool:
    # The publication-epoch scheduler/capability identity is only readable
    # from the canonical connection (see _projections_high_water); a paired
    # match therefore compares every field a projections-only backup can
    # see, which is every field below.
    completed = {
        (
            item["account_ref"],
            item["generation_id"],
            item["intent_id"],
            item["canonical_revision"],
            item["canonical_content_digest"],
            item["projection_digest"],
            item["graph_digest"],
            item["pipeline_revision"],
            item["pipeline_config_digest"],
            item["pipeline_identity_digest"],
            item["expected_previous_generation_id"],
            item["expected_previous_revision"],
            item["publication_epoch"],
            item["witness_sequence"],
            item["writer_owner_id"],
            item["writer_owner_pid"],
            item["writer_process_started_at"],
            item["writer_instance_nonce"],
            item["writer_capability_digest"],
        )
        for item in canonical.canonical_witnesses
        if item["state"] == "completed"
    }
    accounts = canonical.high_water.get("account_identities", {})
    account_by_ref = {
        item["account_ref"]: item["creator_account_id"]
        for item in canonical.canonical_witnesses
    }
    for item in projections.high_water.get("active_generations", []):
        identity = accounts.get(account_by_ref.get(item["account_ref"], ""))
        if identity != {
            "revision": item["canonical_revision"],
            "content_digest": item["canonical_content_digest"],
        }:
            return False
        key = (
            item["account_ref"],
            item["generation_id"],
            item["intent_id"],
            item["canonical_revision"],
            item["canonical_content_digest"],
            item["projection_digest"],
            item["graph_digest"],
            item["pipeline_revision"],
            item["pipeline_config_digest"],
            item["pipeline_identity_digest"],
            item["expected_previous_generation_id"],
            item["expected_previous_revision"],
            item["publication_epoch"],
            item["witness_sequence"],
            item["writer_owner_id"],
            item["writer_owner_pid"],
            item["writer_process_started_at"],
            item["writer_instance_nonce"],
            item["writer_capability_digest"],
        )
        if key not in completed:
            return False
    return True


def _reader_for_store(store_name: str) -> HighWaterReader:
    if store_name == "canonical":
        return _canonical_high_water
    if store_name == "projections":
        return _projections_high_water
    raise SQLiteBackupError("backup store type is unsupported")


def _manifest_path(path: Path) -> Path:
    return path.with_name(path.name + ".manifest.json")


def _write_external_manifest(path: Path, manifest: BackupManifest) -> None:
    payload = {
        "store_name": manifest.store_name,
        "schema_version": manifest.schema_version,
        "high_water": manifest.high_water,
        "canonical_witnesses": manifest.canonical_witnesses,
        "created_at": manifest.created_at.isoformat(),
        "file_sha256": manifest.file_sha256,
        "integrity_limitation": (
            "SHA-256 detects accidental or uncoordinated changes; it is not authenticity."
        ),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        apply_private_file_security(path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    apply_private_file_security(path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
