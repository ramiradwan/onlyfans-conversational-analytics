"""Verify page sharing without weakening generation, source, or cleanup checks."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import shutil

import pytest

from app.analytics.conversation_pages import ConversationPageReference
from app.analytics.conversation_reuse import ConversationBuild
from app.analytics.opaque_refs import account_ref
from app.analytics.validation_receipt import content_stamp
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=2, messages=0)
    with value.repositories.database.transaction() as db:
        for index in range(257):
            insert_message(db, 'chat-0', f'large-{index}',
                NOW - timedelta(days=2) + timedelta(minutes=index), index)
        insert_message(db, 'chat-1', 'small', NOW - timedelta(days=1))
    yield value
    cleanup(value)


def counts(fixture):
    with fixture.stores.database.read() as db:
        return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('conversation_page_content', 'conversation_page_refs',
                              'conversation_owned_pages', 'conversation_page_sets')}


def test_unchanged_pages_share_content_and_carry_only_references(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    stage = fixture.stores.projections.stage_built_artifact
    seen = []
    def observed(artifact, **kwargs):
        seen.extend(kwargs['conversation_pages'])
        return stage(artifact, **kwargs)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', observed)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    staged = counts(fixture)
    assert seen and all(isinstance(value, ConversationPageReference) for value in seen)
    assert all(not hasattr(value, 'pages') for value in seen)
    assert staged['conversation_page_content'] == before['conversation_page_content'] > 0
    assert staged['conversation_page_refs'] == before['conversation_page_refs'] * 2
    assert staged['conversation_owned_pages'] == 0
    result = fixture.pipeline.publish_candidate(candidate)
    assert counts(fixture) == before
    cold_equal(fixture, result.artifact)


def test_discard_preserves_shared_predecessor_and_clear_reclaims_last_reference(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    fixture.pipeline.discard_candidate(candidate)
    assert counts(fixture) == before
    fixture.stores.projections.clear(ACCOUNT)
    assert not any(counts(fixture).values())


def test_changed_pages_share_unchanged_payloads_and_reclaim_obsolete_content(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        previous = {row[0] for row in db.execute('SELECT content_id FROM conversation_page_content')}
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Scheduling request' WHERE message_id='large-65'")
        advance(db)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        selected = {row[0] for row in db.execute(
            'SELECT content_id FROM conversation_page_refs WHERE generation_id=?',
            (candidate.staged_generation_id,))}
        assert previous & selected
        assert previous - selected
        assert selected - previous
        assert db.execute('SELECT COUNT(*) FROM conversation_page_content').fetchone()[0] == len(previous | selected)
    result = fixture.pipeline.publish_candidate(candidate)
    with fixture.stores.database.read() as db:
        assert {row[0] for row in db.execute('SELECT content_id FROM conversation_page_content')} == selected
        assert not list(db.execute('PRAGMA foreign_key_check'))
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('fault', ['missing_page', 'account', 'generation', 'header', 'witness'])
def test_changed_reference_between_build_and_stage_is_rejected_atomically(fixture, monkeypatch, fault):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'added', NOW, 1)
        advance(db)
    stage = fixture.stores.projections.stage_built_artifact
    def changed(artifact, **kwargs):
        reference = kwargs['conversation_pages'][0]
        assert isinstance(reference, ConversationPageReference)
        if fault == 'missing_page':
            with fixture.stores.database.transaction() as db:
                db.execute('DELETE FROM conversation_pages WHERE ordinal=0')
        elif fault == 'witness':
            monkeypatch.setattr(fixture.stores.projections.activation, 'get', lambda _: None)
        else:
            if fault == 'generation':
                reference = replace(reference, generation_id='missing-generation')
            else:
                updates = {'account_ref': account_ref('another-account')} if fault == 'account' else {
                    'input_digest': 'sha256:' + 'e' * 64}
                reference = replace(reference, header=reference.header.model_copy(update=updates))
            kwargs['conversation_pages'] = (reference,)
        return stage(artifact, **kwargs)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', changed)
    expected = (
        'conversation_enrichment_unit_manifest_invalid'
        if fault == 'missing_page' else 'conversation_page_reference_'
    )
    with pytest.raises(ValueError, match=expected):
        fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM projection_generations WHERE status='active'").fetchone()[0] == 1
        assert not list(db.execute('PRAGMA foreign_key_check'))
    assert counts(fixture)['conversation_page_sets'] == before['conversation_page_sets']


def test_references_charge_the_complete_payload_to_the_existing_budget(fixture, monkeypatch):
    from app.analytics.conversation_page_sql import load_pages
    import app.analytics.conversation_reuse as reuse
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        row = db.execute('SELECT * FROM conversation_page_sets').fetchone()
        packed = load_pages(db, row['generation_id'], row['creator_account_id'],
                            row['conversation_ref'], row['input_digest'], row['config_digest'])
    state = ConversationBuild()
    state.retain_pages(packed)
    assert state.bytes_used == packed.retained_bytes
    assert state.page_sets[0].retained_bytes == packed.retained_bytes
    monkeypatch.setattr(reuse, 'MAX_FRAGMENT_TOTAL_BYTES', packed.retained_bytes - 1)
    limited = ConversationBuild()
    limited.retain_pages(packed)
    assert limited.page_sets == [] and limited.bytes_used == 0


@pytest.mark.parametrize('table', ['conversation_page_content', 'conversation_page_refs'])
def test_shared_storage_cannot_be_updated_and_deletion_advances_the_stamp(fixture, table):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        before = content_stamp(db)
        assert before is not None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'UPDATE {table} SET content_id=content_id')
        assert content_stamp(db) == before
        db.execute('DELETE FROM conversation_pages WHERE ordinal=0')
        assert content_stamp(db) != before


@pytest.mark.parametrize('table', ['conversation_page_content', 'conversation_page_refs'])
@pytest.mark.parametrize('operation', ['insert', 'update', 'delete'])
def test_missing_shared_storage_tracking_disables_validation_receipts(tmp_path, table, operation):
    from app.analytics.database import ProjectionsDatabase
    database = ProjectionsDatabase(tmp_path / 'tracking.sqlite3')
    with database.transaction() as db:
        assert content_stamp(db) is not None
        db.execute(f'DROP TRIGGER generation_content_{table}_{operation}')
        assert content_stamp(db) is None



def test_legacy_pages_survive_migration_and_convert_on_next_publication(fixture, tmp_path):
    from app.analytics.database import ProjectionsDatabase
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    catalog = tmp_path / 'version-12'
    catalog.mkdir()
    source = Path(__file__).parents[1] / 'app/analytics/sql'
    for path in sorted(source.glob('*.sql'))[:12]:
        shutil.copy2(path, catalog / path.name)
    path = tmp_path / 'legacy.sqlite3'
    database = ProjectionsDatabase(path, migrations_dir=catalog)
    options = dict(activation=fixture.repositories.projection_activation,
                   canonical_identity_reader=fixture.source.read_identity)
    legacy = SQLiteAnalyticsProjectionStore(database, **options)
    pipeline = AnalyticsPipeline(fixture.source, projections=legacy,
                                 enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    first = pipeline.project_account(ACCOUNT).artifact
    with database.read() as db:
        assert content_stamp(db) is not None
        before = db.execute('SELECT COUNT(*) FROM conversation_pages').fetchone()[0]
        assert before > 0
    upgraded = ProjectionsDatabase(path)
    with upgraded.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 17
        assert content_stamp(db) is not None
        assert db.execute('SELECT COUNT(*) FROM conversation_owned_pages').fetchone()[0] == before
        assert db.execute('SELECT COUNT(*) FROM conversation_page_content').fetchone()[0] == 0
    current = SQLiteAnalyticsProjectionStore(upgraded, **options)
    assert current.get_artifact(ACCOUNT) == first
    pipeline = AnalyticsPipeline(fixture.source, projections=current,
                                 enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    fixture.source.loaded.clear()
    result = pipeline.rebuild_account(ACCOUNT)
    assert fixture.source.loaded == []
    cold_equal(fixture, result.artifact)
    with upgraded.read() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_owned_pages').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM conversation_page_refs').fetchone()[0] == before
        assert db.execute('SELECT COUNT(*) FROM conversation_page_content').fetchone()[0] > 0
        assert not list(db.execute('PRAGMA foreign_key_check'))


@pytest.mark.parametrize('table', ['conversation_page_content', 'conversation_page_refs'])
def test_replacement_cannot_bypass_immutability_while_building(fixture, table):
    fixture.pipeline.project_account(ACCOUNT)
    visited = []
    def attempt(phase, generation):
        if phase != 'built':
            return
        with fixture.stores.database.transaction() as db:
            assert db.execute('PRAGMA recursive_triggers').fetchone()[0] == 0
            before = content_stamp(db)
            if table == 'conversation_page_content':
                row = db.execute('SELECT * FROM conversation_page_content LIMIT 1').fetchone()
                statement = 'INSERT OR REPLACE INTO conversation_page_content VALUES(?,?,?,?)'
                values = (row['creator_account_id'], row['content_id'], row['kind'], b'[]')
            else:
                row = db.execute('SELECT * FROM conversation_page_refs WHERE generation_id=? LIMIT 1',
                                 (generation,)).fetchone()
                statement = 'INSERT OR REPLACE INTO conversation_page_refs VALUES(?,?,?,?,?)'
                values = tuple(row)
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(statement, values)
            assert content_stamp(db) == before
            assert not list(db.execute('PRAGMA foreign_key_check'))
            visited.append(table)
    fixture.stores.projections.crash_hook = attempt
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    assert visited == [table]
    cold_equal(fixture, result.artifact)
