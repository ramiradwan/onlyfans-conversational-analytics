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
async def test_reopened_scheduler_prepares_verified_reuse_without_analyzers(tmp_path):
    original = make_fixture(tmp_path)
    original.pipeline.project_account(ACCOUNT)
    cleanup(original)
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
        assert [a.calls for a in original.analyzers] == before
    finally:
        assert await scheduler.close(timeout=10)


def reopened(tmp_path, maker=make_fixture):
    f = maker(tmp_path)
    f.pipeline.project_account(ACCOUNT)
    cleanup(f)
    source = HistoryAnalyticsSource(f.repositories.history)
    stores = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
        activation=f.repositories.projection_activation,
        canonical_identity_reader=source.read_identity, retention_clock=lambda: NOW)
    pipeline = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    return f, source, stores, pipeline


def test_recovery_preparation_reuses_only_independently_checked_bytes(tmp_path, monkeypatch):
    f, source, stores, pipeline = reopened(tmp_path)
    try:
        read = stores.projections._validate_persisted_generation
        checks = []
        monkeypatch.setattr(stores.projections, '_validate_persisted_generation',
            lambda *args, **kwargs: (checks.append(args), read(*args, **kwargs))[1])
        assert pipeline.prepare_questions(ACCOUNT, 1)
        before = [a.calls for a in f.analyzers]
        assert checks
        assert pipeline.prepare_questions(ACCOUNT, 1)
        assert [a.calls for a in f.analyzers] == before
    finally:
        stores.projections.close_retention_scheduler()


@pytest.mark.parametrize('fault', ['source', 'expiry', 'witness', 'storage'])
def test_change_at_end_of_recovery_cannot_install_proofs(tmp_path, monkeypatch, fault):
    from datetime import timedelta
    from app.analytics import recovered_reuse
    f, source, stores, pipeline = reopened(tmp_path)
    expected = recovered_reuse.expected_units
    def changed(*args):
        yield from expected(*args)
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
    monkeypatch.setattr(recovered_reuse, 'expected_units', changed)
    try:
        assert not pipeline.prepare_questions(ACCOUNT, 1)
        assert not stores.projections._graph_segment_proofs
        assert not stores.projections._conversation_graph_proofs
        assert not stores.projections._conversation_enrichment_proofs
    finally:
        stores.projections.close_retention_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("dominant", [False, True])
async def test_reopened_scheduler_updates_with_verified_units(tmp_path, dominant):
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
    from app.analytics import recovered_reuse
    f, source, stores, pipeline = reopened(tmp_path)
    started, release = Event(), Event()
    original, calls = recovered_reuse.expected_units, []
    def observe(*args):
        calls.append(1)
        started.set()
        assert release.wait(10)
        yield from original(*args)
    monkeypatch.setattr(recovered_reuse, 'expected_units', observe)
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


def test_recovery_sql_is_cancelled_before_installing_reuse(tmp_path, monkeypatch):
    from threading import Event
    from app.analytics.errors import ProjectionBuildCancelled
    from app.analytics import conversation_graph_sql
    from app.persistence import sqlite_api
    f, source, stores, pipeline = reopened(tmp_path)
    stop, interrupted = Event(), []
    original = conversation_graph_sql.encoded_graph_records
    def cancel_during_sql(db, *args, **kwargs):
        stop.set()
        try:
            db.execute('WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n '
                       'WHERE i<1000000) SELECT sum(i) FROM n').fetchone()
        except sqlite_api.OperationalError:
            interrupted.append(True)
            raise
        return original(db, *args, **kwargs)
    monkeypatch.setattr(conversation_graph_sql, 'encoded_graph_records', cancel_during_sql)
    try:
        with pytest.raises(ProjectionBuildCancelled):
            pipeline.prepare_questions(ACCOUNT, 1, cancellation_check=stop.is_set)
        assert interrupted == [True]
        assert not stores.projections._graph_segment_proofs
        assert not stores.projections._conversation_enrichment_proofs
        assert not pipeline._account_locks
    finally:
        stores.projections.close_retention_scheduler()
