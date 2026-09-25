"""Graph content survives until its last account-scoped reference is removed."""

from pathlib import Path
import re
import shutil

import pytest
from sqlcipher3 import dbapi2 as sqlite3

from app.analytics.database import ProjectionsDatabase
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.validation_receipt import content_stamp
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)

SQL = Path(__file__).parents[1] / 'app/analytics/sql'


@pytest.fixture(params=[18, 19])
def reclaim_connection(request):
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    for kind in ('node', 'edge'):
        db.execute(f'''CREATE TABLE graph_{kind}_content (
            creator_account_id TEXT, content_id TEXT, payload TEXT,
            PRIMARY KEY(creator_account_id,content_id))''')
        db.execute(f'''CREATE TABLE graph_segment_{kind}s (
            creator_account_id TEXT, segment_id TEXT, record_id TEXT,
            content_id TEXT, PRIMARY KEY(creator_account_id,segment_id,record_id),
            FOREIGN KEY(creator_account_id,content_id)
                REFERENCES graph_{kind}_content(creator_account_id,content_id))''')
        source = (SQL/'0010_shared_graph_segments.sql').read_text()
        triggers = re.findall(
            rf'CREATE TRIGGER graph_{kind}_content_reclaim\b.*?END;', source, re.S
        )
        assert len(triggers) == 1
        db.executescript(triggers[0])
    if request.param == 19:
        db.executescript((SQL/'0019_graph_reclamation_guards.sql').read_text())
    yield db
    db.close()


def populate(db, kind):
    db.executemany(f'INSERT INTO graph_{kind}_content VALUES (?,?,?)', [
        ('a', 'shared', 'shared content'), ('a', 'old', 'old version'),
        ('a', 'new', 'new version'), ('b', 'shared', 'another account'),
    ])
    db.executemany(f'INSERT INTO graph_segment_{kind}s VALUES (?,?,?,?)', [
        ('a', 's1', 'r1', 'shared'), ('a', 's2', 'r1', 'shared'),
        ('a', 's1', 'r2', 'old'), ('a', 's2', 'r2', 'new'),
        ('b', 's1', 'r1', 'shared'),
    ])
    db.commit()


@pytest.mark.parametrize('kind', ['node', 'edge'])
@pytest.mark.parametrize('foreign_keys', [0, 1])
def test_reclaim_preserves_shared_versions_and_other_accounts(
    reclaim_connection, kind, foreign_keys
):
    db = reclaim_connection
    populate(db, kind)
    db.execute(f'PRAGMA foreign_keys={foreign_keys}')
    db.execute(f'DELETE FROM graph_segment_{kind}s '
               'WHERE creator_account_id=? AND segment_id=?', ('a', 's1'))
    assert db.execute(f'SELECT * FROM graph_{kind}_content '
                      'ORDER BY creator_account_id,content_id').fetchall() == [
        ('a', 'new', 'new version'), ('a', 'shared', 'shared content'),
        ('b', 'shared', 'another account'),
    ]
    db.execute(f'DELETE FROM graph_segment_{kind}s '
               'WHERE creator_account_id=?', ('a',))
    assert db.execute(f'SELECT * FROM graph_{kind}_content').fetchall() == [
        ('b', 'shared', 'another account')
    ]
    db.execute(f'DELETE FROM graph_segment_{kind}s')
    assert not db.execute(f'SELECT * FROM graph_{kind}_content').fetchall()
    assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_reclaim_failure_rolls_back_membership_and_content(reclaim_connection, kind):
    db = reclaim_connection
    populate(db, kind)
    tables = (f'graph_segment_{kind}s', f'graph_{kind}_content')
    def snapshot():
        return [sorted(db.execute('SELECT * FROM '+table).fetchall()) for table in tables]
    before = snapshot()
    db.execute(f'''CREATE TRIGGER reject_final_content BEFORE DELETE ON graph_{kind}_content
        WHEN OLD.content_id='new'
        BEGIN SELECT RAISE(ABORT,'injected failure'); END''')
    db.execute('BEGIN IMMEDIATE')
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        db.execute(f'DELETE FROM graph_segment_{kind}s WHERE creator_account_id=?', ('a',))
    assert snapshot() == before
    db.rollback()
    assert snapshot() == before
    db.execute('DROP TRIGGER reject_final_content')
    db.execute(f'DELETE FROM graph_segment_{kind}s WHERE creator_account_id=?', ('a',))
    assert db.execute(f'SELECT creator_account_id FROM graph_{kind}_content').fetchall() == [('b',)]


