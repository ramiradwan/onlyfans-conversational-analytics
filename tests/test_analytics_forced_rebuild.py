"""A creator-requested rebuild reruns analysis even at an unchanged revision."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.analytics.scheduling import InProcessProjectionScheduler
from app.models.analytics import AvailabilityStatus
from tests.test_continuous_analytics import ready, ACCOUNT
from tests.test_analytics_lifecycle_receipts import sink, read_all

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.mark.asyncio
async def test_forced_rebuild_ignores_current_projection_and_coalesces(ready):
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        await scheduler.start()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        calls = [analyzer.calls for analyzer in ready.analyzers]
        revision = await scheduler.canonical_revision(ACCOUNT)
        await scheduler.schedule(ACCOUNT, revision)
        assert [analyzer.calls for analyzer in ready.analyzers] == calls
        await scheduler.schedule(ACCOUNT, revision, force=True)
        await scheduler.schedule(ACCOUNT, revision, force=True)
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        assert [analyzer.calls for analyzer in ready.analyzers] == [value + 9 for value in calls]
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_force_during_an_unchanged_build_is_not_lost(ready, monkeypatch):
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    started = asyncio.Event()
    proceed = asyncio.Event()
    original = scheduler._run_owned
    blocked = False

    async def hold(operation):
        nonlocal blocked
        if getattr(operation, "func", None) == scheduler._build_observed and not blocked:
            blocked = True
            started.set()
            await proceed.wait()
        return await original(operation)

    monkeypatch.setattr(scheduler, "_run_owned", hold)
    try:
        await scheduler.start(recover=False)
        revision = await scheduler.canonical_revision(ACCOUNT)
        await scheduler.schedule(ACCOUNT, revision)
        await asyncio.wait_for(started.wait(), 10)
        ownership = (scheduler._scheduler_owner_id, scheduler._publication_epoch, revision)
        assert scheduler._pending_question_state(ACCOUNT) == ownership
        await scheduler.schedule(ACCOUNT, revision, force=True)
        assert scheduler._pending_question_state(ACCOUNT) == ownership
        proceed.set()
        assert (await scheduler.wait(ACCOUNT)).availability == AvailabilityStatus.AVAILABLE
        assert scheduler._pending_question_state(ACCOUNT) is None
        assert [analyzer.calls for analyzer in ready.analyzers] == [18, 18, 18]
    finally:
        proceed.set()
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_shutdown_receipt_requires_actual_build_cancellation(ready, monkeypatch, sink):
    import threading
    import time
    from app.analytics.errors import ProjectionBuildCancelled

    started = threading.Event()
    def cancellable(account, *, cancellation_check, **kwargs):
        started.set()
        deadline = time.monotonic() + 3
        while not cancellation_check():
            if time.monotonic() > deadline:
                raise AssertionError("worker cancellation missing")
            time.sleep(0.001)
        raise ProjectionBuildCancelled()
    monkeypatch.setattr(ready.pipeline, "build_candidate", cancellable)
    scheduler = InProcessProjectionScheduler(ready.pipeline)
    try:
        await scheduler.start(recover=False)
        revision = await scheduler.canonical_revision(ACCOUNT)
        await scheduler.schedule(ACCOUNT, revision, force=True)
        assert await asyncio.to_thread(started.wait, 3)
    finally:
        joined = await scheduler.close(timeout=10)
    assert joined
    observer, reader = sink
    observer.finish(joined)
    records = read_all(reader)
    events = [record for record in records if record["event_type"].startswith("build_")]
    assert [record["event_type"] for record in events] == ["build_started", "build_cancelled"]
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert all(record["full_rebuild"] for record in events)
    assert records[-1]["complete"] is True


def test_rebuild_route_rejects_account_override_and_requires_admission(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.activation import require_activated_runtime
    from app.api.dependencies import get_authenticated_account_session
    from app.api.endpoints import insights
    from app.api.security import csrf_token
    from app.security.runtime_policy import RuntimeAuthorizationDenied
    from tests.test_analytics_evidence import policy

    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[require_activated_runtime] = lambda: None
    app.dependency_overrides[get_authenticated_account_session] = lambda: policy()
    rebuild = AsyncMock()
    monkeypatch.setattr(insights.insights_service, "rebuild_projection", rebuild)
    with TestClient(app) as client:
        client.headers["x-csrf-token"] = csrf_token(policy())
        for body in ({"creator_account_id": "another"}, [], None):
            assert client.post("/api/v1/insights/rebuild", json=body).status_code in (415, 422)
        assert client.post("/api/v1/insights/rebuild?account=another", json={}).status_code == 422
        rebuild.assert_not_called()
        rebuild.side_effect = RuntimeAuthorizationDenied("denied")
        assert client.post("/api/v1/insights/rebuild", json={}).status_code == 403
        rebuild.side_effect = None
        response = client.post("/api/v1/insights/rebuild", json={})
        assert response.status_code == 202
        assert response.json() == {"availability": "building"}
        assert response.headers["cache-control"] == "no-store"
        del client.headers["x-csrf-token"]
        assert client.post("/api/v1/insights/rebuild", json={}).status_code == 403
