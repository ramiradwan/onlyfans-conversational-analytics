"""Reuse only independently read canonical identities in witness transactions."""

from unittest.mock import Mock
import pytest

from dataclasses import replace

from app.analytics.identity import canonical_identity
from app.persistence.projection_activation import _sqlite_identity, _sqlite_identity_matches
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    import app.analytics.source_snapshot as snapshots
    value = make_fixture(tmp_path)
    value.scan = Mock(wraps=snapshots.scan_identity)
    monkeypatch.setattr(snapshots, 'scan_identity', value.scan)
    yield value
    cleanup(value)


def read(fixture, connection):
    return _sqlite_identity(connection, ACCOUNT,
        cache=fixture.repositories.projection_activation._verified_sources)


def test_repeated_transaction_read_keeps_its_independent_identity(fixture):
    with fixture.repositories.database.transaction() as db:
        first = read(fixture, db)
    with fixture.repositories.database.transaction() as db:
        assert read(fixture, db) == first
    assert fixture.scan.call_count == 1


@pytest.mark.parametrize('sql', [
    "UPDATE account_messages SET text='Modified' WHERE message_id='m-0-0'",
    "DELETE FROM account_messages WHERE message_id='m-0-0'",
    "UPDATE account_heads SET canonical_revision=canonical_revision+1",
    "UPDATE account_chats SET platform_user_id='other' WHERE chat_id='chat-0'",
])
def test_changes_in_the_current_transaction_invalidate_previous_proof(fixture, sql):
    with fixture.repositories.database.transaction() as db:
        first = read(fixture, db)
    with fixture.repositories.database.transaction() as db:
        db.execute(sql)
        assert read(fixture, db) != first
    assert fixture.scan.call_count == 2
    assert fixture.source.read_identity(ACCOUNT) == canonical_identity(fixture.source.account_read_model(ACCOUNT))


def test_rolled_back_changes_cannot_poison_witness_identity(fixture):
    with fixture.repositories.database.transaction() as db:
        first = read(fixture, db)
    with fixture.repositories.database.read() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE account_messages SET text='Uncommitted'")
        assert read(fixture, db) != first
        db.rollback()
    with fixture.repositories.database.transaction() as db:
        assert read(fixture, db) == first
    assert fixture.scan.call_count == 3


def test_missing_tracking_and_expiry_require_another_source_scan(fixture):
    cache = fixture.repositories.projection_activation._verified_sources
    now = [0.0]
    cache._clock = lambda: now[0]
    with fixture.repositories.database.transaction() as db:
        read(fixture, db)
    now[0] = 60
    with fixture.repositories.database.transaction() as db:
        read(fixture, db)
    assert fixture.scan.call_count == 2
    with fixture.repositories.database.transaction() as db:
        db.execute('DROP TRIGGER analytics_source_token_account_messages_update')
        read(fixture, db)
        read(fixture, db)
    assert fixture.scan.call_count == 4


def test_witness_cache_does_not_accept_a_caller_supplied_digest(fixture):
    fixture.source.analytics_snapshot(ACCOUNT)
    assert not fixture.repositories.projection_activation._verified_sources._entries
    with fixture.repositories.database.transaction() as db:
        result = read(fixture, db)
    assert result == canonical_identity(fixture.source.account_read_model(ACCOUNT))
    assert fixture.scan.call_count == 2


def test_witness_transaction_accepts_only_the_scanned_process_proof(fixture):
    snapshot = fixture.source.analytics_snapshot(ACCOUNT)
    cache = fixture.repositories.projection_activation._verified_sources
    with fixture.repositories.database.transaction() as db:
        assert _sqlite_identity_matches(
            db, ACCOUNT, snapshot.identity, cache=cache,
            proof=snapshot.identity_proof,
        )
    assert fixture.scan.call_count == 1

    forged = replace(snapshot.identity_proof, signature='0' * 64)
    cache.clear()
    with fixture.repositories.database.transaction() as db:
        assert _sqlite_identity_matches(
            db, ACCOUNT, snapshot.identity, cache=cache, proof=forged,
        )
    assert fixture.scan.call_count == 2


def test_witness_process_proof_rejects_a_new_transactional_source_token(fixture):
    snapshot = fixture.source.analytics_snapshot(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Changed witness source' WHERE message_id='m-0-0'")
        assert not _sqlite_identity_matches(
            db, ACCOUNT, snapshot.identity,
            cache=fixture.repositories.projection_activation._verified_sources,
            proof=snapshot.identity_proof,
        )
    assert fixture.scan.call_count == 1
