"""Check bounded reuse of large conversation outputs against clean builds."""

from dataclasses import replace
from datetime import timedelta
import json
import zlib

import pytest

from app.analytics.conversation_pages import PAGE_BYTES, PAGE_RECORDS, unpack_page
from app.analytics.opaque_refs import conversation_ref
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import (ACCOUNT, NOW, make_fixture, cleanup,
    cold_equal, insert_message, advance)


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=2, messages=0)
    with value.repositories.database.transaction() as db:
        for index in range(513):
            insert_message(db, 'chat-0', f'large-{index}',
                NOW-timedelta(days=3)+timedelta(minutes=index), index)
        insert_message(db, 'chat-1', 'small', NOW-timedelta(days=1))
    yield value
    cleanup(value)


def calls(fixture):
    return [item.calls for item in fixture.analyzers]


def test_unchanged_large_conversation_skips_source_and_projector(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    before = calls(fixture)
    fixture.source.loaded.clear()
    projector = fixture.pipeline.graph_projector.batches
    seen = []
    def observed(account, revision, conversations, *args, **kwargs):
        seen.extend(item.conversation_id for item in conversations)
        yield from projector(account, revision, conversations, *args, **kwargs)
    monkeypatch.setattr(fixture.pipeline.graph_projector, 'batches', observed)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'added', NOW, 1); advance(db)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    result = fixture.pipeline.publish_candidate(candidate)
    assert calls(fixture) == [n+1 for n in before]
    assert fixture.source.loaded == ['chat-1']
    assert seen == ['chat-1']
    cold_equal(fixture, result.artifact)


