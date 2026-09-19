"""Check changed-conversation processing against full deterministic rebuilds."""

from datetime import timedelta
from contextlib import nullcontext

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.errors import CanonicalRevisionChanged
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)


@pytest.fixture(params=['memory', 'sqlite'])
def ready(request, tmp_path):
    fixture = make_fixture(tmp_path, request.param)
    yield fixture
    cleanup(fixture)


def test_catalog_matches_canonical_identity_without_materializing_an_account(ready):
    catalog = ready.source.analytics_snapshot(ACCOUNT)
    raw = HistoryAnalyticsSource(ready.repositories.history).account_read_model(ACCOUNT)
    assert catalog.identity == canonical_identity(raw)
    assert catalog.scanned_messages == 9
    assert len(catalog.digests) == 3
    assert ready.source.full_reads == 0
    for chat in catalog.digests:
        assert catalog.conversation(chat) == raw.conversations[chat]


def test_append_recomputes_one_conversation_and_preserves_independent_enrichment(ready, monkeypatch):
    first = ready.pipeline.project_account(ACCOUNT)
    assert ready.source.loaded == ['chat-0', 'chat-1', 'chat-2']
    cold_equal(ready, first.artifact)
    ready.source.loaded.clear()
    calls = []
    original = ready.pipeline.graph_projector.batches
    def project(account, revision, conversations, *args, **kwargs):
        calls.extend(c.conversation_id for c in conversations)
        return original(account, revision, conversations, *args, **kwargs)
    monkeypatch.setattr(ready.pipeline.graph_projector, 'batches', project)
    with ready.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'new', NOW-timedelta(hours=1), 4)
        advance(db)
    result = ready.pipeline.project_account(ACCOUNT)
    assert ready.source.loaded == calls == ['chat-1']
    assert [a.calls for a in ready.analyzers] == [10, 10, 10]
    assert ready.source.full_reads == 0
    cold_equal(ready, result.artifact)


def test_revision_only_rebuild_copies_all_parts_without_source_body_loads(ready):
    ready.pipeline.project_account(ACCOUNT)
    ready.source.loaded.clear()
    with ready.repositories.database.transaction() as db:
        advance(db)
    result = ready.pipeline.project_account(ACCOUNT)
    assert ready.source.loaded == []
    assert [a.calls for a in ready.analyzers] == [9, 9, 9]
    cold_equal(ready, result.artifact)
    with ready.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='synthetic changed price' WHERE message_id='m-1-1'")
        advance(db)
    ready.pipeline.project_account(ACCOUNT)
    assert [a.calls for a in ready.analyzers] == [10, 10, 10]


@pytest.mark.parametrize('change', ['edit', 'late', 'direction', 'participant', 'delete', 'conversation', 'same_revision'])
def test_changed_and_deleted_sources_converge(ready, change):
    ready.pipeline.project_account(ACCOUNT)
    ready.source.loaded.clear()
    with ready.repositories.database.transaction() as db:
        if change in {'edit', 'same_revision'}:
            db.execute("UPDATE account_messages SET text='synthetic support issue' WHERE message_id='m-1-1'")
        elif change == 'late':
            insert_message(db, 'chat-1', 'late', NOW-timedelta(days=9), 4)
        elif change == 'direction':
            db.execute("UPDATE account_messages SET direction='outbound' WHERE message_id='m-1-2'")
        elif change == 'participant':
            db.execute("UPDATE account_chats SET platform_user_id='synthetic-other-person' WHERE chat_id='chat-1'")
        elif change == 'delete':
            db.execute("DELETE FROM account_messages WHERE message_id='m-1-1'")
        else:
            db.execute("DELETE FROM account_messages WHERE chat_id='chat-1'")
            db.execute("DELETE FROM account_chats WHERE chat_id='chat-1'")
        if change != 'same_revision':
            advance(db)
    result = ready.pipeline.project_account(ACCOUNT)
    assert ready.source.loaded == ([] if change == 'conversation' else ['chat-1'])
    assert ready.source.full_reads == 0
    cold_equal(ready, result.artifact)
    if change == 'conversation':
        links = [e for e in result.artifact.edges if e.properties.get('scope') == 'conversation']
        assert len(links) == 1
        assert len(result.artifact.projection.conversation_metrics) == 2


def test_late_source_change_invalidates_catalog_instead_of_mixing_versions(ready):
    catalog = ready.source.analytics_snapshot(ACCOUNT)
    with ready.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='synthetic replacement' WHERE chat_id='chat-1'")
    with pytest.raises(CanonicalRevisionChanged):
        catalog.conversation('chat-1')


def test_stale_candidate_cannot_publish_or_supply_parts(ready):
    ready.pipeline.project_account(ACCOUNT)
    with ready.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'new', NOW-timedelta(hours=1), 4)
        advance(db)
    candidate = ready.pipeline.build_candidate(ACCOUNT)
    with ready.repositories.database.transaction() as db:
        db.execute("DELETE FROM account_messages WHERE message_id='new'")
        advance(db)
    with pytest.raises(CanonicalRevisionChanged):
        ready.pipeline.publish_candidate(candidate)
    ready.pipeline.discard_candidate(candidate)
    result = ready.pipeline.project_account(ACCOUNT)
    cold_equal(ready, result.artifact)


