"""A verified unchanged prefix does not repeat message-local graph construction."""
from datetime import timedelta

import pytest

from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, make_fixture, insert_message, advance, cleanup, cold_equal,
)


def dominant_fixture(path):
    f = make_fixture(path, conversations=3, messages=0)
    with f.repositories.database.transaction() as db:
        for i in range(1024):
            insert_message(db, 'chat-0', f'dominant-{i}', NOW - timedelta(hours=2) + timedelta(seconds=i), i)
        for chat in (1, 2):
            insert_message(db, f'chat-{chat}', f'small-{chat}', NOW - timedelta(hours=3), 0)
    return f


@pytest.mark.parametrize('small_cache', [False, True])
def test_append_builds_only_the_boundary_and_new_message(tmp_path, monkeypatch, small_cache):
    if small_cache:
        monkeypatch.setattr('app.analytics.enrichment_cache.MAX_CACHE_ENTRIES', 3)
        monkeypatch.setattr('app.analytics.enrichment_cache.MAX_CONVERSATION_CACHE_ENTRIES', 3)
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = [a.calls for a in f.analyzers]
        batches, built = f.pipeline.graph_projector.batches, []
        def traced(account, revision, conversations, findings, metrics, **kwargs):
            built.append(sum(len(c.messages) for c in conversations))
            yield from batches(account, revision, conversations, findings, metrics, **kwargs)
        monkeypatch.setattr(f.pipeline.graph_projector, 'batches', traced)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-last-message', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert built == [2], built
        assert [a.calls - count for a, count in zip(f.analyzers, before)] == [1, 1, 1]
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


@pytest.mark.parametrize('mutation', ['edit', 'late', 'delete', 'participant'])
def test_prefix_changes_fall_back_to_complete_conversation_work(tmp_path, mutation):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            if mutation == 'edit':
                db.execute("UPDATE account_messages SET text='Changed prefix' WHERE message_id='dominant-0'")
            elif mutation == 'late':
                insert_message(db, 'chat-0', 'late-message', NOW - timedelta(days=1), 2)
            elif mutation == 'delete':
                db.execute("DELETE FROM account_messages WHERE message_id='dominant-0'")
            else:
                db.execute("UPDATE account_chats SET platform_user_id='different-participant' WHERE chat_id='chat-0'")
            insert_message(db, 'chat-0', 'new-last-message', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


@pytest.mark.parametrize('text', ['Hello, can you help tomorrow?', 'Bad service https://example.test/help $12'])
def test_append_changed_topics_and_sentiment_keep_full_rebuild_semantics(tmp_path, text):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = [a.calls for a in f.analyzers]
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'different-finding', NOW, 3, text=text)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert [a.calls-n for a, n in zip(f.analyzers, before)] == [1, 1, 1]
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_expired_prefix_cannot_use_append_reuse(tmp_path):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        f.clock.now = NOW + timedelta(days=91)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'retained-new-message', f.clock.now, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert len(result.artifact.projection.message_enrichments) == 1
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_tied_timestamp_append_preserves_canonical_order(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'a-tied-tail', NOW, 2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        before = [a.calls for a in f.analyzers]
        batches, built = f.pipeline.graph_projector.batches, []
        def observe(account, revision, conversations, findings, metrics, **kwargs):
            built.append(sum(len(c.messages) for c in conversations))
            yield from batches(account, revision, conversations, findings, metrics, **kwargs)
        monkeypatch.setattr(f.pipeline.graph_projector, 'batches', observe)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'z-tied-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert built == [2]
        assert [a.calls-n for a, n in zip(f.analyzers, before)] == [1, 1, 1]
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)
