"""Generation verification envelopes reuse only constituent independently checked proofs."""
from types import SimpleNamespace

import pytest

from tests.continuous_analytics_fixture import ACCOUNT, NOW, insert_message, advance, cleanup, make_fixture


def test_live_envelope_skips_recovery_snapshot_after_currentness_refresh(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        assert f.pipeline.prepare_questions(ACCOUNT, 1)
        clock = [61.0]
        f.stores.projections._currentness.clock = lambda: clock[0]
        f.stores.projections._currentness.entries.clear()
        def forbidden(*args, **kwargs):
            raise AssertionError('live verification envelope launched full recovery')
        monkeypatch.setattr(f.stores.projections, 'prepare_update_reuse', forbidden)
        monkeypatch.setattr(f.source, 'analytics_snapshot', forbidden)
        assert f.pipeline.prepare_questions(ACCOUNT, 1)
    finally:
        cleanup(f)


@pytest.mark.parametrize('cache', [
    '_graph_segment_proofs', '_conversation_graph_proofs', '_conversation_enrichment_proofs',
])
def test_envelope_is_not_independent_authority(tmp_path, cache):
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        identity = f.source.read_identity(ACCOUNT)
        assert f.stores.projections.update_reuse_prepared(ACCOUNT, identity)
        getattr(f.stores.projections, cache).clear()
        assert not f.stores.projections.update_reuse_prepared(ACCOUNT, identity)
    finally:
        cleanup(f)


def test_envelope_survives_guarded_activation_and_noop_gc(tmp_path):
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'envelope-next-message', NOW, 2)
            advance(db)
        f.pipeline.project_account(ACCOUNT)
        identity = f.source.read_identity(ACCOUNT)
        assert f.stores.projections.update_reuse_prepared(ACCOUNT, identity)
    finally:
        cleanup(f)


def test_verified_integrity_groups_skip_unrelated_membership_payloads():
    from app.analytics.conversation_membership_validation import proven_unit_is_unchanged
    class NoReads:
        in_transaction = True
        def execute(self, *args, **kwargs):
            raise AssertionError('unrelated integrity bucket opened membership payload')
    header = SimpleNamespace(
        conversation_ref='c1:' + '1' * 64,
        unit_id='2' * 64,
        node_count=1,
        edge_count=0,
    )
    changed = {'node': {'g1:aa' + '3' * 62}, 'edge': set()}
    groups = (('node', 'bb', 1, '4' * 64, '5' * 64),)
    assert proven_unit_is_unchanged(
        NoReads(), 'generation', 'a1:' + '6' * 64, header, changed, lambda: None,
        integrity_groups=groups,
    )


def test_matching_integrity_bucket_still_requires_exact_membership_read():
    from app.analytics.conversation_membership_validation import proven_unit_is_unchanged
    class RequiredRead:
        in_transaction = True
        def execute(self, *args, **kwargs):
            raise RuntimeError('membership-read-observed')
    header = SimpleNamespace(
        conversation_ref='c1:' + '1' * 64,
        unit_id='2' * 64,
        node_count=1,
        edge_count=0,
    )
    changed = {'node': {'g1:aa' + '3' * 62}, 'edge': set()}
    groups = (('node', 'aa', 1, '4' * 64, '5' * 64),)
    with pytest.raises(RuntimeError, match='membership-read-observed'):
        proven_unit_is_unchanged(
            RequiredRead(), 'generation', 'a1:' + '6' * 64, header, changed, lambda: None,
            integrity_groups=groups,
        )


def test_integrity_bucket_summary_is_bounded_and_falls_back(tmp_path, monkeypatch):
    from app.analytics import conversation_graph_units as units
    monkeypatch.setattr(units, 'MAX_PROOF_BUCKET_KEYS', 1)
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        store = f.stores.projections
        identity = f.source.read_identity(ACCOUNT)
        assert store.update_reuse_prepared(ACCOUNT, identity)
        with store.database.read() as db:
            row = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
            envelope = store._trusted_verification_envelope(db, row)
        assert envelope is not None
        assert envelope.conversations.integrity_groups == ()
    finally:
        cleanup(f)


def test_prefix_summary_proves_unrelated_identity_absent_without_payload_read():
    from app.analytics.conversation_membership_validation import proven_unit_is_unchanged
    from app.analytics.membership_prefixes import bitmap
    class NoReads:
        in_transaction = True
        def execute(self, *args, **kwargs):
            raise AssertionError('negative prefix proof opened membership payload')
    header = SimpleNamespace(
        conversation_ref='c1:' + '1' * 64, unit_id='2' * 64,
        node_count=1, edge_count=0,
    )
    node_member = 'g1:aa11' + '3' * 60
    changed = {'node': {'g1:aa22' + '4' * 60}, 'edge': set()}
    prefixes = (bitmap((node_member,)), bitmap(()))
    assert proven_unit_is_unchanged(
        NoReads(), 'generation', 'a1:' + '6' * 64, header, changed, lambda: None,
        membership_prefixes=prefixes,
    )


def test_prefix_summary_positive_still_requires_exact_payload_read():
    from app.analytics.conversation_membership_validation import proven_unit_is_unchanged
    from app.analytics.membership_prefixes import bitmap
    class RequiredRead:
        in_transaction = True
        def execute(self, *args, **kwargs):
            raise RuntimeError('exact-membership-read-observed')
    header = SimpleNamespace(
        conversation_ref='c1:' + '1' * 64, unit_id='2' * 64,
        node_count=1, edge_count=0,
    )
    member = 'g1:aa22' + '3' * 60
    changed = {'node': {'g1:aa22' + '4' * 60}, 'edge': set()}
    prefixes = (bitmap((member,)), bitmap(()))
    with pytest.raises(RuntimeError, match='exact-membership-read-observed'):
        proven_unit_is_unchanged(
            RequiredRead(), 'generation', 'a1:' + '6' * 64, header, changed, lambda: None,
            membership_prefixes=prefixes,
        )


def test_membership_prefix_envelope_is_bounded_and_optional(tmp_path, monkeypatch):
    from app.analytics import membership_prefixes
    monkeypatch.setattr(membership_prefixes, 'MAX_ENVELOPE_PREFIX_BYTES', 1)
    f = make_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        store = f.stores.projections
        identity = f.source.read_identity(ACCOUNT)
        assert store.update_reuse_prepared(ACCOUNT, identity)
        with store.database.read() as db:
            row = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
            envelope = store._trusted_verification_envelope(db, row)
        assert envelope is not None
        assert envelope.conversations.membership_prefixes == ()
    finally:
        cleanup(f)


def test_lazy_currentness_forwards_cancellation_keyword(monkeypatch):
    from app.analytics.resilient_projection_store import LazySQLiteAnalyticsProjectionStore
    store = object.__new__(LazySQLiteAnalyticsProjectionStore)
    calls = []
    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return True
    monkeypatch.setattr(store, '_read', observed)
    marker = object()
    clock = lambda: None
    assert store.projection_currentness(
        'account-a', 'identity', 'revision', 'config', clock,
        cancellation_check=marker,
    )
    assert len(calls) == 1
    assert calls[0][0] == (
        'projection_currentness', 'account-a', 'account-a',
        'identity', 'revision', 'config', clock,
    )
    assert calls[0][1] == {'cancellation_check': marker}
