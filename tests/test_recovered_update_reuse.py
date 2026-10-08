"""Startup re-establishes reuse from verified contents, not stored proof claims."""
from types import SimpleNamespace

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.factory import create_analytics_stores
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.scheduling import InProcessProjectionScheduler
from app.models.analytics import AvailabilityStatus
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.mark.asyncio
async def test_reopened_scheduler_prepares_verified_reuse_without_analyzers(tmp_path, monkeypatch):
    original = make_fixture(tmp_path)
    original.pipeline.project_account(ACCOUNT)
    cleanup(original)
    from app.analytics import sqlite_projection_store
    from app.core.lifecycle_receipts import StartupTrace
    trace = StartupTrace('real-reopened-process');trace.start()
    real = sqlite_projection_store.recompute_generation
    calls = []
    def observed(*args, **kwargs):
        calls.append(dict(kwargs));return real(*args, **kwargs)
    monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', observed)
    source = HistoryAnalyticsSource(original.repositories.history)
    stores = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
        activation=original.repositories.projection_activation,
        canonical_identity_reader=source.read_identity, retention_clock=lambda: NOW, lazy=True)
    pipeline = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=original.pipeline.enrichment, clock=lambda: NOW)
    scheduler = InProcessProjectionScheduler(pipeline)
    before = [a.calls for a in original.analyzers]
    try:
        await scheduler.start(recover=True)
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        store = stores.projections._store
        with store.database.read() as db:
            row = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
            assert store._trusted_graph_segment_proof(db, row) is not None
            assert store._trusted_conversation_graph_proof(db, row) is not None
            assert store._trusted_conversation_enrichment_proof(db, row) is not None
            envelope = store._trusted_verification_envelope(db, row)
            assert envelope is not None
            assert envelope.conversations.membership_prefixes
        assert [a.calls for a in original.analyzers] == before
        assert len(calls) == 1 and calls[0]['materialize_projection'] is False
        summary = trace.finish()
        assert summary['complete'] and summary['counters']['persisted_recomputations'] == 1
        assert summary['counters'].get('product_builds', 0) == 0
        assert summary['counters'].get('canonical_graph_batches', 0) == 0
        assert summary['counters'].get('enrichment_batches', 0) == 0
        assert summary['counters']['startup_handoff_hits'] == 1
        assert not store._startup_verifications
    finally:
        trace.finish(False)
        assert await scheduler.close(timeout=10)


def reopened(tmp_path, maker=make_fixture, *, startup_handoff=False):
    f = maker(tmp_path)
    f.pipeline.project_account(ACCOUNT)
    cleanup(f)
    source = HistoryAnalyticsSource(f.repositories.history)
    stores = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
        activation=f.repositories.projection_activation,
        canonical_identity_reader=source.read_identity, retention_clock=lambda: NOW)
    pipeline = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    if not startup_handoff:
        stores.projections._startup_verifications.clear()  # Exercise the existing post-open fallback explicitly.
    return f, source, stores, pipeline


def test_recovery_envelope_does_not_rebuild_expected_conversation_graphs(tmp_path, monkeypatch):
    from app.analytics import recovered_reuse
    f, source, stores, pipeline = reopened(tmp_path)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError('v2 restart recovery rebuilt expected canonical conversation graphs')
        monkeypatch.setattr(recovered_reuse, 'expected_units', forbidden)
        assert pipeline.prepare_questions(ACCOUNT, 1)
        identity = source.read_identity(ACCOUNT)
        assert stores.projections.update_reuse_prepared(ACCOUNT, identity)
    finally:
        stores.projections.close_retention_scheduler()


def test_recovery_preparation_reuses_only_independently_checked_bytes(tmp_path, monkeypatch):
    from app.analytics import sqlite_projection_store
    f, source, stores, pipeline = reopened(tmp_path)
    try:
        verify = sqlite_projection_store.recompute_generation
        checks = []
        def observed(*args, **kwargs):
            checks.append(dict(kwargs))
            return verify(*args, **kwargs)
        monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', observed)
        assert pipeline.prepare_questions(ACCOUNT, 1)
        before = [a.calls for a in f.analyzers]
        assert checks
        assert checks[0].get('materialize_projection') is False
        assert pipeline.prepare_questions(ACCOUNT, 1)
        assert len(checks) == 1
        assert [a.calls for a in f.analyzers] == before
    finally:
        stores.projections.close_retention_scheduler()


