"""Process-local lifecycle for derived analytics."""

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
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.analytics.pipeline import AnalyticsPipeline, CanonicalReadModelSource
from app.analytics.scheduling import InProcessProjectionScheduler
from app.analytics.query_runtime import QuestionResources
from app.core.config import settings
from app.persistence.projection_activation import ProjectionActivationRepository
from app.security.analysis_authorization import clear_analysis_policies


AnalyticsBackend = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class AnalyticsRuntime:
    """One process-local analytics pipeline and its owned scheduler."""

    source: CanonicalReadModelSource
    pipeline: AnalyticsPipeline
    scheduler: InProcessProjectionScheduler
    questions: QuestionResources | None = None


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
    """Configure the default runtime from bootstrap-owned dependencies."""

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
                if previous_runtime.questions is not None:
                    previous_runtime.questions.close()
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
            if existing.questions is not None:
                existing.questions.close()
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
    """Return the default runtime or one built from an explicit source."""

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
        questions=QuestionResources(source, pipeline),
    )
    _RUNTIMES[key] = runtime
    return runtime


def _pipeline_type(
    configuration: _DefaultRuntimeConfiguration | None,
):
    if (
        configuration is not None
        and settings.identity_binding_source == "verified_grants"
    ):
        return LicensedAnalyticsPipeline
    return AnalyticsPipeline


def _build_pipeline(
    source: CanonicalReadModelSource,
    *,
    configuration: _DefaultRuntimeConfiguration | None,
) -> AnalyticsPipeline:
    pipeline_type = _pipeline_type(configuration)
    if configuration is None or configuration.backend == "memory":
        return pipeline_type(source)
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
    return pipeline_type(
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
    """Start the configured scheduler and recover projections."""

    runtime = analytics_runtime()
    if runtime.questions is not None:
        runtime.questions.start()
    scheduler = runtime.scheduler
    await scheduler.start(recover=True)
    return scheduler


def launch_default_analytics_runtime() -> asyncio.Task[None]:
    """Launch derived-state recovery."""

    global _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    scheduler = projection_scheduler()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        return _STARTUP_TASK

    async def start() -> None:
        runtime = analytics_runtime()
        if runtime.questions is not None:
            runtime.questions.start()
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
    """Request a coalesced rebuild after a canonical commit."""

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
    if runtime.questions is not None:
        runtime.questions.discard(creator_account_id)
    if runtime.scheduler.closed:
        return False
    account = await runtime.scheduler.canonical_account(creator_account_id)
    await runtime.scheduler.request_recovery(
        creator_account_id, account.view_revision
    )
    return True


async def shutdown_default_analytics_runtime(*, timeout: float = 5.0) -> bool:
    """Close the default scheduler and await its work."""

    with _RUNTIME_LOCK:
        configuration = _DEFAULT_CONFIGURATION
        if configuration is None:
            return True
        source = configuration.source
        key = id(source)
        runtime = _RUNTIMES.get(key)
    if runtime is None or runtime.source is not source:
        return True
    if runtime.questions is not None:
        runtime.questions.close()
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
    """Clear derived process state while retaining bootstrap configuration."""

    global _STARTUP_TASK, _STARTUP_TASK_SOURCE_KEY
    with _RUNTIME_LOCK:
        for runtime in _RUNTIMES.values():
            if runtime.questions is not None:
                runtime.questions.close()
            runtime.scheduler.abort()
        _RUNTIMES.clear()
    clear_analysis_policies()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        _STARTUP_TASK.cancel()
    _STARTUP_TASK = None
    _STARTUP_TASK_SOURCE_KEY = None


def invalidate_question_sources(account_id: str) -> None:
    """Discard derived navigation state without opening an analytics store."""

    with _RUNTIME_LOCK:
        resources = [runtime.questions for runtime in _RUNTIMES.values() if runtime.questions is not None]
    for resource in resources:
        try:
            resource.discard(account_id)
        except Exception:
            LOGGER.warning("analytics_question_event reason_code=evidence_invalidation_failed")
