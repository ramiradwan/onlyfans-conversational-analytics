"""Check exact identity reuse without treating account revision as content identity."""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.identity import CanonicalIdentity, canonical_identity
from app.analytics.source_tokens import SourceIdentityCache, SourceToken, MAX_IDENTITIES
from app.persistence.retention import CreatorVaultRetention
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    import app.analytics.source_snapshot as snapshots
    f = make_fixture(tmp_path)
    scan = Mock(wraps=snapshots.scan_identity)
    monkeypatch.setattr(snapshots, 'scan_identity', scan)
    f.scan = scan
    yield f
    cleanup(f)


def test_unchanged_identity_is_reused_but_not_source_records(fixture):
    first = fixture.source.read_identity(ACCOUNT)
    assert fixture.source.read_identity(ACCOUNT) == first
    assert fixture.scan.call_count == 1
    assert first == canonical_identity(fixture.source.account_read_model(ACCOUNT))


@pytest.mark.parametrize('sql', [
    "UPDATE account_messages SET text='Changed' WHERE message_id='m-1-1'",
    "UPDATE account_messages SET direction='outbound' WHERE message_id='m-1-1'",
    "UPDATE account_messages SET sent_at='2026-09-17T12:00:00+00:00' WHERE message_id='m-1-1'",
    "UPDATE account_messages SET sender_platform_user_id='changed' WHERE message_id='m-1-1'",
    "UPDATE account_messages SET winning_source_seq=70 WHERE message_id='m-1-1'",
    "UPDATE account_messages SET is_deleted=1 WHERE message_id='m-1-1'",
    "DELETE FROM account_messages WHERE message_id='m-1-1'",
    "UPDATE account_chats SET platform_user_id='changed' WHERE chat_id='chat-1'",
    "UPDATE account_chats SET display_name='Changed' WHERE chat_id='chat-1'",
    "UPDATE account_chats SET is_deleted=1 WHERE chat_id='chat-1'",
    "UPDATE account_heads SET canonical_revision=canonical_revision+1",
])
def test_source_mutations_invalidate_even_without_a_revision_change(fixture, sql):
    fixture.source.read_identity(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute(sql)
    actual = fixture.source.read_identity(ACCOUNT)
    assert fixture.scan.call_count == 2
    assert actual == canonical_identity(fixture.source.account_read_model(ACCOUNT))


def test_publication_bookkeeping_does_not_invalidate_source_content(fixture):
    identity = fixture.source.read_identity(ACCOUNT)
    fixture.pipeline.open_publication_epoch('synthetic-writer')
    assert fixture.source.read_identity(ACCOUNT) == identity
    assert fixture.scan.call_count == 1


@pytest.mark.parametrize('kind,target', [('message','m-1-1'), ('conversation','chat-1'), ('participant','synthetic-fan'), ('all',None)])
def test_creator_deletion_invalidates_identity(fixture, kind, target):
    fixture.source.read_identity(ACCOUNT)
    retention = CreatorVaultRetention(fixture.repositories.database, clock=lambda: NOW)
    method = getattr(retention, 'delete_' + kind)
    method(ACCOUNT, target) if target else method(ACCOUNT)
    assert fixture.source.read_identity(ACCOUNT) == canonical_identity(fixture.source.account_read_model(ACCOUNT))
    assert fixture.scan.call_count == 2


def test_rollback_cannot_poison_a_committed_identity(fixture):
    first = fixture.source.read_identity(ACCOUNT)
    with fixture.repositories.database.read() as db:
        db.execute('BEGIN')
        db.execute("UPDATE account_messages SET text='Uncommitted'")
        supplied = HistoryAnalyticsSource(fixture.repositories.history, connection=db)
        assert supplied.read_identity(ACCOUNT) != first
        assert db.in_transaction
        db.rollback()
    assert fixture.source.read_identity(ACCOUNT) == first
    assert fixture.scan.call_count == 2


def test_missing_tracking_trigger_disables_reuse(fixture):
    fixture.source.read_identity(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute('DROP TRIGGER analytics_source_token_account_messages_update')
        db.execute("UPDATE account_messages SET text='Changed without trigger'")
    assert fixture.source.read_identity(ACCOUNT) == canonical_identity(fixture.source.account_read_model(ACCOUNT))
    fixture.source.read_identity(ACCOUNT)
    assert fixture.scan.call_count == 3


def test_bounded_cache_expires_without_sliding_reads():
    now = [0.0]
    cache = SourceIdentityCache(monotonic=lambda: now[0])
    token = SourceToken(1, 'a'*32, 1)
    identity = CanonicalIdentity(1, 'sha256:'+'b'*64)
    cache.put(ACCOUNT, token, identity)
    now[0] = 59
    assert cache.get(ACCOUNT, token) == identity
    now[0] = 60
    assert cache.get(ACCOUNT, token) is None
    for index in range(MAX_IDENTITIES+1):
        cache.put(str(index), token, identity)
    assert cache.get('0', token) is None
    assert cache.get(str(MAX_IDENTITIES), token) == identity
    assert cache.get(ACCOUNT, None) is None
    cache.clear()
    assert cache.get(str(MAX_IDENTITIES), token) is None


def test_account_keys_do_not_share_cached_identities():
    cache = SourceIdentityCache()
    token = SourceToken(1, 'a'*32, 1)
    cache.put(ACCOUNT, token, CanonicalIdentity(1, 'sha256:'+'b'*64))
    assert cache.get('another-account', token) is None


def test_change_during_cached_question_verification_is_rejected(fixture, monkeypatch):
    from app.analytics.errors import ProjectionUnavailable
    from app.analytics.query_execution import QuestionBudget, QuestionLimits
    fixture.source.read_identity(ACCOUNT)
    original = fixture.source._identity_cache.get
    def changed(account, token):
        result = original(account, token)
        with fixture.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='Concurrent edit' WHERE message_id='m-1-1'")
        return result
    monkeypatch.setattr(fixture.source._identity_cache, 'get', changed)
    with pytest.raises(ProjectionUnavailable):
        with fixture.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits())):
            pytest.fail('a changed source reached execution')


def test_same_account_recreation_gets_a_new_token(fixture):
    first = fixture.source.read_identity(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute('DELETE FROM account_heads WHERE creator_account_id=?', (ACCOUNT,))
        db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)',
            (ACCOUNT, 1, NOW.isoformat()))
    assert fixture.source.read_identity(ACCOUNT) == first
    assert fixture.scan.call_count == 2
