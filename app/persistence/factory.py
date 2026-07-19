"""Explicit canonical repository backend selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from app.persistence.database import CanonicalSQLite
from app.persistence.history import HistoryRepository, ProjectionRepository
from app.persistence.migrations import MigrationRunner
from app.persistence.repositories import (
    SQLiteAgentConfigRepository,
    SQLiteCommandRepository,
)
from app.services.agent_configuration import (
    AgentConfigRepository,
    InMemoryAgentConfigRepository,
)
from app.services.command_execution import CommandRepository, InMemoryCommandRepository


CanonicalBackend = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class CanonicalRepositories:
    history: HistoryRepository
    projection: ProjectionRepository
    configuration: AgentConfigRepository
    commands: CommandRepository
    database: CanonicalSQLite
    projection_database: CanonicalSQLite
    temporary_directory: TemporaryDirectory[str] | None = None


def create_canonical_repositories(
    backend: CanonicalBackend = "memory",
    *,
    canonical_path: str | Path | None = None,
    projection_path: str | Path | None = None,
    migrations_dir: str | Path | None = None,
    busy_timeout_ms: int = 5_000,
) -> CanonicalRepositories:
    """Create all canonical repositories against one explicitly selected backend."""
    if backend not in {"memory", "sqlite"}:
        raise ValueError(f"Unsupported canonical persistence backend {backend!r}")
    temporary_directory: TemporaryDirectory[str] | None = None
    if backend == "memory":
        temporary_directory = TemporaryDirectory(prefix="onlyfans-brain-")
        root = Path(temporary_directory.name)
        canonical_path = root / "canonical.sqlite3"
        projection_path = root / "projections.sqlite3"
    elif canonical_path is None:
        raise ValueError("canonical_path is required for the sqlite backend")
    if projection_path is None:
        canonical_file = Path(canonical_path).expanduser().resolve()
        projection_path = canonical_file.with_name("projections.sqlite3")
    database = CanonicalSQLite(canonical_path, busy_timeout_ms=busy_timeout_ms)
    MigrationRunner(database, migrations_dir=migrations_dir).run()
    history = HistoryRepository(database)
    projection = ProjectionRepository.create(projection_path, history)
    return CanonicalRepositories(
        history=history,
        projection=projection,
        configuration=(
            InMemoryAgentConfigRepository()
            if backend == "memory"
            else SQLiteAgentConfigRepository(database)
        ),
        commands=(
            InMemoryCommandRepository()
            if backend == "memory"
            else SQLiteCommandRepository(database)
        ),
        database=database,
        projection_database=projection.database,
        temporary_directory=temporary_directory,
    )
