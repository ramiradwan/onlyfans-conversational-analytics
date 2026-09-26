"""Check exact identity reuse without treating account revision as content identity."""

from dataclasses import replace
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


def test_process_proof_rebinds_expired_identity_without_rescanning(fixture):
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    snapshot = fixture.source.analytics_snapshot(ACCOUNT)
    assert snapshot.identity_proof is not None and fixture.scan.call_count == 1
    now[0] = 61.0
    assert fixture.source.verify_identity_proof(
        ACCOUNT, snapshot.identity, snapshot.identity_proof
    )
    assert fixture.source.read_identity(ACCOUNT) == snapshot.identity
    assert fixture.scan.call_count == 1


def test_process_proof_detects_committed_source_change_without_rescanning(fixture):
    snapshot = fixture.source.analytics_snapshot(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Changed after proof' WHERE message_id='m-1-1'")
    assert fixture.source.verify_identity_proof(
        ACCOUNT, snapshot.identity, snapshot.identity_proof
    ) is False
    assert fixture.scan.call_count == 1
    assert fixture.source.read_identity(ACCOUNT) != snapshot.identity
    assert fixture.scan.call_count == 2


def test_forged_process_proof_falls_back_to_normal_identity_path(fixture):
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    snapshot = fixture.source.analytics_snapshot(ACCOUNT)
    forged = replace(snapshot.identity_proof, signature='0' * 64)
    now[0] = 61.0
    assert fixture.source.verify_identity_proof(ACCOUNT, snapshot.identity, forged) is None
    assert fixture.source.read_identity(ACCOUNT) == snapshot.identity
    assert fixture.scan.call_count == 2


def test_pipeline_process_proof_survives_cache_expiry_through_publication(
    fixture, monkeypatch
):
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    fixture.repositories.projection_activation._verified_sources._clock = lambda: now[0]
    original_build = fixture.pipeline._build

    def delayed_build(*args, **kwargs):
        artifact = original_build(*args, **kwargs)
        now[0] = 61.0
        return artifact

    monkeypatch.setattr(fixture.pipeline, '_build', delayed_build)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert fixture.scan.call_count == 1

    now[0] = 122.0
    result = fixture.pipeline.publish_candidate(candidate)
    assert result.changed
    assert fixture.scan.call_count == 1


def test_pipeline_process_proof_detects_change_before_publication(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert fixture.scan.call_count == 1
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Changed after build' WHERE message_id='m-1-1'")
    from app.analytics.errors import CanonicalRevisionChanged
    with pytest.raises(CanonicalRevisionChanged):
        fixture.pipeline.publish_candidate(candidate)
    assert fixture.scan.call_count == 1


def test_pipeline_proof_survives_identity_cache_expiry_during_build(
    fixture, monkeypatch
):
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    original = fixture.pipeline._build

    def delayed(*args, **kwargs):
        artifact = original(*args, **kwargs)
        now[0] = 61.0
        return artifact

    monkeypatch.setattr(fixture.pipeline, '_build', delayed)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result.changed
    assert fixture.scan.call_count == 1


def test_pipeline_proof_retries_when_source_token_changes_during_build(
    fixture, monkeypatch
):
    original = fixture.pipeline._build
    changed = [False]

    def mutate(*args, **kwargs):
        artifact = original(*args, **kwargs)
        if not changed[0]:
            changed[0] = True
            with fixture.repositories.database.transaction() as db:
                db.execute(
                    "UPDATE account_messages SET text='Changed during build' "
                    "WHERE message_id='m-1-1'"
                )
        return artifact

    monkeypatch.setattr(fixture.pipeline, '_build', mutate)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result.changed and changed[0]
    assert fixture.scan.call_count == 2
    assert result.artifact.projection.canonical_content_digest == (
        canonical_identity(fixture.source.account_read_model(ACCOUNT)).content_digest
    )


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
