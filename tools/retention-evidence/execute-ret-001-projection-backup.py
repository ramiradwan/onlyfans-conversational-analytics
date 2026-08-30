#!/usr/bin/env python3
"""Execute RET-001 RES-010 against the production analytics projection backup/restore path."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ret001_execution import load_module, validate_product_revision, write_json

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_PATH = Path(__file__).with_name('run-ret-001-companion-observations.py')
BACKUP_TEST_HELPER = ROOT / 'tests' / 'test_sqlite_backup.py'

lib = load_module('ret001_companion_observation_library', LIBRARY_PATH)
backup_fixture = load_module('ret001_sqlite_backup_fixture', BACKUP_TEST_HELPER)
from app.persistence import backup as backup_module  # noqa: E402
from app.persistence.backup import backup_projections_database, restore_backup, verify_backup  # noqa: E402


def remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f'{path}-wal'), Path(f'{path}-shm')):
        candidate.unlink(missing_ok=True)


def run_projection_backup_restore(root: Path, product_revision: str) -> dict:
    runtime = asyncio.run(backup_fixture.seeded_runtime(root / 'source'))
    if runtime.stores.database is None:
        raise RuntimeError('RET-001 RES-010 precondition failed: analytics projection database unavailable')
    before = runtime.stores.projections.get_artifact(runtime.creator_account_id)
    if before is None:
        raise RuntimeError('RET-001 RES-010 precondition failed: active analytics projection absent')

    backup_path = root / 'analytics-projections.backup.sqlite3'
    manifest = backup_projections_database(runtime.stores.database, backup_path)
    if verify_backup(backup_path, expected_store='projections') != manifest:
        raise RuntimeError('RET-001 RES-010 analytics projection backup verification differs')
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()

    # Isolated characterization mutation only; this is not a production deletion API.
    remove_sqlite_files(runtime.projections_path)
    if runtime.projections_path.exists():
        raise RuntimeError('RET-001 RES-010 test-only analytics projection removal failed')

    staging = {'temporary_seen_before_publish': False, 'temporary_suffix': '.restore.tmp'}
    original_publish = backup_module._publish_staged_file

    def observing_publish(temporary, destination):
        staging['temporary_seen_before_publish'] = Path(temporary).exists()
        return original_publish(temporary, destination)

    backup_module._publish_staged_file = observing_publish
    try:
        restore_backup(
            backup_path,
            runtime.projections_path,
            expected_store='projections',
            overwrite=True,
        )
    finally:
        backup_module._publish_staged_file = original_publish

    after = runtime.stores.projections.get_artifact(runtime.creator_account_id)
    if after is None:
        raise RuntimeError('RET-001 RES-010 analytics projection was not restored')
    if after != before:
        raise RuntimeError('RET-001 RES-010 restored analytics projection differs from backup source')
    if staging['temporary_seen_before_publish'] is not True:
        raise RuntimeError('RET-001 RES-010 restore staging was not observed')

    return lib.result_document(
        'RES-010-PROJECTION-BACKUP-RESTORE',
        product_revision,
        'OBSERVED_REENTRY',
        [
            {
                'relationship_requirement_id': 'RES-010-R1',
                'outcome': 'OBSERVED',
                'evidence_ids': ['EXEC-RES-010-ANALYTICS-PROJECTION-BACKUP'],
                'facts': {
                    'backup_store_name': manifest.store_name,
                    'verified_backup_sha256': backup_sha256,
                    'backup_high_water': manifest.high_water,
                    'source_projection_present_before_removal': True,
                },
            },
            {
                'relationship_requirement_id': 'RES-010-R2',
                'outcome': 'OBSERVED',
                'evidence_ids': ['EXEC-RES-010-ANALYTICS-RESTORE-STAGING'],
                'facts': {**staging, 'restore_completed': True},
            },
            {
                'relationship_requirement_id': 'RES-010-R3',
                'outcome': 'OBSERVED',
                'evidence_ids': ['EXEC-RES-010-ANALYTICS-PROJECTION-RESTORED'],
                'facts': {
                    'restored_projection_present': True,
                    'restored_artifact_equal_to_backup_source': after == before,
                },
            },
        ],
        notes=[
            'The analytics projection file removal is confined to an isolated characterization directory and is not a production deletion API.',
            'This scenario executes the existing analytics projection backup and restore APIs independently of the canonical-backup scenario.',
            'The observation demonstrates re-entry of linkable analytics state from a surviving backup; it does not select a retention or deletion policy.',
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--product-revision', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    validate_product_revision(args.product_revision)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='ret-001-projection-backup-') as temporary:
        result = run_projection_backup_restore(Path(temporary), args.product_revision)
    destination = args.output_dir / 'res-010-projection-backup-restore.json'
    write_json(destination, result)
    print(json.dumps({
        'schema': 'ofca-ret-001-projection-backup-observation-run/v1',
        'product_revision': args.product_revision,
        'scenario_id': result['scenario_id'],
        'result_status': result['result_status'],
        'output': str(destination),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
