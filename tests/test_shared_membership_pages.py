"""Shared memberships preserve graph identity, scope and deletion guarantees."""

from pathlib import Path
import shutil

import pytest
from sqlcipher3 import dbapi2 as sqlite3

from app.analytics.database import ProjectionsDatabase
from app.analytics.enrichment_proof_transition import _guards_match
from app.analytics.validation_receipt import content_stamp
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)

TABLES = ('graph_membership_pages', 'graph_segment_membership_pages',
          'graph_membership_nodes', 'graph_membership_edges',
          'graph_node_content', 'graph_edge_content', 'graph_node_identities')


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=3, messages=20)
    yield value
    cleanup(value)


def add_message(fixture, ordinal=1):
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', f'page-message-{ordinal}', NOW, ordinal)
        advance(db)
    return fixture.pipeline.project_account(ACCOUNT)


def counts(connection):
    return {name: connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
            for name in TABLES}


def test_changed_segments_share_exact_unchanged_pages(fixture):
    fixture.stores.projections.rollback_retention = 2
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        old = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        before = counts(db)
    result = add_message(fixture)
    with fixture.stores.database.read() as db:
        new = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        changed = db.execute("""SELECT n.kind,n.segment_id,o.segment_id
            FROM generation_graph_segments n JOIN generation_graph_segments o
              USING(creator_account_id,kind,bucket)
            WHERE n.generation_id=? AND o.generation_id=? AND n.segment_id!=o.segment_id""", (new,old)).fetchall()
        assert changed
        logical_members = 0
        reused_pages = 0
        for kind, new_segment, old_segment in changed:
            logical_members += db.execute(
                f'SELECT COUNT(*) FROM graph_segment_{kind}s WHERE segment_id=?',
                (new_segment,)).fetchone()[0]
            reused_pages += db.execute("""SELECT COUNT(*) FROM graph_segment_membership_pages n
                JOIN graph_segment_membership_pages o USING(creator_account_id,page_id,bucket,kind)
                WHERE n.segment_id=? AND o.segment_id=?""", (new_segment, old_segment)).fetchone()[0]
        after = counts(db)
        written_members = sum(after[t]-before[t] for t in ('graph_membership_nodes','graph_membership_edges'))
        assert reused_pages > 0
        assert 0 < written_members < logical_members
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('foreign_keys', [0, 1])
@pytest.mark.parametrize('table,operation', [
    ('graph_membership_pages','DELETE'), ('graph_membership_pages','UPDATE'),
    ('graph_membership_pages','REPLACE'), ('graph_segment_membership_pages','DELETE'),
    ('graph_segment_membership_pages','UPDATE'), ('graph_segment_membership_pages','REPLACE'),
    ('graph_membership_nodes','DELETE'), ('graph_membership_nodes','UPDATE'),
    ('graph_membership_nodes','REPLACE'), ('graph_membership_edges','DELETE'),
    ('graph_membership_edges','UPDATE'), ('graph_membership_edges','REPLACE'),
])
def test_selected_pages_cannot_be_changed(fixture, foreign_keys, table, operation):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        db.execute(f'PRAGMA foreign_keys={foreign_keys}')
        before = counts(db)
        sql = {'DELETE': f'DELETE FROM {table}',
               'UPDATE': f'UPDATE {table} SET creator_account_id=creator_account_id',
               'REPLACE': f'INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1'}[operation]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
        assert counts(db) == before