def test_populated_upgrade_preserves_graph_and_reclaims_final_references(tmp_path):
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore

    f = make_fixture(tmp_path/'canonical', backend='memory', conversations=3, messages=12)
    catalog = tmp_path/'catalog'
    catalog.mkdir()
    for migration in sorted(SQL.glob('*.sql'))[:18]:
        shutil.copy2(migration, catalog/migration.name)
    legacy = ProjectionsDatabase(tmp_path/'legacy.sqlite3', migrations_dir=catalog)
    options = dict(activation=f.repositories.projection_activation,
                   canonical_identity_reader=f.source.read_identity)
    store = SQLiteAnalyticsProjectionStore(legacy, **options)
    store.rollback_retention = 2
    pipeline = AnalyticsPipeline(f.source, projections=store,
                                 enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    try:
        pipeline.project_account(ACCOUNT)
        for ordinal in (1, 2):
            with f.repositories.database.transaction() as db:
                insert_message(db, 'chat-1', f'reclaim-{ordinal}', NOW, ordinal)
                advance(db)
            expected = pipeline.project_account(ACCOUNT).artifact
        with legacy.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 18
            before = [tuple(row) for row in db.execute(
                'SELECT * FROM graph_node_content ORDER BY creator_account_id,content_id'
            )]
            assert _guards_match(db) and content_stamp(db) is not None
        upgraded = ProjectionsDatabase(legacy.path)
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 19
            assert _guards_match(db) and content_stamp(db) is not None
            assert [tuple(row) for row in db.execute(
                'SELECT * FROM graph_node_content ORDER BY creator_account_id,content_id'
            )] == before
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert upgraded.migration_runner.last_backup_path is not None
        with upgraded.open_detached(upgraded.migration_runner.last_backup_path, read_only=True) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 18
            assert [tuple(row) for row in db.execute(
                'SELECT * FROM graph_node_content ORDER BY creator_account_id,content_id'
            )] == before
        current = SQLiteAnalyticsProjectionStore(upgraded, rollback_retention=2, **options)
        current.rollback_retention = 0
        assert current.get_artifact(ACCOUNT) == expected
        with upgraded.read() as db:
            retired = db.execute(
                "SELECT COUNT(*) FROM projection_generations WHERE status='retired'"
            ).fetchone()[0]
        assert retired > 0
        assert current.collect_garbage(expected.projection.account_ref) == retired
        with upgraded.read() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM projection_generations WHERE status='retired'"
            ).fetchone()[0] == 0
        assert current.get_artifact(ACCOUNT) == expected
        cold_equal(f, expected)
        with upgraded.read() as db:
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            for kind in ('node', 'edge'):
                assert not db.execute(f'''SELECT 1 FROM graph_{kind}_content c
                    WHERE NOT EXISTS(SELECT 1 FROM graph_segment_{kind}s r
                        WHERE r.creator_account_id=c.creator_account_id
                          AND r.content_id=c.content_id) LIMIT 1''').fetchone()
        current.clear(ACCOUNT)
        with upgraded.read() as db:
            for table in ('graph_node_content', 'graph_edge_content', 'graph_node_identities',
                          'graph_segment_nodes', 'graph_segment_edges', 'graph_segments',
                          'generation_graph_segments', 'graph_segment_chunks'):
                assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0
    finally:
        cleanup(f)


def test_unreviewed_catalog_does_not_renew_enrichment_proofs(tmp_path):
    database = ProjectionsDatabase(tmp_path/'analytics.sqlite3')
    with database.read() as db:
        assert _guards_match(db) and content_stamp(db) is not None
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('PRAGMA user_version=20')
            assert not _guards_match(db)
            assert content_stamp(db) is None
        finally:
            db.rollback()
        assert _guards_match(db) and content_stamp(db) is not None
