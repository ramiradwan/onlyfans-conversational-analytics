"""Verify physical graph reuse without changing generation semantics."""

from datetime import timedelta
from pathlib import Path
import shutil

import pytest

from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.opaque_refs import account_ref
from app.analytics.sqlite_projection_store import ProjectionValidationError
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, make_fixture, cleanup, cold_equal, insert_message, advance,
)


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=3, messages=30)
    yield value
    cleanup(value)


def counts(fixture):
    with fixture.stores.database.read() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                     for table in ('graph_node_content','graph_edge_content','graph_segments'))


def test_unchanged_graph_reuses_all_physical_records(fixture, monkeypatch):
    import app.analytics.shared_graph as storage
    writes = []
    original = storage.write_shared_graph
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        writes.append(result)
        return result
    monkeypatch.setattr(storage, 'write_shared_graph', observed)
    first = fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    second = fixture.pipeline.rebuild_account(ACCOUNT)
    assert second.artifact == first.artifact
    assert counts(fixture) == before
    assert writes[-1]['segments_written'] == 0
    assert writes[-1]['node_content_written'] == writes[-1]['edge_content_written'] == 0
    cold_equal(fixture, second.artifact)


@pytest.mark.parametrize('mutation', ['append','edit','delete','late','direction','participant'])
def test_changed_graph_matches_full_rebuild(fixture, mutation):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        if mutation in ('append','late'):
            at = NOW if mutation == 'append' else NOW-timedelta(days=20)
            insert_message(db, 'chat-1', 'new-message', at)
        elif mutation == 'edit':
            db.execute("UPDATE account_messages SET text='Different scheduling' WHERE message_id='m-1-1'")
        elif mutation == 'delete':
            db.execute("DELETE FROM account_messages WHERE message_id='m-1-1'")
        elif mutation == 'direction':
            db.execute("UPDATE account_messages SET direction='inbound' WHERE message_id='m-1-1'")
        else:
            db.execute("UPDATE account_chats SET platform_user_id='synthetic-other' WHERE chat_id='chat-1'")
        advance(db)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    result = fixture.pipeline.publish_candidate(candidate)
    cold_equal(fixture, result.artifact)
    with fixture.stores.database.read() as db:
        assert not list(db.execute('PRAGMA foreign_key_check'))
        assert db.execute('SELECT COUNT(*) FROM graph_owned_nodes').fetchone()[0] == 0
        shared = db.execute('''SELECT COUNT(*) FROM generation_graph_segments m
          WHERE m.generation_id=? AND EXISTS(SELECT 1 FROM generation_graph_segments p
          WHERE p.creator_account_id=m.creator_account_id AND p.segment_id=m.segment_id
          AND p.generation_id!=m.generation_id)''', (candidate.staged_generation_id,)).fetchone()[0]
        assert shared > 0


@pytest.mark.parametrize('table,column', [('graph_node_content','properties_json'),
    ('graph_edge_content','properties_json'), ('graph_segments','content_digest'),
    ('graph_segment_nodes','content_id'), ('graph_segment_edges','content_id'),
    ('generation_graph_segments','segment_id')])
def test_active_content_and_manifest_are_immutable(fixture, table, column):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'UPDATE {table} SET {column}={column}')


@pytest.mark.parametrize('table', ['graph_nodes','graph_edges','generation_graph_segments',
    'graph_segment_nodes','graph_segment_edges','graph_segments','graph_node_content','graph_edge_content'])
def test_active_records_cannot_be_deleted(fixture, table):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM '+table)


@pytest.mark.parametrize('table,error', [
    ('graph_node_content', 'graph_content_referenced'),
    ('graph_edge_content', 'graph_content_referenced'),
    ('graph_segments', 'graph_segment_referenced'),
])
def test_referenced_shared_graph_rows_resist_delete_without_foreign_keys(
    fixture, table, error
):
    fixture.pipeline.project_account(ACCOUNT)
    connection = fixture.stores.database.connect()
    try:
        connection.execute('PRAGMA foreign_keys=OFF')
        before = connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        assert before > 0
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.IntegrityError, match=error):
            connection.execute('DELETE FROM ' + table)
        connection.rollback()
        assert connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == before
    finally:
        connection.close()


