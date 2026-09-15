"""Explicit production composition for persistence, transport, and analytics."""

from __future__ import annotations

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.core.config import settings
from app.persistence.factory import create_canonical_repositories
from app.provisioning.progress_reporting import configured_runtime_onboarding_progress
from app.transport.account_bound_manager import AccountBoundTransportManager


# Compose persistence, transport, and analytics at the application boundary.
repositories = create_canonical_repositories(
    settings.canonical_persistence_backend,
    canonical_path=(
        settings.canonical_database_path
        if settings.canonical_persistence_backend == "sqlite"
        else None
    ),
    projection_path=(
        settings.projection_database_path
        if settings.canonical_persistence_backend == "sqlite"
        else None
    ),
)
transport_manager = AccountBoundTransportManager(
    repositories,
    onboarding_progress=configured_runtime_onboarding_progress(),
)
history_source = HistoryAnalyticsSource(repositories.history)
