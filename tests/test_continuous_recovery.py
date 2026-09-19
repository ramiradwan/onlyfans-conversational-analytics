"""Recover canonical changes and expiry without relying on delivery notifications."""

import asyncio
from datetime import timedelta

import pytest

from app.analytics.scheduling import InProcessProjectionScheduler
from app.models.analytics import AvailabilityStatus
from app.persistence.retention import CreatorVaultRetention
from tests.test_continuous_analytics import ready
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cold_equal, insert_message


@pytest.mark.asyncio
async def test_missed_notification_is_recovered_with_normal_admission(ready):
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        await scheduler.start()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        ready.source.loaded.clear()
        with ready.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'new', NOW-timedelta(hours=1), 4)
            advance(db)
        await scheduler.reconcile_once()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        assert ready.source.loaded == ['chat-1']
        assert ready.source.full_reads == 0
        cold_equal(ready, ready.pipeline.projections.get_artifact(ACCOUNT))
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_creator_deletion_without_notification_is_recovered(ready):
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        await scheduler.start()
        await scheduler.wait(ACCOUNT)
        revision = ready.source.account_revision(ACCOUNT)
        CreatorVaultRetention(ready.repositories.database, clock=lambda: NOW).delete_message(ACCOUNT, 'm-1-1')
        assert ready.source.account_revision(ACCOUNT) == revision + 1
        await scheduler.reconcile_once()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        artifact = ready.pipeline.projections.get_artifact(ACCOUNT)
        assert len(artifact.projection.message_enrichments) == 8
        cold_equal(ready, artifact)
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_idle_expiry_rebuilds_surviving_messages(ready):
    with ready.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET sent_at=? WHERE message_id='m-0-0'",
                   ((NOW-timedelta(days=89)).isoformat(),))
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        await scheduler.start()
        await scheduler.wait(ACCOUNT)
        ready.clock.now += timedelta(days=2)
        await scheduler.reconcile_once()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        artifact = ready.pipeline.projections.get_artifact(ACCOUNT)
        assert len(artifact.projection.message_enrichments) == 8
        cold_equal(ready, artifact)
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_periodic_reconciliation_runs_without_queries_and_stops_on_close(ready):
    scheduler = InProcessProjectionScheduler(ready.pipeline, reconciliation_interval=0.05)
    try:
        await scheduler.start()
        await scheduler.wait(ACCOUNT)
        with ready.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'new', NOW-timedelta(hours=1), 4)
            advance(db)
        async with asyncio.timeout(15):
            while True:
                state = scheduler.state(ACCOUNT)
                if state.availability == AvailabilityStatus.AVAILABLE and state.attempted_revision == 2:
                    break
                await asyncio.sleep(0.025)
        assert len(ready.pipeline.projections.get(ACCOUNT).message_enrichments) == 10
    finally:
        assert await scheduler.close(timeout=10)
    assert scheduler._reconciliation_task.done()
    await scheduler.reconcile_once()
    assert scheduler.closed


@pytest.mark.asyncio
async def test_reconciliation_does_not_bypass_analysis_authorization(ready, monkeypatch):
    import app.analytics.licensed_pipeline as licensed
    from app.security.runtime_policy import RuntimeAuthorizationDenied

    def denied(*args):
        raise RuntimeAuthorizationDenied('Analysis is not authorized')
    monkeypatch.setattr(licensed, '_auth_store', lambda path: object())
    monkeypatch.setattr(licensed, 'require_cached_analysis_run', denied)
    pipeline = licensed.LicensedAnalyticsPipeline(ready.source, projections=ready.stores.projections,
        enrichment=ready.pipeline.enrichment, clock=lambda: NOW)
    scheduler = InProcessProjectionScheduler(pipeline)
    try:
        await scheduler.start()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.ERROR
        assert [a.calls for a in ready.analyzers] == [0, 0, 0]
        assert ready.source.loaded == []
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_commit_notification_reads_only_the_account_head(ready):
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        assert await scheduler.canonical_revision(ACCOUNT) == 1
        assert ready.source.loaded == [] and ready.source.full_reads == 0
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_periodic_recovery_survives_an_initial_storage_failure(ready, monkeypatch):
    from app.analytics.errors import ProjectionStorageUnavailable
    scheduler = InProcessProjectionScheduler(ready.pipeline, reconciliation_interval=0.05)
    ensure = scheduler._ensure_projection_storage
    attempts = 0
    async def transient_failure():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProjectionStorageUnavailable()
        await ensure()
    monkeypatch.setattr(scheduler, '_ensure_projection_storage', transient_failure)
    try:
        with pytest.raises(ProjectionStorageUnavailable):
            await scheduler.start()
        async with asyncio.timeout(15):
            while scheduler.state(ACCOUNT).availability != AvailabilityStatus.AVAILABLE:
                await asyncio.sleep(0.025)
        assert attempts >= 2
    finally:
        assert await scheduler.close(timeout=10)
    assert scheduler._reconciliation_task.done()
