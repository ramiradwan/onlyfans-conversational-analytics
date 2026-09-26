"""Preserve projection metadata binding while reducing full-document SQL parsing."""

from pathlib import Path
import shutil

import pytest

from app.analytics.database import ProjectionsDatabase
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence import sqlite_api
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


@pytest.mark.parametrize('field,value', [('projection_generation',55),('source_message_count',0),
    ('first_source',None),('first_source','2000-01-01T00:00:00Z'),
    ('projection_digest','sha256:'+'0'*64)])
def test_metadata_binding_rejects_each_mismatched_field(tmp_path, field, value):
    f=make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.database.read() as db:
            row=dict(db.execute('SELECT * FROM projection_query_metadata').fetchone())
        row[field]=value
        with pytest.raises(sqlite_api.DatabaseError,match='metadata_mismatch'):
            with f.stores.database.transaction() as db:
                db.execute('INSERT INTO projection_query_metadata VALUES (?,?,?,?,?,?)',tuple(row.values()))
    finally:
        cleanup(f)


def test_empty_projection_keeps_null_source_time(tmp_path):
    f=make_fixture(tmp_path,conversations=0,messages=0)
    try:
        projection=f.pipeline.project_account(ACCOUNT).artifact.projection
        with f.stores.database.read() as db:
            row=db.execute('SELECT * FROM projection_query_metadata').fetchone()
            assert row['projection_generation']==projection.projection_generation
            assert row['source_message_count']==0 and row['first_source'] is None
    finally:
        cleanup(f)


def test_populated_metadata_trigger_upgrade_preserves_artifact(tmp_path):
    f=make_fixture(tmp_path/'canonical')
    catalog=tmp_path/'catalog'; catalog.mkdir()
    for file in (Path(__file__).parents[1]/'app/analytics/sql').glob('*.sql'):
        if int(file.name[:4])<=7:
            shutil.copy2(file,catalog/file.name)
    path=tmp_path/'analytics.sqlite3'
    legacy=ProjectionsDatabase(path,migrations_dir=catalog)
    store=SQLiteAnalyticsProjectionStore(legacy,activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity)
    try:
        pipeline=AnalyticsPipeline(f.source,projections=store,clock=lambda:NOW)
        expected=pipeline.project_account(ACCOUNT).artifact
        with legacy.read() as db:
            before=[tuple(row) for row in db.execute('SELECT * FROM projection_query_metadata')]
        updated=ProjectionsDatabase(path)
        backup=updated.migration_runner.last_backup_path
        assert backup is not None
        with updated.open_detached(backup) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==7
            assert [tuple(row) for row in db.execute('SELECT * FROM projection_query_metadata')]==before
        with updated.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==20
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert [tuple(row) for row in db.execute('SELECT * FROM projection_query_metadata')]==before
        reopened=SQLiteAnalyticsProjectionStore(updated,activation=f.repositories.projection_activation,
            canonical_identity_reader=f.source.read_identity)
        assert reopened.get_artifact(ACCOUNT)==expected
    finally:
        cleanup(f)