def test_expiry_without_new_ingestion_rebuilds_only_permitted_sources(ready):
    with ready.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET sent_at=? WHERE message_id='m-0-0'",
                   ((NOW-timedelta(days=89)).isoformat(),))
    ready.pipeline.project_account(ACCOUNT)
    ready.clock.now += timedelta(days=2)
    result = ready.pipeline.project_account(ACCOUNT)
    assert len(result.artifact.projection.message_enrichments) == 8
    cold_equal(ready, result.artifact)


def test_corrupt_fragment_falls_back_to_recomputation(ready, monkeypatch):
    ready.pipeline.project_account(ACCOUNT)
    ready.source.loaded.clear()
    monkeypatch.setattr(ready.stores.projections, 'open_conversation_fragments', lambda _: nullcontext(lambda *a, **k: b'{}'))
    result = ready.pipeline.rebuild_account(ACCOUNT)
    assert ready.source.loaded == ['chat-0', 'chat-1', 'chat-2']
    cold_equal(ready, result.artifact)


def test_fragment_capacity_does_not_limit_analysis(ready, monkeypatch):
    import app.analytics.conversation_reuse as reuse
    monkeypatch.setattr(reuse, 'MAX_FRAGMENT_TOTAL_BYTES', 1)
    first = ready.pipeline.project_account(ACCOUNT)
    ready.source.loaded.clear()
    result = ready.pipeline.rebuild_account(ACCOUNT)
    assert ready.source.loaded == ['chat-0', 'chat-1', 'chat-2']
    assert result.artifact == first.artifact


def test_identical_native_ids_and_text_do_not_cross_account_cache_boundaries(ready):
    ready.pipeline.project_account(ACCOUNT)
    other = 'synthetic-separate-owner'
    with ready.repositories.database.transaction() as db:
        for table in ('account_heads', 'account_chats', 'account_messages'):
            columns = [str(row[1]) for row in db.execute(f'PRAGMA table_info({table})')]
            selected = ','.join('?' if name == 'creator_account_id' else '"'+name+'"' for name in columns)
            db.execute(f'INSERT INTO {table} SELECT {selected} FROM {table} WHERE creator_account_id=?',
                       (other, ACCOUNT))
    ready.source.loaded.clear()
    result = ready.pipeline.project_account(other)
    assert ready.source.loaded == ['chat-0', 'chat-1', 'chat-2']
    assert [a.calls for a in ready.analyzers] == [18, 18, 18]
    from app.analytics.opaque_refs import account_ref
    assert all(n.account_ref == account_ref(other) for n in result.artifact.nodes)


def test_source_cancellation_closes_its_read_connection(ready):
    from app.analytics.errors import ProjectionBuildCancelled
    from app.persistence.database import LocalSQLite
    with pytest.raises(ProjectionBuildCancelled):
        ready.source.analytics_snapshot(ACCOUNT, cancellation_check=lambda: True)
    assert LocalSQLite.open_connection_count(ready.repositories.database.path) == 0
    assert ready.pipeline.project_account(ACCOUNT).changed


def test_build_retries_a_source_change_before_staging(ready, monkeypatch):
    ready.pipeline.project_account(ACCOUNT)
    with ready.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='synthetic first edit' WHERE message_id='m-1-1'")
        advance(db)
    original = ready.source.conversation_read_model
    changed = False
    def concurrent_edit(account, conversation, **kwargs):
        nonlocal changed
        value = original(account, conversation, **kwargs)
        if not changed:
            changed = True
            with ready.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='synthetic second edit' WHERE message_id='m-1-1'")
                advance(db)
        return value
    monkeypatch.setattr(ready.source, 'conversation_read_model', concurrent_edit)
    result = ready.pipeline.project_account(ACCOUNT)
    assert result.attempts == 2 and result.artifact.projection.source_revision == 3
    cold_equal(ready, result.artifact)


@pytest.mark.parametrize('in_transaction', [False, True])
def test_borrowed_identity_read_preserves_the_callers_cursor_and_transaction(ready, in_transaction):
    manager = ready.repositories.database.transaction if in_transaction else ready.repositories.database.read
    with manager() as connection:
        cursor = connection.execute('SELECT chat_id FROM account_chats ORDER BY chat_id')
        assert next(cursor)[0] == 'chat-0'
        borrowed = HistoryAnalyticsSource(ready.repositories.history, connection=connection)
        assert borrowed.read_identity(ACCOUNT) == ready.source.read_identity(ACCOUNT)
        assert connection.in_transaction is in_transaction
        assert [row[0] for row in cursor] == ['chat-1', 'chat-2']
