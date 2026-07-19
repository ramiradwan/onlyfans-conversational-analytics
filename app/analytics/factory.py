"""Explicit backend selection for rebuildable analytics projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from app.analytics.database import ProjectionsDatabase
from app.analytics.identity import CanonicalIdentity
from app.analytics.graph_store import GraphReader, InMemoryGraphRepository
from app.analytics.resilient_projection_store import (
    LazySQLiteAnalyticsProjectionStore,
)
from app.analytics.projection_store import (
    AnalyticsProjectionStore,
    InMemoryAnalyticsProjectionStore,
)
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence.projection_activation import ProjectionActivationRepository


ProjectionBackend = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class AnalyticsStores:
    projections: AnalyticsProjectionStore
    graph: GraphReader
    database: ProjectionsDatabase | None = None


def create_analytics_stores(
    backend: ProjectionBackend = "memory",
    *,
    projections_path: str | Path | None = None,
    canonical_path: str | Path | None = None,
    activation: ProjectionActivationRepository | None = None,
    canonical_identity_reader: Callable[[str], CanonicalIdentity | None] | None = None,
    busy_timeout_ms: int = 5_000,
    lease_seconds: float = 120.0,
    rollback_retention: int = 1,
    gc_batch_size: int = 8,
    lazy: bool = False,
) -> AnalyticsStores:
    if backend == "memory":
        repository = InMemoryGraphRepository(
            rollback_retention=rollback_retention,
            gc_batch_size=gc_batch_size,
        )
        projections = InMemoryAnalyticsProjectionStore(
            graph_repository=repository,
            activation=activation,
            canonical_identity_reader=canonical_identity_reader,
            rollback_retention=rollback_retention,
            gc_batch_size=gc_batch_size,
        )
        return AnalyticsStores(
            projections=projections,
            graph=projections.graph,
        )
    if backend != "sqlite":
        raise ValueError(f"Unsupported analytics persistence backend {backend!r}")
    if projections_path is None:
        raise ValueError("projections_path is required for the sqlite backend")
    if canonical_path is not None and (
        Path(projections_path).expanduser().resolve()
        == Path(canonical_path).expanduser().resolve()
    ):
        raise ValueError("canonical and projections databases must use separate files")
    if activation is None or canonical_identity_reader is None:
        raise ValueError(
            "activation and canonical_identity_reader are required for sqlite"
        )
    if lazy:
        projections = LazySQLiteAnalyticsProjectionStore(
            projections_path,
            activation=activation,
            canonical_identity_reader=canonical_identity_reader,
            busy_timeout_ms=busy_timeout_ms,
            lease_seconds=lease_seconds,
            rollback_retention=rollback_retention,
            gc_batch_size=gc_batch_size,
        )
        return AnalyticsStores(
            projections=projections,
            graph=projections.graph,
        )
    database = ProjectionsDatabase(
        projections_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    projections = SQLiteAnalyticsProjectionStore(
        database,
        activation=activation,
        canonical_identity_reader=canonical_identity_reader,
        lease_seconds=lease_seconds,
        rollback_retention=rollback_retention,
        gc_batch_size=gc_batch_size,
    )
    return AnalyticsStores(
        projections=projections,
        graph=projections.graph,
        database=database,
    )
