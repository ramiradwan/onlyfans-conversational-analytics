"""Barrier-aware restore for authoritative canonical and disposable analytics persistence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.analytics.opaque_refs import (
    account_ref as analytics_account_ref,
    message_ref as analytics_message_ref,
)
from app.analytics.projection_store import CLEAR_PIPELINE_REVISION
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.retention_store import BOUNDED_PIPELINE_PREFIX
from app.models.analytics import AnalyticsProjection
from app.persistence import sqlite_api as sqlite3
from app.persistence.backup import (
    BackupManifest,
    SQLiteBackupError,
    _canonical_high_water,
    _destination_database,
    _discard_projection_file_unlocked,
    _exclusive_targets,
    _identity_preflight,
    _key_path,
    _manifest_path,
    _paired_witnesses_match,
    _path_identity,
    _publish_staged_file,
    _stage_verified_file_unlocked,
    _witnesses,
    verify_backup,
)
from app.persistence.database import CanonicalSQLite
from app.persistence.managed_recovery import (
    managed_recovery_is_current,
    prune_managed_recovery_files,
)
from app.persistence.migrations import MigrationRunner, load_migration_catalog
from app.persistence.private_files import apply_private_file_security, sync_file


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("restore_reconciliation_time_timezone_required")
    return value.astimezone(timezone.utc)


def _current_authority(database: CanonicalSQLite) -> tuple[list[dict], list[dict]]:
    try:
        with database.read() as connection:
            return (
                [dict(row) for row in connection.execute("SELECT * FROM archive_policies")],
                [dict(row) for row in connection.execute("SELECT * FROM deletion_barriers")],
            )
    except sqlite3.Error as error:
        raise SQLiteBackupError("current_retention_authority_unavailable") from error


def _merge_authority(
    connection: sqlite3.Connection,
    policies: list[dict],
    barriers: list[dict],
) -> None:
    for row in policies:
        connection.execute(
            """
            INSERT INTO archive_policies(
                creator_account_id,vault_enabled,policy_type,finite_horizon_days,
                activated_at,creator_action_ref,policy_revision,
                indefinite_gate_state,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(creator_account_id) DO UPDATE SET
                vault_enabled=excluded.vault_enabled,
                policy_type=excluded.policy_type,
                finite_horizon_days=excluded.finite_horizon_days,
                activated_at=excluded.activated_at,
                creator_action_ref=excluded.creator_action_ref,
                policy_revision=excluded.policy_revision,
                indefinite_gate_state=excluded.indefinite_gate_state,
                updated_at=excluded.updated_at
            """,
            tuple(
                row[key]
                for key in (
                    "creator_account_id",
                    "vault_enabled",
                    "policy_type",
                    "finite_horizon_days",
                    "activated_at",
                    "creator_action_ref",
                    "policy_revision",
                    "indefinite_gate_state",
                    "updated_at",
                )
            ),
        )
    for row in barriers:
        connection.execute(
            """
            INSERT INTO deletion_barriers(
                creator_account_id,scope_kind,scope_key,deletion_revision,
                deleted_at,provenance
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(creator_account_id,scope_kind,scope_key) DO UPDATE SET
                deletion_revision=CASE
                    WHEN excluded.deletion_revision>deletion_barriers.deletion_revision
                    THEN excluded.deletion_revision ELSE deletion_barriers.deletion_revision END,
                deleted_at=CASE
                    WHEN excluded.deletion_revision>=deletion_barriers.deletion_revision
                    THEN excluded.deleted_at ELSE deletion_barriers.deleted_at END,
                provenance=CASE
                    WHEN excluded.deletion_revision>=deletion_barriers.deletion_revision
                    THEN excluded.provenance ELSE deletion_barriers.provenance END
            """,
            tuple(
                row[key]
                for key in (
                    "creator_account_id",
                    "scope_kind",
                    "scope_key",
                    "deletion_revision",
                    "deleted_at",
                    "provenance",
                )
            ),
        )
    connection.execute(
        """
        DELETE FROM account_messages
        WHERE lifecycle_origin='ordinary' AND EXISTS (
            SELECT 1 FROM deletion_barriers b
            WHERE b.creator_account_id=account_messages.creator_account_id
              AND ((b.scope_kind='account' AND b.scope_key='*')
                OR (b.scope_kind='conversation' AND b.scope_key=account_messages.chat_id)
                OR (b.scope_kind='message' AND b.scope_key=account_messages.message_id)
                OR (b.scope_kind='participant' AND b.scope_key=account_messages.sender_platform_user_id))
        )
        """
    )
    connection.execute(
        """
        DELETE FROM account_messages
        WHERE lifecycle_origin='creator_import' AND EXISTS (
            SELECT 1 FROM deletion_barriers b
            WHERE b.creator_account_id=account_messages.creator_account_id
              AND b.deleted_at>=account_messages.lifecycle_started_at
              AND ((b.scope_kind='account' AND b.scope_key='*')
                OR (b.scope_kind='conversation' AND b.scope_key=account_messages.chat_id)
                OR (b.scope_kind='message' AND b.scope_key=account_messages.message_id)
                OR (b.scope_kind='participant' AND b.scope_key=account_messages.sender_platform_user_id))
        )
        """
    )
    connection.execute(
        """
        DELETE FROM account_chats
        WHERE lifecycle_origin='ordinary' AND EXISTS (
            SELECT 1 FROM deletion_barriers b
            WHERE b.creator_account_id=account_chats.creator_account_id
              AND ((b.scope_kind='account' AND b.scope_key='*')
                OR (b.scope_kind='conversation' AND b.scope_key=account_chats.chat_id))
        )
        """
    )
    connection.execute(
        """
        DELETE FROM account_chats
        WHERE lifecycle_origin='creator_import' AND EXISTS (
            SELECT 1 FROM deletion_barriers b
            WHERE b.creator_account_id=account_chats.creator_account_id
              AND b.deleted_at>=account_chats.lifecycle_started_at
              AND ((b.scope_kind='account' AND b.scope_key='*')
                OR (b.scope_kind='conversation' AND b.scope_key=account_chats.chat_id))
        )
        """
    )


def _reconciled_canonical_manifest(
    connection: sqlite3.Connection,
    source: BackupManifest,
) -> BackupManifest:
    return BackupManifest(
        "canonical",
        source.schema_version,
        _canonical_high_water(connection),
        _witnesses(connection),
        source.created_at,
        source.file_sha256,
    )


def _projection_retention_is_current(
    canonical: sqlite3.Connection,
    projection: sqlite3.Connection,
    *,
    now: datetime,
) -> bool:
    cutoff = _utc(now) - timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
    accounts = {
        analytics_account_ref(str(row["creator_account_id"])): str(row["creator_account_id"])
        for row in canonical.execute(
            "SELECT creator_account_id FROM account_heads ORDER BY creator_account_id"
        )
    }
    canonical_messages = {
        analytics_message_ref(
            str(row["creator_account_id"]),
            str(row["chat_id"]),
            str(row["message_id"]),
        )
        for row in canonical.execute(
            """
            SELECT creator_account_id,chat_id,message_id
            FROM account_messages WHERE is_deleted=0
            """
        )
    }
    for row in projection.execute(
        """
        SELECT g.creator_account_id,g.pipeline_revision,p.document_json
        FROM projection_generations g
        JOIN analytics_projections p
          ON p.generation_id=g.generation_id
         AND p.creator_account_id=g.creator_account_id
        WHERE g.status='active'
        """
    ):
        if str(row["creator_account_id"]) not in accounts:
            return False
        document = AnalyticsProjection.model_validate_json(row["document_json"])
        if document.pipeline_revision != row["pipeline_revision"]:
            return False
        if document.pipeline_revision == CLEAR_PIPELINE_REVISION:
            if document.message_enrichments:
                return False
            continue
        if not document.pipeline_revision.startswith(BOUNDED_PIPELINE_PREFIX):
            return False
        for item in document.message_enrichments:
            if _utc(item.sent_at) <= cutoff or item.message_ref not in canonical_messages:
                return False
    return True


def _queue_projection_reseed(
    connection: sqlite3.Connection,
    creator_account_ids: list[str],
    *,
    now: datetime,
) -> None:
    created_at = _utc(now).isoformat()
    for account_id in sorted(set(creator_account_ids)):
        row = connection.execute(
            "SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
            (account_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO projection_work(
                    creator_account_id,canonical_revision,work_kind,
                    conversation_id,created_at
                ) VALUES (?,?,'reseed',NULL,?)
                """,
                (account_id, int(row[0]), created_at),
            )


def _require_current_recovery(path: Path, *, now: datetime) -> None:
    if not managed_recovery_is_current(path, now=now):
        raise SQLiteBackupError("managed_recovery_expired_or_unclassified")


def restore_backup_pair_with_deletion_barriers(
    canonical_backup: str | Path,
    canonical_destination: str | Path,
    *,
    projections_backup: str | Path | None = None,
    projections_destination: str | Path | None = None,
    overwrite: bool = True,
    now: datetime | None = None,
) -> tuple[BackupManifest, BackupManifest | None]:
    if projections_backup is not None and projections_destination is None:
        raise SQLiteBackupError("projection_restore_destination_required")
    current_time = _utc(now or datetime.now(timezone.utc))
    canonical_source = _path_identity(canonical_backup).path
    canonical_target_candidate = _path_identity(canonical_destination).path
    canonical_temporary_candidate = canonical_target_candidate.with_name(
        f".{canonical_target_candidate.name}.{uuid4().hex}.barrier-restore.tmp"
    )
    roles: dict[str, str | Path] = {
        "canonical_source": canonical_source,
        "canonical_source_manifest": _manifest_path(canonical_source),
        "canonical_source_key": _key_path(canonical_source),
        "canonical_destination": canonical_target_candidate,
        "canonical_destination_wal": Path(f"{canonical_target_candidate}-wal"),
        "canonical_destination_shm": Path(f"{canonical_target_candidate}-shm"),
        "canonical_temporary": canonical_temporary_candidate,
    }
    if projections_backup is not None:
        source = _path_identity(projections_backup).path
        roles.update(
            {
                "projection_source": source,
                "projection_source_manifest": _manifest_path(source),
                "projection_source_key": _key_path(source),
            }
        )
    if projections_destination is not None:
        target = _path_identity(projections_destination).path
        temp = target.with_name(f".{target.name}.{uuid4().hex}.barrier-restore.tmp")
        roles.update(
            {
                "projection_destination": target,
                "projection_destination_wal": Path(f"{target}-wal"),
                "projection_destination_shm": Path(f"{target}-shm"),
                "projection_temporary": temp,
            }
        )
    paths = _identity_preflight(**roles)
    canonical_target = paths["canonical_destination"]
    canonical_temporary = paths["canonical_temporary"]
    projection_source = paths.get("projection_source")
    projection_target = paths.get("projection_destination")
    projection_temporary = paths.get("projection_temporary")
    canonical_manifest = verify_backup(paths["canonical_source"], expected_store="canonical")
    _require_current_recovery(paths["canonical_source"], now=current_time)
    projection_manifest = None
    if projection_source is not None:
        try:
            projection_manifest = verify_backup(
                projection_source, expected_store="projections"
            )
            _require_current_recovery(projection_source, now=current_time)
        except SQLiteBackupError:
            projection_manifest = None
    canonical_database = CanonicalSQLite(canonical_target)
    policies, barriers = (
        _current_authority(canonical_database)
        if canonical_target.exists()
        else ([], [])
    )
    if canonical_target.exists() and not overwrite:
        raise FileExistsError(canonical_target)
    if projection_target is not None and projection_target.exists() and not overwrite:
        raise FileExistsError(projection_target)
    canonical_target.parent.mkdir(parents=True, exist_ok=True)
    if projection_target is not None:
        projection_target.parent.mkdir(parents=True, exist_ok=True)
    projection_database = (
        None
        if projection_target is None
        else _destination_database("projections", projection_target)
    )
    targets = [canonical_target] + ([] if projection_target is None else [projection_target])
    compatible = False
    try:
        with _exclusive_targets(targets):
            _stage_verified_file_unlocked(
                paths["canonical_source"],
                canonical_temporary,
                canonical_manifest,
                canonical_database,
            )
            staged_canonical = canonical_database.open_detached(canonical_temporary)
            try:
                staged_canonical.execute("PRAGMA foreign_keys=ON")
                staged_canonical.execute("BEGIN IMMEDIATE")
                _merge_authority(staged_canonical, policies, barriers)
                if staged_canonical.execute("PRAGMA foreign_key_check").fetchall():
                    raise SQLiteBackupError(
                        "restore_barrier_reconciliation_foreign_key_failed"
                    )
                if staged_canonical.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SQLiteBackupError(
                        "restore_barrier_reconciliation_integrity_failed"
                    )
                staged_canonical.commit()
                reconciled_manifest = _reconciled_canonical_manifest(
                    staged_canonical, canonical_manifest
                )
                compatible = (
                    projection_manifest is not None
                    and projection_source is not None
                    and projection_target is not None
                    and projection_temporary is not None
                    and _paired_witnesses_match(reconciled_manifest, projection_manifest)
                )
                if compatible:
                    try:
                        _stage_verified_file_unlocked(
                            projection_source,
                            projection_temporary,
                            projection_manifest,
                            projection_database,
                        )
                        staged_projection = projection_database.open_detached(
                            projection_temporary
                        )
                        try:
                            compatible = _projection_retention_is_current(
                                staged_canonical,
                                staged_projection,
                                now=current_time,
                            )
                        finally:
                            staged_projection.close()
                    except SQLiteBackupError:
                        compatible = False
                        projection_temporary.unlink(missing_ok=True)
                if projection_target is not None and not compatible:
                    _queue_projection_reseed(
                        staged_canonical,
                        list(reconciled_manifest.high_water["account_identities"]),
                        now=current_time,
                    )
                    staged_canonical.commit()
            except BaseException:
                staged_canonical.rollback()
                raise
            finally:
                staged_canonical.close()
            if projection_target is not None:
                _discard_projection_file_unlocked(projection_target)
            _publish_staged_file(canonical_temporary, canonical_target)
            if compatible and projection_target is not None and projection_temporary is not None:
                try:
                    _publish_staged_file(projection_temporary, projection_target)
                except BaseException:
                    _discard_projection_file_unlocked(projection_target)
                    raise
            else:
                projection_manifest = None
        prune_managed_recovery_files(paths["canonical_source"].parent, now=current_time)
        if projection_source is not None and projection_source.parent != paths["canonical_source"].parent:
            prune_managed_recovery_files(projection_source.parent, now=current_time)
        return canonical_manifest, projection_manifest
    finally:
        canonical_temporary.unlink(missing_ok=True)
        if projection_temporary is not None:
            projection_temporary.unlink(missing_ok=True)


def restore_canonical_with_deletion_barriers(
    backup_path: str | Path,
    destination: str | Path,
    *,
    overwrite: bool = True,
    discard_projections_path: str | Path | None = None,
    now: datetime | None = None,
) -> BackupManifest:
    canonical, _ = restore_backup_pair_with_deletion_barriers(
        backup_path,
        destination,
        projections_destination=discard_projections_path,
        overwrite=overwrite,
        now=now,
    )
    return canonical


def restore_migration_backup_with_deletion_barriers(
    database: CanonicalSQLite,
    migration_backup: str | Path,
    *,
    discard_projections_path: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Restore a pre-migration snapshot without allowing stale data to bypass deletion."""
    current_time = _utc(now or datetime.now(timezone.utc))
    source_candidate = _path_identity(migration_backup).path
    destination_candidate = _path_identity(database.path).path
    temporary_candidate = destination_candidate.with_name(
        f".{destination_candidate.name}.{uuid4().hex}.migration-restore.tmp"
    )
    roles: dict[str, str | Path] = {
        "migration_source": source_candidate,
        "canonical_destination": destination_candidate,
        "canonical_destination_wal": Path(f"{destination_candidate}-wal"),
        "canonical_destination_shm": Path(f"{destination_candidate}-shm"),
        "canonical_temporary": temporary_candidate,
    }
    if discard_projections_path is not None:
        projection_candidate = _path_identity(discard_projections_path).path
        roles.update(
            {
                "projection_discard": projection_candidate,
                "projection_discard_wal": Path(f"{projection_candidate}-wal"),
                "projection_discard_shm": Path(f"{projection_candidate}-shm"),
            }
        )
    paths = _identity_preflight(**roles)
    source = paths["migration_source"]
    destination = paths["canonical_destination"]
    temporary = paths["canonical_temporary"]
    projection_target = paths.get("projection_discard")
    _require_current_recovery(source, now=current_time)
    policies, barriers = _current_authority(database)
    targets = [destination] + ([] if projection_target is None else [projection_target])
    catalog = load_migration_catalog()
    try:
        with _exclusive_targets(targets):
            source_connection = database.open_detached(source, read_only=True)
            staged = database.open_detached(temporary)
            try:
                apply_private_file_security(temporary)
                source_connection.backup(staged)
                MigrationRunner._ensure_ledger(staged)
                applied = MigrationRunner._validate_applied(staged, catalog)
                for migration in catalog:
                    if migration.version not in applied:
                        MigrationRunner._apply(staged, migration)
                MigrationRunner._validate_applied(staged, catalog)
                staged.execute("PRAGMA foreign_keys=ON")
                staged.execute("BEGIN IMMEDIATE")
                _merge_authority(staged, policies, barriers)
                if staged.execute("PRAGMA foreign_key_check").fetchall():
                    raise SQLiteBackupError(
                        "migration_restore_barrier_reconciliation_foreign_key_failed"
                    )
                if staged.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SQLiteBackupError(
                        "migration_restore_barrier_reconciliation_integrity_failed"
                    )
                staged.commit()
                MigrationRunner._validate_database(staged)
            except BaseException:
                if staged.in_transaction:
                    staged.rollback()
                raise
            finally:
                staged.close()
                source_connection.close()
            apply_private_file_security(temporary)
            sync_file(temporary)
            if projection_target is not None:
                _discard_projection_file_unlocked(projection_target)
            _publish_staged_file(temporary, destination)
        prune_managed_recovery_files(source.parent, now=current_time)
        return destination
    except SQLiteBackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise SQLiteBackupError("migration_restore_failed") from error
    finally:
        temporary.unlink(missing_ok=True)