def test_reclaim_keeps_current_shared_records_then_removes_everything(fixture):
    fixture.stores.projections.rollback_retention = 0
    for index in range(3):
        with fixture.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'added-'+str(index), NOW+timedelta(seconds=index))
            advance(db)
        result = fixture.pipeline.project_account(ACCOUNT)
        cold_equal(fixture, result.artifact)
        with fixture.stores.database.read() as db:
            assert not list(db.execute('PRAGMA foreign_key_check'))
            assert db.execute("SELECT COUNT(*) FROM projection_generations WHERE status='retired'").fetchone()[0] == 0
    fixture.stores.projections.clear(ACCOUNT)
    assert counts(fixture) == (0,0,0)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM generation_graph_segments').fetchone()[0] == 0


def test_reused_corrupt_payload_is_rejected_before_activation(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        db.execute('DROP TRIGGER graph_node_content_immutable')
        db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
    with fixture.repositories.database.transaction() as db:
        advance(db)
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.build_candidate(ACCOUNT)


def test_failed_segment_write_cannot_publish(fixture, monkeypatch):
    import app.analytics.shared_graph as storage
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        previous = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'added-message', NOW)
        advance(db)
    original = storage._records
    def interrupted(*args, **kwargs):
        yield from islice_for_test(original(*args, **kwargs), 1)
        raise ProjectionBuildCancelled()
    monkeypatch.setattr(storage, '_records', interrupted)
    with pytest.raises(ProjectionBuildCancelled):
        fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0] == previous


def islice_for_test(values, count):
    from itertools import islice
    return islice(values, count)


def test_expiry_removes_shared_payloads(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.clock.now = NOW + timedelta(days=91)
    assert fixture.stores.projections.enforce_retention(ACCOUNT)
    assert counts(fixture) == (0,0,0)


def test_reopening_preserves_shared_content(fixture, tmp_path):
    from app.analytics.factory import create_analytics_stores
    from app.analytics.pipeline import AnalyticsPipeline
    first = fixture.pipeline.project_account(ACCOUNT).artifact
    before = counts(fixture)
    cleanup(fixture)
    reopened = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
        activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity, retention_clock=lambda: NOW)
    try:
        pipeline = AnalyticsPipeline(fixture.source, projections=reopened.projections,
            enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
        result = pipeline.rebuild_account(ACCOUNT)
        assert result.artifact == first
        assert counts(fixture) == before
        with reopened.database.read() as db:
            assert not list(db.execute('PRAGMA foreign_key_check'))
    finally:
        reopened.projections.close_retention_scheduler()


def test_accounts_do_not_share_physical_content(fixture):
    other = 'synthetic-shared-other-owner'
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        for table in ('account_heads','account_chats','account_messages'):
            records = [dict(row) for row in db.execute('SELECT * FROM '+table+' WHERE creator_account_id=?', (ACCOUNT,))]
            for record in records:
                record['creator_account_id'] = other
                fields = ','.join(record)
                marks = ','.join('?' for _ in record)
                db.execute(f'INSERT INTO {table}({fields}) VALUES ({marks})', tuple(record.values()))
    fixture.pipeline.project_account(other)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(DISTINCT creator_account_id) FROM graph_node_content').fetchone()[0] == 2
        assert db.execute('''SELECT COUNT(*) FROM graph_node_content a JOIN graph_node_content b
          ON a.content_id=b.content_id AND a.creator_account_id!=b.creator_account_id''').fetchone()[0] == 0
        assert not list(db.execute('PRAGMA foreign_key_check'))


@pytest.mark.parametrize('invalid', ['{"unknown_property":1}', '{"role":{"nested":true}}'])
def test_shared_content_preserves_sql_property_validation(fixture, invalid, monkeypatch):
    import app.analytics.shared_graph as storage
    original = storage._records
    def invalid_properties(*args, **kwargs):
        for values, membership in original(*args, **kwargs):
            yield (*values[:-1], invalid), membership
    monkeypatch.setattr(storage, '_records', invalid_properties)
    with pytest.raises(sqlite3.IntegrityError, match='graph_property_invalid'):
        fixture.pipeline.build_candidate(ACCOUNT)


def test_populated_owned_generation_upgrades_and_rebuilds(fixture, tmp_path):
    from app.analytics.database import ProjectionsDatabase
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    from app.analytics.pipeline import AnalyticsPipeline
    catalog = tmp_path/'catalog'
    catalog.mkdir()
    for path in (Path(__file__).parents[1]/'app/analytics/sql').glob('*.sql'):
        if int(path.name[:4]) <= 9:
            shutil.copy2(path, catalog/path.name)
    path = tmp_path/'owned.sqlite3'
    legacy = ProjectionsDatabase(path, migrations_dir=catalog)
    options = dict(activation=fixture.repositories.projection_activation,
                   canonical_identity_reader=fixture.source.read_identity)
    old_store = SQLiteAnalyticsProjectionStore(legacy, **options)
    first = AnalyticsPipeline(fixture.source, projections=old_store, clock=lambda: NOW).project_account(ACCOUNT).artifact
    upgraded = ProjectionsDatabase(path)
    assert upgraded.migration_runner.last_backup_path is not None
    store = SQLiteAnalyticsProjectionStore(upgraded, rollback_retention=0, **options)
    assert store.get_artifact(ACCOUNT) == first
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'after-upgrade', NOW)
        advance(db)
    current = AnalyticsPipeline(fixture.source, projections=store, clock=lambda: NOW).project_account(ACCOUNT)
    cold_equal(fixture, current.artifact)
    with upgraded.read() as db:
        assert db.execute('SELECT COUNT(*) FROM graph_owned_nodes').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM graph_node_content').fetchone()[0] > 0
        assert not list(db.execute('PRAGMA foreign_key_check'))


