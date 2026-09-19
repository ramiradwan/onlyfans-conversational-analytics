"""Source-catalog reuse must follow live canonical changes and expiry."""

from unittest.mock import Mock
import pytest

from app.analytics.catalog_cache import SourceCatalogCache, MAX_CATALOG_BYTES
from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.identity import canonical_identity, CanonicalIdentity
from app.analytics.source_tokens import SourceIdentityCache, SourceToken
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    import app.analytics.source_snapshot as module
    value = make_fixture(tmp_path)
    value.scan = Mock(wraps=module.scan_identity)
    monkeypatch.setattr(module, 'scan_identity', value.scan)
    yield value
    cleanup(value)


def test_unchanged_catalog_has_a_fixed_expiry(fixture):
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    first = fixture.source.analytics_snapshot(ACCOUNT)
    now[0] = 59
    second = fixture.source.analytics_snapshot(ACCOUNT)
    assert second.identity == first.identity and second.digests == first.digests
    assert second.scanned_messages == 0 and fixture.scan.call_count == 1
    now[0] = 60
    assert fixture.source.analytics_snapshot(ACCOUNT).scanned_messages == 9
    assert fixture.scan.call_count == 2


@pytest.mark.parametrize('sql', [
    "UPDATE account_messages SET text='Changed' WHERE message_id='m-1-1'",
    "DELETE FROM account_messages WHERE message_id='m-1-1'",
    "UPDATE account_chats SET platform_user_id='different' WHERE chat_id='chat-1'",
    "UPDATE account_heads SET canonical_revision=canonical_revision+1",
])
def test_canonical_mutations_invalidate_cached_catalog(fixture, sql):
    fixture.source.analytics_snapshot(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute(sql)
    current = fixture.source.analytics_snapshot(ACCOUNT)
    assert fixture.scan.call_count == 2
    assert current.identity == canonical_identity(fixture.source.account_read_model(ACCOUNT))


def test_returned_catalog_does_not_mutate_cached_catalog(fixture):
    first = fixture.source.analytics_snapshot(ACCOUNT)
    original = dict(first.digests)
    first.digests.clear()
    assert fixture.source.analytics_snapshot(ACCOUNT).digests == original
    assert fixture.scan.call_count == 1


def test_refresh_does_not_rescan_a_valid_identity(fixture):
    fixture.source.analytics_snapshot(ACCOUNT)
    fixture.source.refresh_identity_cache(ACCOUNT)
    assert fixture.scan.call_count == 1


def test_supplied_transactions_never_reuse_a_committed_catalog(fixture):
    fixture.source.analytics_snapshot(ACCOUNT)
    with fixture.repositories.database.read() as db:
        db.execute('BEGIN')
        db.execute("UPDATE account_messages SET text='Uncommitted'")
        source = HistoryAnalyticsSource(fixture.repositories.history, connection=db)
        current = source.analytics_snapshot(ACCOUNT)
        assert current.identity != fixture.source.read_identity(ACCOUNT)
        assert db.in_transaction
        db.rollback()
    assert fixture.scan.call_count == 2


def test_missing_source_tracking_requires_rescanning(fixture):
    fixture.source.analytics_snapshot(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute('DROP TRIGGER analytics_source_token_account_messages_update')
    fixture.source.analytics_snapshot(ACCOUNT)
    fixture.source.analytics_snapshot(ACCOUNT)
    assert fixture.scan.call_count == 3


def test_catalogs_are_bounded_and_account_scoped():
    identities = SourceIdentityCache()
    catalogs = SourceCatalogCache(identities)
    token = SourceToken(1, 'a'*32, 1)
    identity = CanonicalIdentity(1, 'sha256:'+'b'*64)
    identities.put('owner', token, identity)
    values = {'conversation': 'sha256:'+'c'*64}
    catalogs.put('owner', token, identity, values)
    values.clear()
    assert catalogs.get('owner', token)[1]
    assert catalogs.get('other-owner', token) is None
    identities.clear()
    assert catalogs.get('owner', token) is None


def test_aggregate_catalog_budget_is_enforced(monkeypatch):
    import app.analytics.catalog_cache as module
    monkeypatch.setattr(module, 'MAX_CATALOG_BYTES', 128)
    identities = SourceIdentityCache()
    catalogs = SourceCatalogCache(identities)
    token = SourceToken(1, 'a'*32, 1)
    identity = CanonicalIdentity(1, 'sha256:'+'b'*64)
    for index in range(12):
        account = str(index)
        identities.put(account, token, identity)
        catalogs.put(account, token, identity, {'conversation': identity.content_digest})
        assert catalogs.bytes_used <= 128
        assert len(catalogs.entries) <= 1
    assert catalogs.get('0', token) is None
    assert catalogs.get('11', token) is not None
