"""Explicitly configured process-local lifecycle for derived analytics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Literal

from app.analytics.errors import (
    ProjectionCoordinatorClosed,
    ProjectionStorageUnavailable,
)
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline, CanonicalReadModelSource
from app.analytics.scheduling import InProcessProjectionScheduler
from app.persistence.projection_activation import ProjectionActivationRepository


AnalyticsBackend = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class AnalyticsRuntime:
    """One process-local analytics pipeline and its owned scheduler."""

    source: CanonicalReadModelSource
    pipeline: AnalyticsPipeline
    scheduler: InProcessProjectionScheduler


@dataclass(frozen=True, slots=True)
class _DefaultRuntimeConfiguration:
    """Dependencies supplied by bootstrap for the default analytics runtime."""

    source: CanonicalReadModelSource
    backend: AnalyticsBackend
    projections_path: str | Path | None
    canonical_path: str | Path | None
    activation: ProjectionActivationRepository | None
    post_commit_rebuild_enabled: bool


_RUNTIMES: dict[int, AnalyticsRuntime] = {}
_RUNTIME_LOCK = RLock()
_DEFAULT_CONFIGURATION: _DefaultRuntimeConfiguration | None = None
_STARTUP_TASK: asyncio.Task[None] | None = None
_STARTUP_TASK_SOURCE_KEY: int | None = None
LOGGER = logging.getLogger(__name__)


def configure_default_analytics_runtime(
    source: CanonicalReadModelSource,
    *,
    backend: AnalyticsBackend,
    projections_path: str | Path | None = None,
    canonical_path: str | Path | None = None,
    activation: ProjectionActivationRepository | None = None,
    post_commit_rebuild_enabled: bool | None = None,
) -> AnalyticsRuntime:
    """Register bootstrap-owned dependencies for the default analytics runtime.

    This module deliberately does not look up transport, persistence settings,
    or activation state.  The application bootstrap supplies those dependencies
    in construction order and this runtime owns only the derived pipeline and
    scheduler built from them.
    """

    if backend not in {"memory", "sqlite"}:
        raise ValueError(f"Unsupported analytics backend {backend!r}")
    configuration = _DefaultRuntimeConfiguration(
        source=source,
        backend=backend,
        projections_path=projections_path,
        canonical_path=canonical_path,
        activation=activation,
        post_commit_rebuild_enabled=(
            backend == "sqlite"
            if post_commit_rebuild_enabled is None
            else post_commit_rebuild_enabled
        ),
    )
    global _DEFAULT_CONFIGURATION, _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    with _RUNTIME_LOCK:
        previous = _DEFAULT_CONFIGURATION
        if previous is not None and previous.source is not source:
            previous_runtime = _RUNTIMES.get(id(previous.source))
            if (
                previous_runtime is not None
                and previous_runtime.source is previous.source
            ):
                previous_runtime.scheduler.abort()
                _RUNTIMES.pop(id(previous.source), None)
            if _STARTUP_TASK_SOURCE_KEY == id(previous.source):
                if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
                    _STARTUP_TASK.cancel()
                _STARTUP_TASK = None
                _STARTUP_TASK_SOURCE_KEY = None
        _DEFAULT_CONFIGURATION = configuration
        existing = _RUNTIMES.get(id(source))
        if (
            existing is not None
            and existing.source is source
            and not existing.scheduler.closed
            and previous == configuration
        ):
            return existing
        if existing is not None and existing.source is source:
            existing.scheduler.abort()
            _RUNTIMES.pop(id(source), None)
            if _STARTUP_TASK_SOURCE_KEY == id(source):
                if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
                    _STARTUP_TASK.cancel()
                _STARTUP_TASK = None
                _STARTUP_TASK_SOURCE_KEY = None
        return _runtime_for_source_locked(source, configuration=configuration)


def analytics_runtime(
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsRuntime:
    """Return the registered default or an explicit-source analytics runtime."""

    with _RUNTIME_LOCK:
        configuration: _DefaultRuntimeConfiguration | None = None
        if source is None:
            configuration = _DEFAULT_CONFIGURATION
            if configuration is None:
                raise RuntimeError(
                    "default analytics runtime has not been configured by bootstrap"
                )
            source = configuration.source
        return _runtime_for_source_locked(source, configuration=configuration)


def _runtime_for_source_locked(
    source: CanonicalReadModelSource,
    *,
    configuration: _DefaultRuntimeConfiguration | None,
) -> AnalyticsRuntime:
    key = id(source)
    existing = _RUNTIMES.get(key)
    if (
        existing is not None
        and existing.source is source
        and not existing.scheduler.closed
    ):
        return existing
    pipeline = _build_pipeline(source, configuration=configuration)
    runtime = AnalyticsRuntime(
        source=source,
        pipeline=pipeline,
        scheduler=InProcessProjectionScheduler(pipeline),
    )
    _RUNTIMES[key] = runtime
    return runtime


def _build_pipeline(
    source: CanonicalReadModelSource,
    *,
    configuration: _DefaultRuntimeConfiguration | None,
) -> AnalyticsPipeline:
    if configuration is None or configuration.backend == "memory":
        return AnalyticsPipeline(source)
    stores = create_analytics_stores(
        "sqlite",
        projections_path=configuration.projections_path,
        canonical_path=configuration.canonical_path,
        activation=configuration.activation,
        canonical_identity_reader=lambda account_id: (
            canonical_identity(source.account_read_model(account_id))
            if source.account_exists(account_id)
            else None
        ),
        lazy=True,
    )
    return AnalyticsPipeline(
        source,
        projections=stores.projections,
        graph=stores.graph,
    )


def analytics_pipeline(
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsPipeline:
    return analytics_runtime(source).pipeline


def projection_scheduler(
    source: CanonicalReadModelSource | None = None,
) -> InProcessProjectionScheduler:
    return analytics_runtime(source).scheduler


async def start_default_analytics_runtime() -> InProcessProjectionScheduler:
    """Start only the configured analytics scheduler and recover projections."""

    scheduler = projection_scheduler()
    await scheduler.start(recover=True)
    return scheduler


def launch_default_analytics_runtime() -> asyncio.Task[None]:
    """Launch derived recovery without coordinating another subsystem lifecycle."""

    global _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    scheduler = projection_scheduler()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        return _STARTUP_TASK

    async def start() -> None:
        try:
            await scheduler.start(recover=True)
        except (ProjectionCoordinatorClosed, ProjectionStorageUnavailable):
            LOGGER.warning(
                "analytics_scheduler_event "
                "reason_code=analytics_projection_start_unavailable "
                "event_type=startup count=1"
            )
        except Exception:
            LOGGER.exception(
                "analytics_scheduler_event "
                "reason_code=analytics_projection_start_failed "
                "event_type=startup count=1"
            )

    _STARTUP_TASK = asyncio.create_task(
        start(), name="analytics-projection-startup"
    )
    _STARTUP_TASK_SOURCE_KEY = id(analytics_runtime().source)
    return _STARTUP_TASK


async def request_projection_rebuild(
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> bool:
    """Request a coalesced derived rebuild after an accepted canonical commit."""

    if source is None:
        with _RUNTIME_LOCK:
            configuration = _DEFAULT_CONFIGURATION
        if configuration is None:
            raise RuntimeError(
                "default analytics runtime has not been configured by bootstrap"
            )
        if not configuration.post_commit_rebuild_enabled:
            return False
    runtime = analytics_runtime(source)
    if runtime.scheduler.closed:
        return False
    account = await runtime.scheduler.canonical_account(creator_account_id)
    await runtime.scheduler.request_recovery(
        creator_account_id, account.view_revision
    )
    return True


async def shutdown_default_analytics_runtime(*, timeout: float = 5.0) -> bool:
    """Close only the default analytics scheduler and await its owned work."""

    with _RUNTIME_LOCK:
        configuration = _DEFAULT_CONFIGURATION
        if configuration is None:
            return True
        source = configuration.source
        key = id(source)
        runtime = _RUNTIMES.get(key)
    if runtime is None or runtime.source is not source:
        return True
    drained = await runtime.scheduler.close(timeout=timeout)
    global _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    startup_task = _STARTUP_TASK
    _STARTUP_TASK = None
    _STARTUP_TASK_SOURCE_KEY = None
    if startup_task is not None and not startup_task.done():
        startup_task.cancel()
    with _RUNTIME_LOCK:
        if _RUNTIMES.get(key) is runtime:
            _RUNTIMES.pop(key, None)
    return drained


def reset_analytics_runtimes() -> None:
    """Clear derived process state while preserving explicit bootstrap wiring."""

    global _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    with _RUNTIME_LOCK:
        for runtime in _RUNTIMES.values():
            runtime.scheduler.abort()
        _RUNTIMES.clear()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        _STARTUP_TASK.cancel()
    _STARTUP_TASK = None
    _STARTUP_TASK_SOURCE_KEY = None
