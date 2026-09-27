"""Versioned optional integrity metadata upgrades without changing the published graph."""
from pathlib import Path
import shutil
import pytest
from app.analytics.database import ProjectionsDatabase
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.analytics.validation_receipt import content_stamp
from app.persistence.migrations import SchemaCompatibilityError
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


def legacy_store(tmp_path):
    f = make_fixture(tmp_path / 'canonical', backend='memory')
    catalog = tmp_path / 'legacy-catalog'
    catalog.mkdir()
    source = Path(__file__).parents[1] / 'app/analytics/sql'
    for migration in sorted(source.glob('*.sql'))[:20]:
        shutil.copy2(migration, catalog / migration.name)
    database = ProjectionsDatabase(tmp_path / 'analytics.sqlite3', migrations_dir=catalog)
    options = dict(activation=f.repositories.projection_activation,
                   canonical_identity_reader=f.source.read_identity)
    store = SQLiteAnalyticsProjectionStore(database, **options)
    pipeline = AnalyticsPipeline(f.source, projections=store,
                                 enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    return f, catalog, database, options, pipeline


def test_schema21_keeps_legacy_units_and_published_output(tmp_path):
    f, catalog, database, options, pipeline = legacy_store(tmp_path)
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        with database.read() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM conversation_graph_units ORDER BY unit_id')]
            assert db.execute('PRAGMA user_version').fetchone()[0] == 20
        upgraded = ProjectionsDatabase(database.path)
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 21
            assert content_stamp(db) is not None
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            rows = [tuple(r) for r in db.execute('SELECT * FROM conversation_graph_units ORDER BY unit_id')]
            assert [row[:-2] for row in rows] == before
            assert all(row[-2:] == (1, None) for row in rows)
        current = SQLiteAnalyticsProjectionStore(upgraded, **options)
        assert current.get_artifact(ACCOUNT) == expected
        assert upgraded.migration_runner.last_backup_path is not None
        backup = upgraded.open_detached(upgraded.migration_runner.last_backup_path, read_only=True)
        try:
            assert backup.execute('PRAGMA user_version').fetchone()[0] == 20
            assert [tuple(r) for r in backup.execute('SELECT * FROM conversation_graph_units ORDER BY unit_id')] == before
        finally:
            backup.close()
        with pytest.raises(SchemaCompatibilityError):
            ProjectionsDatabase(database.path, migrations_dir=catalog)
    finally:
        cleanup(f)


def test_failed_integrity_migration_rolls_back_all_schema_changes(tmp_path):
    f, catalog, database, options, pipeline = legacy_store(tmp_path)
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        source = Path(__file__).parents[1] / 'app/analytics/sql/0021_conversation_integrity.sql'
        (catalog / source.name).write_text(source.read_text() + '\nINVALID INTEGRITY MIGRATION;\n')
        with pytest.raises(sqlite3.DatabaseError):
            ProjectionsDatabase(database.path, migrations_dir=catalog)
        with database.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 20
            assert 'checksum_version' not in {r[1] for r in db.execute('PRAGMA table_info(conversation_graph_units)')}
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert SQLiteAnalyticsProjectionStore(database, **options).get_artifact(ACCOUNT) == expected
    finally:
        cleanup(f)
