"""Keep shared analyzer documents scoped, immutable and disposable."""

from datetime import timedelta
from pathlib import Path
import shutil

import pytest

from app.analytics.validation_receipt import content_stamp
from app.persistence import sqlite_api
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, make_fixture,
)


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path)
    yield value
    cleanup(value)


def counts(fixture):
    with fixture.stores.database.read() as db:
        return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('enrichment_content', 'enrichment_refs', 'enrichment_owned_records')}


def test_unchanged_generations_share_documents_and_discard_keeps_the_predecessor(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    assert before == {'enrichment_content': 27, 'enrichment_refs': 27, 'enrichment_owned_records': 0}
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert counts(fixture) == {**before, 'enrichment_refs': 54}
    fixture.pipeline.discard_candidate(candidate)
    assert counts(fixture) == before
    cold_equal(fixture, fixture.pipeline.rebuild_account(ACCOUNT).artifact)
    assert counts(fixture) == before
    fixture.stores.projections.clear(ACCOUNT)
    assert not any(counts(fixture).values())


def test_an_edit_replaces_only_changed_documents(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        before = {row[0] for row in db.execute('SELECT content_id FROM enrichment_content')}
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Scheduling request' WHERE message_id='m-1-1'")
        advance(db)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        selected = {row[0] for row in db.execute('SELECT content_id FROM enrichment_refs WHERE generation_id=?', (candidate.staged_generation_id,))}
    assert len(selected - before) == len(before - selected) == 3
    assert counts(fixture)['enrichment_content'] == 30
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)
    assert counts(fixture)['enrichment_content'] == 27


def test_corrupt_shared_bytes_use_an_owned_replacement_without_overwriting(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        key, original = db.execute('SELECT content_id,document_json FROM enrichment_content LIMIT 1').fetchone()
        db.execute('DROP TRIGGER enrichment_content_immutable')
        db.execute("UPDATE enrichment_content SET document_json=document_json || ' ' WHERE content_id=?", (key,))
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert counts(fixture)['enrichment_owned_records'] == 1
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT document_json FROM enrichment_content WHERE content_id=?', (key,)).fetchone()[0] == original + ' '
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)
    assert counts(fixture)['enrichment_content'] == 26
    cold_equal(fixture, fixture.pipeline.rebuild_account(ACCOUNT).artifact)
    assert counts(fixture) == {'enrichment_content': 27, 'enrichment_refs': 27, 'enrichment_owned_records': 0}


@pytest.mark.parametrize('table', ['enrichment_content', 'enrichment_refs', 'enrichment_owned_records'])
@pytest.mark.parametrize('operation', ['insert', 'update', 'delete'])
def test_missing_content_tracking_disables_receipts(fixture, table, operation):
    name = 'enrichment_reuse' if table == 'enrichment_owned_records' else table
    with fixture.stores.database.transaction() as db:
        assert content_stamp(db) is not None
        db.execute('DROP TRIGGER generation_content_' + name + '_' + operation)
        assert content_stamp(db) is None


@pytest.mark.parametrize('table', ['enrichment_content', 'enrichment_refs', 'enrichment_owned_records'])
@pytest.mark.parametrize('operation', ['update', 'replace'])
@pytest.mark.parametrize('recursive', [False, True])
def test_immutable_rows_reject_sql_replacement(fixture, table, operation, recursive):
    fixture.stores.projections.reuse_enrichment_content = table != 'enrichment_owned_records'
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        db.execute('PRAGMA recursive_triggers=' + ('ON' if recursive else 'OFF'))
        before = [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
        assert before
        statement = ('UPDATE ' + table + ' SET creator_account_id=creator_account_id' if operation == 'update'
                     else 'INSERT OR REPLACE INTO ' + table + ' SELECT * FROM ' + table)
        with pytest.raises(sqlite_api.IntegrityError):
            db.execute(statement)
        assert [tuple(row) for row in db.execute('SELECT * FROM ' + table)] == before


def test_source_expiry_reclaims_shared_documents(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.clock.now += timedelta(days=91)
    assert fixture.stores.projections.enforce_retention(ACCOUNT)
    assert not any(counts(fixture).values())


def test_new_content_is_rolled_back_when_reference_insertion_fails(fixture, monkeypatch):
    from app.analytics import enrichment_sql
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Different request' WHERE message_id='m-1-1'")
        advance(db)
    insert, writes = enrichment_sql.insert_entries, []
    def failing(connection, generation_id, entries, **options):
        class Connection:
            def __getattr__(self, name):
                return getattr(connection, name)
            def executemany(self, sql, parameters):
                if sql.startswith('INSERT INTO enrichment_refs'):
                    raise RuntimeError('reference insertion failed')
                writes.append(sql)
                return connection.executemany(sql, parameters)
        return insert(Connection(), generation_id, entries, **options)
    monkeypatch.setattr(enrichment_sql, 'insert_entries', failing)
    with pytest.raises(RuntimeError, match='reference insertion failed'):
        fixture.pipeline.build_candidate(ACCOUNT)
    assert any('enrichment_content' in sql for sql in writes)
    assert counts(fixture) == before
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 1


def test_legacy_owned_records_remain_readable_and_convert(fixture, tmp_path):
    from app.analytics.database import ProjectionsDatabase
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    migrations = tmp_path / 'version-13'
    migrations.mkdir()
    for path in sorted((Path(__file__).parents[1] / 'app/analytics/sql').glob('*.sql'))[:13]:
        shutil.copy2(path, migrations / path.name)
    path = tmp_path / 'legacy.sqlite3'
    options = dict(activation=fixture.repositories.projection_activation,
                   canonical_identity_reader=fixture.source.read_identity)
    database = ProjectionsDatabase(path, migrations_dir=migrations)
    legacy = SQLiteAnalyticsProjectionStore(database, **options)
    pipeline = AnalyticsPipeline(fixture.source, projections=legacy,
                                 enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    first = pipeline.project_account(ACCOUNT).artifact
    upgraded = ProjectionsDatabase(path)
    with upgraded.read() as db:
        assert db.execute('SELECT COUNT(*) FROM enrichment_owned_records').fetchone()[0] == 27
        assert content_stamp(db) is not None
    store = SQLiteAnalyticsProjectionStore(upgraded, **options)
    pipeline = AnalyticsPipeline(fixture.source, projections=store,
                                 enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    fixture.source.loaded.clear()
    assert pipeline.rebuild_account(ACCOUNT).artifact == first
    assert fixture.source.loaded == []
    with upgraded.read() as db:
        assert db.execute('SELECT COUNT(*) FROM enrichment_refs').fetchone()[0] == 27
        assert db.execute('SELECT COUNT(*) FROM enrichment_owned_records').fetchone()[0] == 0


def test_missing_shared_content_fails_independent_generation_validation(fixture):
    from app.analytics.sqlite_projection_store import recompute_generation
    from app.analytics.graph_store import GraphReferentialIntegrityError
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DROP TRIGGER enrichment_content_referenced')
        db.execute('DELETE FROM enrichment_content WHERE content_id=(SELECT content_id FROM enrichment_content LIMIT 1)')
        with pytest.raises(GraphReferentialIntegrityError, match='enrichment_reference_invalid'):
            recompute_generation(db, generation)


def test_sharing_requires_the_callers_transaction(fixture):
    from app.analytics.enrichment_sql import insert_entries
    with fixture.stores.database.read() as db:
        assert not db.in_transaction
        with pytest.raises(ValueError, match='enrichment_sharing_requires_transaction'):
            insert_entries(db, 'absent', (), shared=True)


@pytest.mark.parametrize('size', [65537, 131072])
def test_oversized_shared_documents_use_owned_replacements(fixture, size):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        key, original = db.execute('SELECT content_id,document_json FROM enrichment_content LIMIT 1').fetchone()
        db.execute('DROP TRIGGER enrichment_content_immutable')
        db.execute('PRAGMA ignore_check_constraints=ON')
        db.execute('UPDATE enrichment_content SET document_json=? WHERE content_id=?',
                   (original + ' ' * size, key))
        db.execute('PRAGMA ignore_check_constraints=OFF')
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert counts(fixture)['enrichment_owned_records'] == 1
    cold_equal(fixture, fixture.pipeline.publish_candidate(candidate).artifact)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT 1 FROM enrichment_content WHERE content_id=?', (key,)).fetchone() is None


@pytest.mark.parametrize('shared', [False, True])
def test_storage_modes_preserve_exact_keys_bytes_and_expiry(fixture, shared):
    fixture.stores.projections.reuse_enrichment_content = not shared
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        before = [tuple(row) for row in db.execute('SELECT cache_key,expires_at,document_json,document_digest FROM enrichment_reuse ORDER BY cache_key')]
    fixture.stores.projections.reuse_enrichment_content = shared
    cold_equal(fixture, fixture.pipeline.rebuild_account(ACCOUNT).artifact)
    with fixture.stores.database.read() as db:
        assert before == [tuple(row) for row in db.execute('SELECT cache_key,expires_at,document_json,document_digest FROM enrichment_reuse ORDER BY cache_key')]


@pytest.mark.parametrize('mutation', ['message', 'conversation'])
def test_deletion_reclaims_only_documents_without_live_references(fixture, mutation):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        if mutation == 'message':
            db.execute("DELETE FROM account_messages WHERE message_id='m-1-1'")
        else:
            db.execute("DELETE FROM account_messages WHERE chat_id='chat-1'")
            db.execute("DELETE FROM account_chats WHERE chat_id='chat-1'")
        advance(db)
    cold_equal(fixture, fixture.pipeline.project_account(ACCOUNT).artifact)
    expected = 24 if mutation == 'message' else 18
    assert counts(fixture) == {'enrichment_content': expected, 'enrichment_refs': expected,
                               'enrichment_owned_records': 0}


def test_shared_documents_never_cross_accounts(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    other = 'synthetic-other-owner'
    with fixture.repositories.database.transaction() as db:
        for table in ('account_heads', 'account_chats', 'account_messages'):
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
            selection = ','.join('?' if c == 'creator_account_id' else '"' + c + '"' for c in columns)
            db.execute('INSERT INTO ' + table + ' SELECT ' + selection + ' FROM ' + table + ' WHERE creator_account_id=?', (other, ACCOUNT))
    fixture.pipeline.project_account(other)
    assert counts(fixture)['enrichment_content'] == 54
    fixture.stores.projections.clear(other)
    assert counts(fixture) == {'enrichment_content': 27, 'enrichment_refs': 27, 'enrichment_owned_records': 0}


@pytest.mark.parametrize('cancel_after', ['content', 'refs'])
def test_cancellation_rolls_back_shared_documents_and_references(fixture, monkeypatch, cancel_after):
    from app.analytics import enrichment_sql
    from app.analytics.errors import ProjectionBuildCancelled
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Different request' WHERE message_id='m-1-1'")
        advance(db)
    insert, writes = enrichment_sql.insert_entries, []
    def observed(connection, generation_id, entries, **options):
        class Connection:
            def __getattr__(self, name):
                return getattr(connection, name)
            def executemany(self, sql, parameters):
                result = connection.executemany(sql, parameters)
                writes.append(sql)
                return result
        return insert(Connection(), generation_id, entries, **options)
    monkeypatch.setattr(enrichment_sql, 'insert_entries', observed)
    def cancelled():
        return any(sql.startswith('INSERT INTO enrichment_' + cancel_after + '(') for sql in writes)
    with pytest.raises(ProjectionBuildCancelled):
        fixture.pipeline.build_candidate(ACCOUNT, cancellation_check=cancelled)
    assert writes and counts(fixture) == before
    with fixture.stores.database.read() as db:
        assert not list(db.execute('PRAGMA foreign_key_check'))