def test_pages_have_bounded_records_and_no_source_text(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        rows = list(db.execute('SELECT kind,data FROM conversation_pages'))
        assert rows and any(row['kind'] == 'analyzer' for row in rows)
        for row in rows:
            assert len(row['data']) <= PAGE_BYTES
            assert 1 <= len(json.loads(unpack_page(bytes(row['data'])))) <= PAGE_RECORDS
            assert b'Thanks pricing' not in unpack_page(bytes(row['data']))


@pytest.mark.parametrize('mutation', ['append', 'edit', 'delete', 'late', 'direction', 'participant'])
def test_changed_large_conversation_recomputes_exact_result(fixture, mutation):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.source.loaded.clear()
    with fixture.repositories.database.transaction() as db:
        if mutation in ('append', 'late'):
            at = NOW if mutation == 'append' else NOW-timedelta(days=10)
            insert_message(db, 'chat-0', 'new', at, 0)
        elif mutation == 'edit':
            db.execute("UPDATE account_messages SET text='Different scheduling' WHERE message_id='large-65'")
        elif mutation == 'delete':
            db.execute("DELETE FROM account_messages WHERE message_id='large-65'")
        elif mutation == 'direction':
            db.execute("UPDATE account_messages SET direction='outbound' WHERE message_id='large-64'")
        else:
            db.execute("UPDATE account_chats SET platform_user_id='other' WHERE chat_id='chat-0'")
        advance(db)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert fixture.source.loaded == ['chat-0']
    cold_equal(fixture, result.artifact)


def test_message_cache_survives_an_intervening_page_hit(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.pipeline.rebuild_account(ACCOUNT)
    before = calls(fixture)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Updated pricing' WHERE message_id='large-65'")
        advance(db)
    fixture.pipeline.project_account(ACCOUNT)
    assert calls(fixture) == [n+1 for n in before]


@pytest.mark.parametrize('fault', ['missing_page', 'corrupt_page', 'corrupt_header'])
def test_invalid_pages_fall_back_to_current_source(fixture, fault):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.source.loaded.clear()
    with fixture.stores.database.transaction() as db:
        if fault == 'missing_page':
            db.execute('DELETE FROM conversation_pages WHERE ordinal=1')
        elif fault == 'corrupt_page':
            db.execute('DROP TRIGGER conversation_pages_update_blocked')
            db.execute("UPDATE conversation_pages SET data=CAST('[]' AS BLOB) WHERE ordinal=1")
        else:
            db.execute('DROP TRIGGER conversation_page_sets_update_blocked')
            db.execute("UPDATE conversation_page_sets SET header_digest=?", ('f'*64,))
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    assert fixture.source.loaded == ['chat-0']
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('table', ['conversation_pages', 'conversation_page_sets'])
def test_cache_rows_are_immutable(fixture, table):
    fixture.pipeline.project_account(ACCOUNT)
    column = 'data' if table == 'conversation_pages' else 'header_json'
    with fixture.stores.database.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'UPDATE {table} SET {column}={column}')


def test_retirement_and_clear_remove_pages(fixture):
    fixture.stores.projections.rollback_retention = 0
    fixture.pipeline.project_account(ACCOUNT)
    fixture.pipeline.rebuild_account(ACCOUNT)
    fixture.stores.projections.clear(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_pages').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM conversation_page_sets').fetchone()[0] == 0


def test_page_budget_falls_back_without_changing_output(fixture, monkeypatch):
    import app.analytics.conversation_reuse as reuse
    monkeypatch.setattr(reuse, 'MAX_FRAGMENT_TOTAL_BYTES', 512)
    first = fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_page_sets').fetchone()[0] == 0
    second = fixture.pipeline.rebuild_account(ACCOUNT)
    assert second.artifact == first.artifact
    cold_equal(fixture, second.artifact)


def test_expiry_cannot_reuse_a_cached_source_window(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.clock.now = NOW+timedelta(days=90)
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    assert result.artifact.projection.message_enrichments == []
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM conversation_pages').fetchone()[0] == 0


def test_cancelled_candidate_cannot_supply_pages(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.discard_candidate(candidate)
    fixture.source.loaded.clear()
    fixture.pipeline.project_account(ACCOUNT)
    assert fixture.source.loaded == ['chat-0', 'chat-1']


def test_restart_can_reuse_witnessed_large_conversation(fixture):
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.factory import create_analytics_stores
    fixture.pipeline.project_account(ACCOUNT)
    options = dict(activation=fixture.repositories.projection_activation,
        canonical_identity_reader=fixture.source.read_identity, retention_clock=lambda: NOW)
    reopened = create_analytics_stores('sqlite', projections_path=fixture.stores.database.path, **options)
    try:
        pipeline = AnalyticsPipeline(fixture.source, projections=reopened.projections,
            enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
        fixture.source.loaded.clear()
        result = pipeline.rebuild_account(ACCOUNT)
        assert fixture.source.loaded == []
        cold_equal(fixture, result.artifact)
    finally:
        reopened.projections.close_retention_scheduler()


def test_configuration_change_cannot_reuse_pages(fixture):
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.pipeline import AnalyticsPipeline
    fixture.pipeline.project_account(ACCOUNT)
    fixture.analyzers[0].revision += '.changed'
    stage = EnrichmentStage(sentiment=fixture.analyzers[0], topics_entities=fixture.analyzers[1],
        engagement=fixture.analyzers[2])
    pipeline = AnalyticsPipeline(fixture.source, projections=fixture.stores.projections,
        enrichment=stage, clock=lambda: NOW)
    fixture.source.loaded.clear()
    pipeline.project_account(ACCOUNT)
    assert fixture.source.loaded == ['chat-0', 'chat-1']


@pytest.mark.parametrize('kind', ['message', 'node', 'analyzer'])
def test_staging_rejects_changed_pages_even_with_new_checksums(fixture, monkeypatch, kind):
    from app.analytics.conversation_pages import ConversationPage, page_digest
    stage = fixture.stores.projections.stage_built_artifact
    def altered(artifact, **kwargs):
        packed = kwargs['conversation_pages'][0]
        pages = list(packed.pages)
        index = next(i for i,p in enumerate(pages) if p.kind == kind)
        values = json.loads(unpack_page(pages[index].data))
        if kind == 'message':
            values[0]['direction'] = 'outbound'
        elif kind == 'node':
            values[0]['properties'] = {'role':'counterpart'}
        else:
            values[0]['key']['conversation_ref'] = conversation_ref(ACCOUNT, 'chat-1')
        pages[index] = ConversationPage(kind, zlib.compress(json.dumps(values).encode(), 1))
        header = packed.header.model_copy(update={'pages_digest':page_digest(pages),
            'byte_count':sum(len(p.data) for p in pages)})
        kwargs['conversation_pages'] = (replace(packed, header=header, pages=tuple(pages)),)
        return stage(artifact, **kwargs)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', altered)
    with pytest.raises(ValueError):
        fixture.pipeline.build_candidate(ACCOUNT)
    assert fixture.stores.projections.get(ACCOUNT) is None


@pytest.mark.parametrize('field', ['account', 'conversation', 'input_digest', 'config_digest'])
def test_lookup_requires_the_exact_account_and_recipe(fixture, field):
    from app.analytics.conversation_page_sql import load_pages
    from app.analytics.opaque_refs import account_ref
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        row = db.execute('SELECT * FROM conversation_page_sets').fetchone()
        options = {'account':row['creator_account_id'], 'conversation':row['conversation_ref'],
            'input_digest':row['input_digest'], 'config_digest':row['config_digest']}
        options[field] = (account_ref('another-account') if field == 'account' else
            conversation_ref(ACCOUNT, 'another-chat') if field == 'conversation' else 'sha256:'+'e'*64)
        assert load_pages(db, row['generation_id'], **options) is None


def test_page_restoration_checks_cancellation(fixture):
    from app.analytics.conversation_pages import restore_pages
    from app.analytics.conversation_page_sql import load_pages
    from app.analytics.errors import ProjectionBuildCancelled
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        row = db.execute('SELECT * FROM conversation_page_sets').fetchone()
        packed = load_pages(db, row['generation_id'], row['creator_account_id'],
            row['conversation_ref'], row['input_digest'], row['config_digest'])
    def stop():
        raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        restore_pages(packed, stop)


@pytest.mark.parametrize('fault', ['truncated', 'trailing', 'oversized', 'concatenated', 'invalid'])
def test_decompression_rejects_incomplete_or_unbounded_streams(fault):
    valid = zlib.compress(b'[]', 1)
    samples = {'truncated':valid[:-1], 'trailing':valid+b'x',
        'oversized':zlib.compress(b'['+b' ' * PAGE_BYTES+b']', 1),
        'concatenated':valid+valid, 'invalid':b'not compressed'}
    with pytest.raises(ValueError):
        unpack_page(samples[fault])


def test_decompression_accepts_the_exact_byte_limit():
    raw = b'[' + b' ' * (PAGE_BYTES-2) + b']'
    assert unpack_page(zlib.compress(raw, 1)) == raw
