#!/usr/bin/env python3
"""Execute RET-001 companion observations without converting findings into assertions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_PATH = Path(__file__).with_name("run-ret-001-companion-observations.py")
_SPEC = importlib.util.spec_from_file_location("ret001_companion_observation_library", LIBRARY_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load RET-001 observation library {LIBRARY_PATH}")
lib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lib)

from app.persistence.migrations import MigrationRunner, load_migration_catalog  # noqa: E402


def run_projection_rebuild(root: Path, product_revision: str) -> dict:
    scenario_root = root / "res-003"
    scenario_root.mkdir(parents=True, exist_ok=True)
    canonical_path = scenario_root / "canonical.sqlite3"
    projection_path = scenario_root / "projections.sqlite3"
    repositories = lib.create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    account_id, fixture_message = lib.seed_canonical_snapshot(repositories.history)
    repositories.projection.catch_up(account_id)
    before = lib.projection_message(
        repositories.projection_database,
        account_id,
        fixture_message["message_id"],
    )
    if before is None:
        raise RuntimeError("RET-001 RES-003 precondition failed: initial projection row absent")

    lib.delete_projection_files(projection_path)
    if projection_path.exists():
        raise RuntimeError("RET-001 RES-003 precondition failed: projection file still exists")

    recovered = lib.create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    recovered.projection.catch_up(account_id)
    after = lib.projection_message(
        recovered.projection_database,
        account_id,
        fixture_message["message_id"],
    )
    canonical_survived = lib.canonical_message(
        recovered.database,
        account_id,
        fixture_message["message_id"],
    ) is not None

    observed = after is not None
    if observed and after["text_sha256"] != before["text_sha256"]:
        raise RuntimeError("RET-001 RES-003 rebuilt projection text hash differs from source observation")

    return lib.result_document(
        "RES-003-PROJECTION-REBUILD",
        product_revision,
        "OBSERVED_REENTRY" if observed else "NO_REENTRY_OBSERVED",
        [
            {
                "relationship_requirement_id": "RES-003-R1",
                "outcome": "OBSERVED" if observed else "NOT_OBSERVED",
                "evidence_ids": ["EXEC-RES-003-PROJECTION-REBUILD"],
                "facts": {
                    "deleted_store": "projections.sqlite3",
                    "canonical_message_survived": canonical_survived,
                    "ordinary_catch_up_attempted_after_store_recreation": True,
                    "projection_message_before": before,
                    "projection_message_after_recovery_attempt": after,
                    "projection_recreated": observed,
                },
            }
        ],
        notes=[
            "This scenario tests the ordinary repository recreation plus catch_up path after deleting the disposable Bridge projection store.",
            "A NOT_OBSERVED outcome means this path did not demonstrate the registry-declared rebuild relationship; it does not prove that no distinct recovery path exists.",
            "No production deletion or retention control was added or invoked.",
        ],
    )


def _message_from_detached(connection, account_id: str, message_id: str) -> dict | None:
    row = connection.execute(
        """
        SELECT message_id, chat_id, text, sent_at, direction
          FROM account_messages
         WHERE creator_account_id=? AND message_id=?
        """,
        (account_id, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "message_id": str(row[0]),
        "chat_id": str(row[1]),
        "text_sha256": lib.sha256_text(str(row[2])),
        "sent_at": str(row[3]),
        "direction": str(row[4]),
    }


def run_migration_backup_restore(root: Path, product_revision: str) -> dict:
    scenario_root = root / "res-004"
    scenario_root.mkdir(parents=True, exist_ok=True)
    canonical_path = scenario_root / "canonical.sqlite3"
    projection_path = scenario_root / "projections.sqlite3"
    migrations_dir = scenario_root / "migrations"
    backups_dir = scenario_root / "migration-backups"
    repositories = lib.create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    account_id, fixture_message = lib.seed_canonical_snapshot(repositories.history)
    before = lib.canonical_message(
        repositories.database,
        account_id,
        fixture_message["message_id"],
    )
    if before is None:
        raise RuntimeError("RET-001 RES-004 precondition failed: canonical message absent")

    migrations_dir.mkdir(parents=True, exist_ok=True)
    source_migrations = ROOT / "app" / "persistence" / "sql"
    catalog = load_migration_catalog(source_migrations)
    for source in sorted(source_migrations.glob("*.sql")):
        shutil.copy2(source, migrations_dir / source.name)
    next_version = catalog[-1].version + 1
    characterization_migration = migrations_dir / (
        f"{next_version:04d}_ret001_characterization_noop.sql"
    )
    characterization_migration.write_text(
        "CREATE TABLE IF NOT EXISTS ret001_characterization_noop (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )

    runner = MigrationRunner(
        repositories.database,
        migrations_dir=migrations_dir,
        backups_dir=backups_dir,
        lock_path=scenario_root / ".ret001-migration.lock",
    )
    completed = runner.run()
    backup_path = runner.last_backup_path
    if completed != [next_version] or backup_path is None or not backup_path.exists():
        raise RuntimeError("RET-001 RES-004 precondition failed: migration backup was not created")

    detached = repositories.database.open_detached(backup_path)
    try:
        backup_message = _message_from_detached(
            detached,
            account_id,
            fixture_message["message_id"],
        )
    finally:
        detached.close()
    if backup_message is None:
        raise RuntimeError("RET-001 RES-004 migration backup did not preserve the message")
    if backup_message["text_sha256"] != before["text_sha256"]:
        raise RuntimeError("RET-001 RES-004 migration backup text hash differs")

    lib.delete_canonical_message(
        repositories.database,
        account_id,
        fixture_message["message_id"],
    )
    if lib.canonical_message(
        repositories.database,
        account_id,
        fixture_message["message_id"],
    ) is not None:
        raise RuntimeError("RET-001 RES-004 test-only canonical deletion failed")

    restore_error = None
    restored_via_existing_path = False
    try:
        lib.restore_backup(
            backup_path,
            canonical_path,
            expected_store="canonical",
            overwrite=True,
            discard_projections_path=projection_path,
        )
        reopened = lib.create_canonical_repositories(
            "sqlite",
            canonical_path=canonical_path,
            projection_path=projection_path,
        )
        restored_via_existing_path = lib.canonical_message(
            reopened.database,
            account_id,
            fixture_message["message_id"],
        ) is not None
    except Exception as error:  # observational: preserve the existing restore-path rejection
        restore_error = {
            "type": type(error).__name__,
            "message": str(error),
        }

    document = lib.result_document(
        "RES-004-MIGRATION-BACKUP-RESTORE",
        product_revision,
        "OBSERVED_REENTRY" if restored_via_existing_path else "NO_REENTRY_OBSERVED",
        [
            {
                "relationship_requirement_id": "RES-004-R1",
                "outcome": "OBSERVED",
                "evidence_ids": ["EXEC-RES-004-MIGRATION-BACKUP-COPY"],
                "facts": {
                    "migration_completed_version": next_version,
                    "migration_backup_created": True,
                    "migration_backup_filename": backup_path.name,
                    "message_before_test_only_delete": before,
                    "message_in_migration_backup": backup_message,
                    "backup_preserved_same_text_sha256": backup_message["text_sha256"]
                    == before["text_sha256"],
                },
            },
            {
                "relationship_requirement_id": "RES-004-R2",
                "outcome": "OBSERVED" if restored_via_existing_path else "NOT_OBSERVED",
                "evidence_ids": ["EXEC-RES-004-EXISTING-RESTORE-PATH"],
                "facts": {
                    "existing_restore_backup_attempted": True,
                    "restored_via_existing_restore_path": restored_via_existing_path,
                    "restore_error": restore_error,
                },
            },
        ],
        notes=[
            "A test-only next migration is added only to the temporary migration catalog to trigger the existing pre-migration backup path; Product migrations are not modified.",
            "The migration backup itself is read using the database engine to establish retained content. The scenario separately tests whether the existing ordinary backup restore API accepts that artifact.",
            "Direct SQL deletion remains confined to the isolated characterization database and is not a production deletion API.",
        ],
    )
    document["characterization_outcome"] = (
        "MIGRATION_BACKUP_RESTORED_BY_EXISTING_PATH"
        if restored_via_existing_path
        else "MIGRATION_BACKUP_PRESERVES_INFORMATION_WITHOUT_OBSERVED_EXISTING_RESTORE_PATH"
    )
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.product_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.product_revision
    ):
        raise SystemExit("--product-revision must be a lowercase 40-hex commit SHA")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="ret-001-companion-observations-") as temporary:
        root = Path(temporary)
        results = [
            run_projection_rebuild(root, args.product_revision),
            run_migration_backup_restore(root, args.product_revision),
            lib.run_backup_restore(root, args.product_revision),
            lib.run_derived_survival(root, args.product_revision),
        ]

    for result in results:
        destination = args.output_dir / f"{result['scenario_id'].lower()}.json"
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    summary = {
        "schema": "ofca-ret-001-companion-observation-run/v1",
        "product_revision": args.product_revision,
        "runner": "ret-001-companion-observations/v1",
        "executed_scenario_ids": sorted(result["scenario_id"] for result in results),
        "result_statuses": {
            result["scenario_id"]: result["result_status"] for result in results
        },
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
