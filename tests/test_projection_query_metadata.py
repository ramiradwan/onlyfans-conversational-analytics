"""Verify small query metadata remains bound to its immutable projection."""

import shutil
from pathlib import Path

import pytest

from app.analytics.database import ProjectionsDatabase
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.analytics.opaque_refs import account_ref
from app.persistence import sqlite_api
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


def test_metadata_is_exact_immutable_and_removed_with_retired_generation(tmp_path):
    f = make_fixture(tmp_path)
    try:
        value = f.pipeline.project_account(ACCOUNT).artifact.projection
        with f.stores.database.read() as db:
            row = db.execute('SELECT * FROM projection_query_metadata').fetchone()
            assert row['projection_digest'] == value.projection_digest
            assert row['projection_generation'] == value.projection_generation
            assert row['source_message_count'] == value.creator_metrics.message_count
            assert row['first_source'] == value.creator_metrics.active_from.isoformat().replace('+00:00','Z')
        for sql in ('UPDATE projection_query_metadata SET source_message_count=0', 'DELETE FROM projection_query_metadata'):
            with pytest.raises(sqlite_api.DatabaseError):
                with f.stores.database.transaction() as db:
                    db.execute(sql)
        generation = row['generation_id']
        f.pipeline.rebuild_account(ACCOUNT)
        with f.stores.database.transaction() as db:
            db.execute("DELETE FROM projection_generations WHERE generation_id=? AND status='retired'", (generation,))
        with f.stores.database.read() as db:
            assert db.execute('SELECT 1 FROM projection_query_metadata WHERE generation_id=?', (generation,)).fetchone() is None
    finally:
        cleanup(f)


def test_populated_metadata_upgrade_preserves_existing_projection(tmp_path):
    f = make_fixture(tmp_path/'canonical')
    catalog = tmp_path/'catalog'
    catalog.mkdir()
    for path in (Path(__file__).parents[1]/'app/analytics/sql').glob('*.sql'):
        if int(path.name[:4]) <= 6:
            shutil.copy2(path, catalog/path.name)
    path = tmp_path/'legacy.sqlite3'
    legacy = ProjectionsDatabase(path, migrations_dir=catalog)
    store = SQLiteAnalyticsProjectionStore(legacy, activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity)
    try:
        pipeline = AnalyticsPipeline(f.source, projections=store, clock=lambda: NOW)
        before = pipeline.project_account(ACCOUNT).artifact
        updated = ProjectionsDatabase(path)
        with updated.read() as db:
            row = db.execute('SELECT * FROM projection_query_metadata').fetchone()
            assert row['projection_digest'] == before.projection.projection_digest
            assert row['source_message_count'] == 9
        reopened = SQLiteAnalyticsProjectionStore(updated, activation=f.repositories.projection_activation,
            canonical_identity_reader=f.source.read_identity)
        assert reopened.get_artifact(ACCOUNT) == before
    finally:
        cleanup(f)


def test_direct_metadata_insert_cannot_invent_document_fields(tmp_path):
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.database.read() as db:
            row = db.execute('SELECT * FROM projection_query_metadata').fetchone()
        with pytest.raises(sqlite_api.DatabaseError, match='metadata_mismatch'):
            with f.stores.database.transaction() as db:
                db.execute('INSERT INTO projection_query_metadata VALUES (?,?,?,?,?,?)',
                    (row['generation_id'], row['creator_account_id'], 55, 0, None, row['projection_digest']))
    finally:
        cleanup(f)
