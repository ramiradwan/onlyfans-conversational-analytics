"""Narrow ownership and lifecycle checks for the explicit analytics runtime."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.main as main_module
from app.analytics import runtime as analytics_runtime

pytestmark = [pytest.mark.ci_tier('fast')]


class _EmptyCanonicalSource:
    """Sufficient explicit source for registry-only lifecycle coverage."""

    def account_exists(self, creator_account_id: str) -> bool:
        return False

    def account_revisions(self) -> list[tuple[str, int]]:
        return []

    def account_read_model(self, creator_account_id: str):
        raise AssertionError("registry setup must not read canonical state")


def test_reconfiguring_bootstrap_source_closes_and_unregisters_previous_runtime() -> None:
    first_source = _EmptyCanonicalSource()
    second_source = _EmptyCanonicalSource()
    try:
        first = analytics_runtime.configure_default_analytics_runtime(
            first_source,
            backend="memory",
        )
        second = analytics_runtime.configure_default_analytics_runtime(
            second_source,
            backend="memory",
        )

        assert first.scheduler.closed
        assert second.source is second_source
        assert analytics_runtime.analytics_runtime() is second
    finally:
        analytics_runtime.reset_analytics_runtimes()
        main_module.configure_analytics_runtime()


@pytest.mark.asyncio
async def test_reconfiguring_bootstrap_source_cancels_prior_startup_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = _EmptyCanonicalSource()
    second_source = _EmptyCanonicalSource()
    started = asyncio.Event()
    startup_task: asyncio.Task[None] | None = None

    async def block_start(*, recover: bool) -> None:
        assert recover is True
        started.set()
        await asyncio.Future()

    try:
        first = analytics_runtime.configure_default_analytics_runtime(
            first_source,
            backend="memory",
        )
        monkeypatch.setattr(first.scheduler, "start", block_start)
        startup_task = analytics_runtime.launch_default_analytics_runtime()
        await started.wait()

        analytics_runtime.configure_default_analytics_runtime(
            second_source,
            backend="memory",
        )

        with pytest.raises(asyncio.CancelledError):
            await startup_task
        assert first.scheduler.closed
    finally:
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
        analytics_runtime.reset_analytics_runtimes()
        main_module.configure_analytics_runtime()


def test_bootstrap_registers_the_explicit_read_source_without_service_discovery() -> None:
    analytics_runtime.reset_analytics_runtimes()
    configured = main_module.configure_analytics_runtime()

    assert configured.source is main_module.history_source
    assert not hasattr(main_module.transport_manager, "ingestion")
    assert analytics_runtime.analytics_runtime() is configured


@pytest.mark.asyncio
async def test_native_close_failure_still_drains_scheduler_and_retains_runtime(monkeypatch):
    source = _EmptyCanonicalSource()
    events = []
    failing = [True]
    class Questions:
        def close(self):
            events.append('questions-close')
            if failing[0]: raise RuntimeError('native close unavailable')
        def wait_closed(self, timeout):
            events.append('questions-wait')
            return not failing[0]
    class Scheduler:
        async def close(self, *, timeout):
            events.append('scheduler-close')
            return True
    owned = analytics_runtime.AnalyticsRuntime(source, None, Scheduler(), Questions())
    registry = {id(source): owned}
    startup = asyncio.create_task(asyncio.sleep(60))
    monkeypatch.setattr(analytics_runtime, '_RUNTIMES', registry)
    monkeypatch.setattr(analytics_runtime, '_DEFAULT_CONFIGURATION', SimpleNamespace(source=source))
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK', startup)
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK_SOURCE_KEY', id(source))
    assert not await analytics_runtime.shutdown_default_analytics_runtime(timeout=0.1)
    assert events == ['questions-close', 'scheduler-close', 'questions-wait']
    assert registry[id(source)] is owned
    with pytest.raises(asyncio.CancelledError): await startup
    failing[0] = False
    assert await analytics_runtime.shutdown_default_analytics_runtime(timeout=0.1)
    assert not registry


def test_sync_reset_attempts_all_owners_and_retains_native_close_failure(monkeypatch):
    events = []
    failing = [True]
    class Questions:
        def __init__(self, name): self.name = name
        def close(self):
            events.append((self.name, 'questions-close'))
            if self.name == 'first' and failing[0]: raise RuntimeError('native close unavailable')
        def wait_closed(self, timeout): return self.name != 'first' or not failing[0]
    class Scheduler:
        def __init__(self, name): self.name = name
        def abort(self): events.append((self.name, 'scheduler-abort'))
    first_source, second_source = _EmptyCanonicalSource(), _EmptyCanonicalSource()
    first = analytics_runtime.AnalyticsRuntime(first_source, None, Scheduler('first'), Questions('first'))
    second = analytics_runtime.AnalyticsRuntime(second_source, None, Scheduler('second'), Questions('second'))
    registry = {id(first_source): first, id(second_source): second}
    monkeypatch.setattr(analytics_runtime, '_RUNTIMES', registry)
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK', None)
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK_SOURCE_KEY', None)
    analytics_runtime.reset_analytics_runtimes()
    assert events == [('first', 'questions-close'), ('first', 'scheduler-abort'),
                      ('second', 'questions-close'), ('second', 'scheduler-abort')]
    assert registry == {id(first_source): first}
    failing[0] = False
    analytics_runtime.reset_analytics_runtimes()
    assert not registry


def test_configuration_cannot_replace_same_source_with_live_question_handles(monkeypatch):
    from app.analytics.errors import ProjectionCoordinatorClosed
    source = _EmptyCanonicalSource()
    calls = []
    class Questions:
        def close(self): calls.append('questions-close')
        def wait_closed(self, timeout): return False
    class Scheduler:
        closed = True
        def abort(self): calls.append('scheduler-abort')
    owned = analytics_runtime.AnalyticsRuntime(source, None, Scheduler(), Questions())
    registry = {id(source): owned}
    monkeypatch.setattr(analytics_runtime, '_RUNTIMES', registry)
    monkeypatch.setattr(analytics_runtime, '_DEFAULT_CONFIGURATION', SimpleNamespace(source=source))
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK', None)
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK_SOURCE_KEY', None)
    with pytest.raises(ProjectionCoordinatorClosed):
        analytics_runtime.configure_default_analytics_runtime(source, backend='memory')
    assert calls == ['questions-close', 'scheduler-abort']
    assert registry[id(source)] is owned


@pytest.mark.asyncio
async def test_shutdown_reports_false_when_native_close_crosses_original_deadline(monkeypatch):
    source = _EmptyCanonicalSource()
    now = [0.0]
    class Questions:
        def close(self): now[0] = 1.0
        def wait_closed(self, timeout): return True
    class Scheduler:
        async def close(self, *, timeout): return True
    owned = analytics_runtime.AnalyticsRuntime(source, None, Scheduler(), Questions())
    registry = {id(source): owned}
    monkeypatch.setattr(analytics_runtime, '_RUNTIMES', registry)
    monkeypatch.setattr(analytics_runtime, '_DEFAULT_CONFIGURATION', SimpleNamespace(source=source))
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK', None)
    monkeypatch.setattr(analytics_runtime, '_STARTUP_TASK_SOURCE_KEY', None)
    monkeypatch.setattr(analytics_runtime, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    assert not await analytics_runtime.shutdown_default_analytics_runtime(timeout=0.1)
    assert registry[id(source)] is owned
    assert await analytics_runtime.shutdown_default_analytics_runtime(timeout=0.1)
    assert not registry