def test_missing_page_guard_cannot_create_or_reuse_a_receipt(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        generation = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
        assert content_stamp(db) is not None
        assert fixture.stores.projections._trusted_graph_segment_proof(db, generation) is not None
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('DROP TRIGGER graph_membership_nodes_delete')
            assert not _guards_match(db)
            assert content_stamp(db) is None
            assert fixture.stores.projections._trusted_graph_segment_proof(db, generation) is None
        finally:
            db.rollback()
        assert content_stamp(db) is not None


def test_failed_page_reclamation_rolls_back_the_complete_selection(fixture):
    store = fixture.stores.projections
    store.rollback_retention = 2
    fixture.pipeline.project_account(ACCOUNT)
    latest = add_message(fixture).artifact
    store.rollback_retention = 0
    with fixture.stores.database.transaction() as db:
        before = counts(db)
        db.execute("""CREATE TRIGGER fail_page_cleanup BEFORE DELETE ON graph_membership_pages
            BEGIN SELECT RAISE(ABORT,'injected page cleanup failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='injected page cleanup failure'):
        store.collect_garbage(latest.projection.account_ref)
    with fixture.stores.database.transaction() as db:
        assert counts(db) == before
        db.execute('DROP TRIGGER fail_page_cleanup')
    assert store.get_artifact(ACCOUNT) == latest
    assert store.collect_garbage(latest.projection.account_ref) == 1
    assert store.get_artifact(ACCOUNT) == latest
    store.clear(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert all(value == 0 for value in counts(db).values())
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_populated_upgrade_preserves_logical_memberships_and_backup(tmp_path):
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore

    fixture = make_fixture(tmp_path/'canonical', backend='memory', conversations=3, messages=12)
    source = Path(__file__).parents[1]/'app/analytics/sql'
    catalog = tmp_path/'old-catalog'
    catalog.mkdir()
    for migration in sorted(source.glob('*.sql'))[:19]:
        shutil.copy2(migration, catalog/migration.name)
    legacy = ProjectionsDatabase(tmp_path/'legacy.sqlite3', migrations_dir=catalog)
    options = dict(activation=fixture.repositories.projection_activation,
                   canonical_identity_reader=fixture.source.read_identity)
    store = SQLiteAnalyticsProjectionStore(legacy, rollback_retention=2, **options)
    pipeline = AnalyticsPipeline(fixture.source, projections=store,
                                enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        with fixture.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'upgrade-page-message', NOW)
            advance(db)
        expected = pipeline.project_account(ACCOUNT).artifact
        def selected(db):
            return {kind: [tuple(row) for row in db.execute(
                f'SELECT * FROM graph_segment_{kind}s ORDER BY creator_account_id,segment_id,{kind}_id')]
                for kind in ('node','edge')}
        with legacy.read() as db:
            before = selected(db)
            assert db.execute('PRAGMA user_version').fetchone()[0] == 19
        upgraded = ProjectionsDatabase(legacy.path)
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 20
            assert selected(db) == before
            assert content_stamp(db) is not None and _guards_match(db)
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        backup = upgraded.migration_runner.last_backup_path
        assert backup is not None
        with upgraded.open_detached(backup, read_only=True) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 19
            assert selected(db) == before
        current = SQLiteAnalyticsProjectionStore(upgraded, rollback_retention=2, **options)
        assert current.get_artifact(ACCOUNT) == expected
        pipeline.projections = current
        pipeline.graph = current.graph
        with fixture.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'upgraded-next-message', NOW, 2)
            advance(db)
        expected = pipeline.project_account(ACCOUNT).artifact
        assert current.get_artifact(ACCOUNT) == expected
        cold_equal(fixture, expected)
        current.rollback_retention = 0
        current.clear(ACCOUNT)
        with upgraded.read() as db:
            assert all(value == 0 for value in counts(db).values())
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('kind', ['node','edge'])
def test_point_lookup_selects_exact_page_and_content_version(fixture, kind):
    from unittest.mock import Mock
    from app.analytics.shared_graph import selected_content_ids

    fixture.pipeline.project_account(ACCOUNT)
    latest = add_message(fixture).artifact
    account = latest.projection.account_ref
    with fixture.stores.database.read() as db:
        generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        expected = dict(db.execute(f"""SELECT r.{kind}_id,r.content_id FROM generation_graph_segments g
            JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)
            WHERE g.generation_id=? AND g.kind=?""", (generation,kind)))
        keys = list(expected)
        assert selected_content_ids(db, generation, account, kind, keys+keys, page_layout=True) == expected
        assert selected_content_ids(db, 'absent', account, kind, keys, page_layout=True) == {}
        assert selected_content_ids(db, generation, 'a1:'+'f'*64, kind, keys, page_layout=True) == {}
        observed = Mock(wraps=db)
        with pytest.raises(RuntimeError, match='cancelled'):
            selected_content_ids(observed, generation, account, kind, keys,
                check=Mock(side_effect=RuntimeError('cancelled')), page_layout=True)
        observed.execute.assert_not_called()


def test_page_preparation_checks_cancellation_before_sql():
    from unittest.mock import Mock
    from app.analytics.graph_membership_pages import prepare_pages

    db = Mock()
    with pytest.raises(RuntimeError, match='cancelled'):
        prepare_pages(db, 'account', 'segment', 'node', {}, [], 'previous',
                      check=Mock(side_effect=RuntimeError('cancelled')))
    db.execute.assert_not_called()


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_segment_rows_start_from_the_selected_page_manifest(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    account = add_message(fixture).artifact.projection.account_ref
    with fixture.stores.database.read() as db:
        generation = db.execute(
            "SELECT generation_id FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        segment = db.execute(
            'SELECT segment_id FROM generation_graph_segments '
            'WHERE generation_id=? AND creator_account_id=? AND kind=? LIMIT 1',
            (generation, account, kind),
        ).fetchone()[0]
        sql = (f'SELECT c.* FROM graph_segment_{kind}s r '
               f'JOIN graph_{kind}_content c USING(creator_account_id,content_id,{kind}_id) '
               f'WHERE r.creator_account_id=? AND r.segment_id=? ORDER BY r.{kind}_id')
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, (account, segment))]
        assert any('SEARCH m USING PRIMARY KEY' in step and 'segment_id=?' in step for step in plan)
        assert any('SEARCH r USING PRIMARY KEY' in step and 'page_id=?' in step for step in plan)
        actual = [tuple(row) for row in db.execute(sql, (account, segment))]
        expected = [tuple(row) for row in db.execute(
            f'SELECT c.* FROM graph_segment_membership_pages m '
            f'CROSS JOIN graph_membership_{kind}s r USING(creator_account_id,page_id) '
            f'JOIN graph_{kind}_content c USING(creator_account_id,content_id,{kind}_id) '
            f'WHERE m.creator_account_id=? AND m.segment_id=? AND m.kind=? '
            f'ORDER BY r.{kind}_id', (account, segment, kind))]
        assert actual == expected and actual
        assert not db.execute(sql, ('a1:' + 'f' * 64, segment)).fetchall()
        assert not db.execute(sql, (account, 'absent-segment')).fetchall()


def test_new_page_plans_do_not_read_payloads_without_a_predecessor():
    from unittest.mock import MagicMock, Mock
    from app.analytics.graph_membership_pages import prepare_pages

    records = MagicMock()
    records.__getitem__.side_effect = AssertionError('unexpected new-page payload read')
    keys = ['n1:abc' + '0' * 61, 'n1:abc' + '1' * 61, 'n1:abd' + '0' * 61]
    db = Mock()
    check = Mock()
    pages = prepare_pages(db, 'account', 'segment', 'node', records, keys, check=check)
    records.__getitem__.assert_not_called()
    assert {prefix: page.count for prefix, page in pages.items()} == {'abc': 2, 'abd': 1}
    assert not any(page.reused for page in pages.values())
    assert len({page.page_id for page in pages.values()}) == 2
    assert check.call_count >= len(keys)
    assert db.execute.call_count == 4


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_public_point_reads_start_at_the_requested_bucket(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    latest = add_message(fixture).artifact
    account = latest.projection.account_ref
    with fixture.stores.database.read() as db:
        generation = db.execute(
            "SELECT generation_id FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        identity = db.execute(
            f'SELECT {kind}_id FROM graph_{kind}_content '
            'WHERE creator_account_id=? LIMIT 1', (account,)
        ).fetchone()[0]
        sql = (f'SELECT * FROM graph_{kind}s WHERE generation_id=? '
               f'AND creator_account_id=? AND {kind}_id=?')
        parameters = (generation, account, identity)
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql, parameters)]
        assert any('generation_id=?' in step and 'bucket=?' in step for step in plan)
        actual = db.execute(sql, parameters).fetchone()
        assert actual is not None and actual[kind + '_id'] == identity
        assert db.execute(sql, ('missing', account, identity)).fetchone() is None
        assert db.execute(sql, (generation, 'a1:' + 'f' * 64, identity)).fetchone() is None


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_new_page_keys_are_grouped_and_unique_per_segment(kind):
    from unittest.mock import MagicMock, Mock
    from app.analytics.graph_membership_pages import prepare_pages

    records = MagicMock()
    records.__getitem__.side_effect = AssertionError('unexpected payload read')
    keys = ['x1:abd' + '0' * 61, 'x1:abc' + '0' * 61]
    first = '10000000-0000-4000-8000-000000000001'
    second = '20000000-0000-4000-8000-000000000002'
    a = prepare_pages(Mock(), 'account', first, kind, records, keys)
    b = prepare_pages(Mock(), 'account', second, kind, records, keys)
    assert len({p.page_id for p in (*a.values(), *b.values())}) == 4
    assert [p.page_id for p in a.values()] == sorted(p.page_id for p in a.values())
    assert all(p.page_id.startswith(first + ':') and len(p.page_id) == 38
               for p in a.values())
    assert all(not p.reused and p.count == 1 for p in (*a.values(), *b.values()))
    records.__getitem__.assert_not_called()