@pytest.mark.parametrize('fault', ['source', 'expiry', 'witness', 'storage'])
def test_change_at_end_of_recovery_cannot_install_proofs(tmp_path, monkeypatch, fault):
    from datetime import timedelta
    from app.analytics import sqlite_projection_store
    f, source, stores, pipeline = reopened(tmp_path)
    expected = sqlite_projection_store.recompute_generation
    def changed(*args, **kwargs):
        result = expected(*args, **kwargs)
        if fault == 'source':
            with f.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='Changed during preparation'")
        elif fault == 'expiry':
            monkeypatch.setattr(pipeline, '_retention_clock', lambda: NOW + timedelta(days=91))
        elif fault == 'witness':
            monkeypatch.setattr(stores.projections.activation, 'get', lambda _: None)
        else:
            with stores.database.transaction() as db:
                db.execute('UPDATE generation_content_epoch SET value=value+1')
        return result
    monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', changed)
    try:
        assert not pipeline.prepare_questions(ACCOUNT, 1)
        assert not stores.projections._graph_segment_proofs
        assert not stores.projections._conversation_graph_proofs
        assert not stores.projections._conversation_enrichment_proofs
    finally:
        stores.projections.close_retention_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("dominant", [False, True])
async def test_reopened_scheduler_updates_with_verified_units(tmp_path, dominant, monkeypatch):
    from datetime import timedelta
    from tests.continuous_analytics_fixture import insert_message, advance, cold_equal
    from tests.test_dominant_append_reuse import dominant_fixture
    f, source, stores, pipeline = reopened(tmp_path, dominant_fixture if dominant else make_fixture)
    stores.projections.close_retention_scheduler()
    lazy = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
        activation=f.repositories.projection_activation,
        canonical_identity_reader=source.read_identity, retention_clock=lambda: NOW, lazy=True)
    pipeline = AnalyticsPipeline(source, projections=lazy.projections,
        enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    scheduler = InProcessProjectionScheduler(pipeline)
    try:
        await scheduler.start(recover=True)
        before = [a.calls for a in f.analyzers]
        if dominant:
            from app.analytics import conversation_append, conversation_integrity
            def forbidden_rows(*args, **kwargs):
                raise AssertionError('recovered dominant append materialized historical enrichment rows')
            def forbidden_members(*args, **kwargs):
                raise AssertionError('recovered dominant append revalidated predecessor membership frame')
            monkeypatch.setattr(conversation_append, 'message_records', forbidden_rows)
            monkeypatch.setattr(conversation_integrity, 'groups_for_members', forbidden_members)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'after-restart', NOW-timedelta(hours=1), 2)
            advance(db)
        await scheduler.schedule(ACCOUNT, 2)
        state = await scheduler.wait(ACCOUNT)
        assert state.availability == AvailabilityStatus.AVAILABLE, state
        assert [a.calls-n for a, n in zip(f.analyzers, before)] == [1, 1, 1]
        f.source = source
        cold_equal(f, lazy.projections.get_artifact(ACCOUNT))
    finally:
        assert await scheduler.close(timeout=10)