def test_small_change_does_not_rewrite_unchanged_content(fixture, monkeypatch):
    import app.analytics.shared_graph as storage
    observed = []
    original = storage.write_shared_graph
    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(storage, 'write_shared_graph', record)
    first = fixture.pipeline.project_account(ACCOUNT).artifact
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'one-addition', NOW)
        advance(db)
    second = fixture.pipeline.project_account(ACCOUNT).artifact
    changed = observed[-1]
    old_nodes = {node.node_id: node for node in first.nodes}
    old_edges = {edge.edge_id: edge for edge in first.edges}
    assert changed['node_content_written'] == sum(old_nodes.get(n.node_id) != n for n in second.nodes)
    assert changed['edge_content_written'] == sum(old_edges.get(e.edge_id) != e for e in second.edges)
    assert changed['node_content_written'] < 10
    assert changed['edge_content_written'] < 20
    assert changed['segments_reused'] > 0


def test_identity_catalog_disappears_after_final_generation(fixture):
    fixture.stores.projections.rollback_retention = 0
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections.clear(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM graph_node_identities').fetchone()[0] == 0


def test_incremental_validation_reuses_verified_segments(fixture, monkeypatch):
    import app.analytics.graph_verification as verification

    calls, original = [], verification.verify_graph_rows
    def observed(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(verification, 'verify_graph_rows', observed)

    fixture.pipeline.project_account(ACCOUNT)
    assert len(calls) == 1
    calls.clear()
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'verified-segment-reuse', NOW)
        advance(db)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result.changed and calls == []
    cold_equal(fixture, result.artifact)


def test_missing_segment_proof_falls_back_to_full_graph_validation(fixture, monkeypatch):
    import app.analytics.graph_verification as verification

    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections._graph_segment_proofs.clear()
    calls, original = [], verification.verify_graph_rows
    def observed(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(verification, 'verify_graph_rows', observed)

    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'missing-segment-proof', NOW)
        advance(db)
    fixture.pipeline.project_account(ACCOUNT)
    assert len(calls) == 1


def test_schema_change_invalidates_segment_proof(fixture, monkeypatch):
    import app.analytics.graph_verification as verification

    fixture.pipeline.project_account(ACCOUNT)
    calls, original = [], verification.verify_graph_rows
    def observed(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(verification, 'verify_graph_rows', observed)

    with fixture.stores.database.transaction() as db:
        db.execute('DROP TRIGGER graph_node_content_referenced')
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'schema-invalidates-proof', NOW)
        advance(db)
    fixture.pipeline.project_account(ACCOUNT)
    assert len(calls) == 1
