"""Narrow ownership and lifecycle checks for the explicit analytics runtime."""

from __future__ import annotations

import asyncio

import pytest

import app.main as main_module
from app.analytics import runtime as analytics_runtime


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


def test_bootstrap_registers_the_transport_owned_read_source_without_service_discovery() -> None:
    analytics_runtime.reset_analytics_runtimes()
    configured = main_module.configure_analytics_runtime()

    assert configured.source is main_module.transport_manager.ingestion
    assert analytics_runtime.analytics_runtime() is configured