def test_concurrent_preparation_shares_one_account_verification(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.analytics import sqlite_projection_store
    f, source, stores, pipeline = reopened(tmp_path)
    started, release = Event(), Event()
    original, calls = sqlite_projection_store.recompute_generation, []
    def observe(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(pipeline.prepare_questions, ACCOUNT, 1)
            try:
                assert started.wait(5)
                second = pool.submit(pipeline.prepare_questions, ACCOUNT, 1)
                import time
                deadline = time.monotonic() + 5
                while (len(calls) < 2 and pipeline._account_locks.get(ACCOUNT, (None, 0))[1] < 2
                       and time.monotonic() < deadline):
                    time.sleep(0.001)
                assert len(calls) == 2 or pipeline._account_locks[ACCOUNT][1] == 2
            finally:
                release.set()
            assert first.result(timeout=10) and second.result(timeout=10)
        assert len(calls) == 1
    finally:
        release.set()
        stores.projections.close_retention_scheduler()


def test_cancelled_preparation_waiter_does_not_scan_or_keep_account_lock(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.analytics.errors import ProjectionBuildCancelled
    f, source, stores, pipeline = reopened(tmp_path)
    cancelled, entered = Event(), Event()
    def wait_for_owner():
        entered.set()
        return pipeline.prepare_questions(ACCOUNT, 1, cancellation_check=cancelled.is_set)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pipeline._account_lock(ACCOUNT):
                future = pool.submit(wait_for_owner)
                assert entered.wait(5)
                cancelled.set()
                with pytest.raises(ProjectionBuildCancelled):
                    future.result(timeout=5)
                assert pipeline._account_locks[ACCOUNT][1] == 1
            assert not pipeline._account_locks
        assert not stores.projections._graph_segment_proofs
    finally:
        stores.projections.close_retention_scheduler()


def test_recovery_is_cancelled_before_installing_reuse(tmp_path, monkeypatch):
    from threading import Event
    from app.analytics.errors import ProjectionBuildCancelled
    from app.analytics import sqlite_projection_store
    f, source, stores, pipeline = reopened(tmp_path)
    stop = Event()
    original = sqlite_projection_store.recompute_generation
    def cancel_during_verification(*args, **kwargs):
        stop.set()
        kwargs['check']()
        raise AssertionError('cancelled recovery continued verification')
    monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', cancel_during_verification)
    try:
        with pytest.raises(ProjectionBuildCancelled):
            pipeline.prepare_questions(ACCOUNT, 1, cancellation_check=stop.is_set)
        assert not stores.projections._graph_segment_proofs
        assert not stores.projections._conversation_graph_proofs
        assert not stores.projections._conversation_enrichment_proofs
        assert not stores.projections._verification_envelopes
        assert not pipeline._account_locks
    finally:
        stores.projections.close_retention_scheduler()


@pytest.mark.parametrize('fault', ['epoch', 'schema', 'witness', 'expired', 'replacement', 'source', 'retention', 'pipeline'])
def test_startup_handoff_rechecks_live_state(tmp_path, monkeypatch, fault):
    from dataclasses import replace
    from datetime import timedelta
    from app.analytics import sqlite_projection_store
    f, source, stores, pipeline = reopened(tmp_path, startup_handoff=True)
    store = stores.projections
    try:
        assert len(store._startup_verifications) == 1
        key, candidate = next(iter(store._startup_verifications.items()))
        assert not store._verification_envelopes and not store._graph_segment_proofs
        if fault == 'epoch':
            with store.database.transaction() as db:db.execute('UPDATE generation_content_epoch SET value=value+1')
        elif fault == 'schema':
            with store.database.transaction() as db:db.execute('CREATE INDEX extra_test_index ON projection_generations(status)')
        elif fault == 'witness':
            get = store.activation.get
            monkeypatch.setattr(store.activation, 'get', lambda value: replace(get(value), completed_at=NOW-timedelta(days=1)))
        elif fault == 'expired':
            store._startup_verifications[key] = replace(candidate, receipt=replace(candidate.receipt, expires_at=0.0))
        elif fault == 'replacement':
            store._startup_verifications[key] = replace(candidate, database_identity=(-1,-1))
        elif fault == 'source':
            with f.repositories.database.transaction() as db:db.execute("UPDATE account_messages SET text='changed without revision'")
        elif fault == 'retention':monkeypatch.setattr(pipeline, '_retention_clock', lambda: NOW+timedelta(days=91))
        else:monkeypatch.setattr(pipeline, 'pipeline_config_digest', 'sha256:'+'0'*64)
        calls=[];real=sqlite_projection_store.recompute_generation
        def observed(*args, **kwargs):calls.append(1);return real(*args, **kwargs)
        monkeypatch.setattr(sqlite_projection_store, 'recompute_generation', observed)
        ready=pipeline.prepare_questions(ACCOUNT, 1)
        if fault in ('source','retention','pipeline'):
            assert not ready and not store._verification_envelopes
        else:
            assert ready and len(calls)==1
        assert key not in store._startup_verifications or not ready
    finally:
        stores.projections.close_retention_scheduler()


def test_handoff_is_one_use_bounded_and_discarded_on_close(tmp_path):
    from dataclasses import replace
    from app.analytics.validation_receipt import MAX_RECEIPTS
    f, source, stores, pipeline = reopened(tmp_path, startup_handoff=True)
    store=stores.projections
    try:
        candidate=next(iter(store._startup_verifications.values()))
        assert not hasattr(candidate,'message_enrichments') and not hasattr(candidate,'nodes')
        assert sum(len(x[1])+len(x[2]) for x in candidate.envelope.conversations.membership_prefixes) <= 2*1024*1024
        with store.database.read() as db:
            row=db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
            assert store._take_startup_verification(db,row,store.activation.get(row['generation_id'])) is candidate
            assert store._take_startup_verification(db,row,store.activation.get(row['generation_id'])) is None
        store._startup_verifications[candidate.receipt.generation_id]=candidate
        store.close()
        assert not store._startup_verifications
    finally:
        stores.projections.close_retention_scheduler()


def test_cancellation_before_atomic_handoff_install_leaves_no_proofs(tmp_path, monkeypatch):
    from threading import Event
    from app.analytics.errors import ProjectionBuildCancelled
    f, source, stores, pipeline=reopened(tmp_path,startup_handoff=True)
    store=stores.projections;stop=Event();original=store._install_recovery_envelope
    def cancel(envelope,check,**kwargs):
        stop.set();return original(envelope,check,**kwargs)
    monkeypatch.setattr(store,'_install_recovery_envelope',cancel)
    try:
        with pytest.raises(ProjectionBuildCancelled):pipeline.prepare_questions(ACCOUNT,1,cancellation_check=stop.is_set)
        assert not any((store._startup_verifications,store._verification_envelopes,store._graph_segment_proofs,store._conversation_graph_proofs,store._conversation_enrichment_proofs))
        assert not pipeline._account_locks
    finally:
        stores.projections.close_retention_scheduler()


def test_pending_handoff_bound_and_expiry_are_not_renewed(tmp_path):
    from dataclasses import replace
    from app.analytics.validation_receipt import MAX_RECEIPTS
    f, source, stores, pipeline = reopened(tmp_path, startup_handoff=True)
    store=stores.projections
    try:
        original=next(iter(store._startup_verifications.values()))
        # Feed independently checked metadata through the production retention
        # boundary with distinct observed rows/witnesses, not through cache writes.
        values={'graph_segments':original.envelope.graph.segments,
                'enrichment_units':original.envelope.enrichment.headers,
                'conversation_integrity':original.envelope.conversations}
        for index in range(MAX_RECEIPTS+3):
            row=dict(original.row);row['generation_id']='test-generation-'+str(index)
            witness=replace(original.witness,generation_id=row['generation_id'])
            store._retain_startup_verification(row,witness,original.receipt.stamp,values)
        assert len(store._startup_verifications)<=MAX_RECEIPTS
        latest=next(reversed(store._startup_verifications.values()))
        assert latest.receipt.expires_at>=original.receipt.expires_at
        # Inspection does not extend an already issued candidate's lifetime.
        expiry=latest.receipt.expires_at
        assert store._startup_verifications[latest.receipt.generation_id].receipt.expires_at==expiry
        store.close()
        assert not store._startup_verifications
    finally:
        stores.projections.close_retention_scheduler()



def test_real_question_fixture_reuses_initial_full_validation(tmp_path, monkeypatch):
    from pathlib import Path
    from tools import analytics_qualification as q
    from tools.analytics_qualification_fixture import Workload
    from app.analytics import sqlite_projection_store
    manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
    root=tmp_path/'question-input';work=Workload(root,manifest,400,question_case='populated',known_kinds=True)
    candidate=work.f.pipeline.build_candidate(work.account,force=True)
    work.f.pipeline.publish_candidate(candidate)
    expected=work.verify()['expected']
    work.close_for_snapshot()
    calls=[];real=sqlite_projection_store.recompute_generation
    def observed(*args,**kwargs):calls.append(kwargs);return real(*args,**kwargs)
    monkeypatch.setattr(sqlite_projection_store,'recompute_generation',observed)
    opened=Workload(root,manifest,400,reopen=True,question_case='populated',known_kinds=True)
    try:
        opened.f.pipeline.ensure_projection_storage()
        store=opened.f.stores.projections._store
        assert len(store._startup_verifications)==1
        assert opened.f.pipeline.prepare_questions(opened.account,1)
        assert len(calls)==1 and calls[0]['materialize_projection'] is False
        assert len(store._verification_envelopes)==1 and not store._startup_verifications
        proof=next(iter(store._verification_envelopes.values()))
        from app.analytics.generation_verification import envelope_from_values
        from app.analytics.validation_receipt import ValidationReceipt
        # Empty graph segments are a valid verified representation. The helper
        # must not reject them merely for being empty; integrity is established
        # by the complete recomputation before this constructor is called.
        receipt=ValidationReceipt(proof.generation_id, proof.stamp, proof.binding, float('inf'))
        empty_graph=envelope_from_values(receipt, {'graph_segments':(),
            'enrichment_units':proof.enrichment.headers,'conversation_integrity':proof.conversations})
        assert empty_graph is not None and empty_graph.graph.segments==()
    finally:
        opened.close();opened.f.pipeline.close_projection_storage()
