"""Explicit canonical repository backend selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.repositories import (
    SQLiteAgentConfigRepository,
    SQLiteCommandRepository,
    SQLiteIngestionRepository,
)
from app.services.agent_configuration import (
    AgentConfigRepository,
    InMemoryAgentConfigRepository,
)
from app.services.command_execution import CommandRepository, InMemoryCommandRepository
from app.transport.ingestion import InMemoryIngestionRepository, IngestionRepository


CanonicalBackend = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class CanonicalRepositories:
    ingestion: IngestionRepository
    configuration: AgentConfigRepository
    commands: CommandRepository
    database: CanonicalSQLite | None = None


def create_canonical_repositories(
    backend: CanonicalBackend = "memory",
    *,
    canonical_path: str | Path | None = None,
    migrations_dir: str | Path | None = None,
    busy_timeout_ms: int = 5_000,
) -> CanonicalRepositories:
    """Create all canonical repositories against one explicitly selected backend."""
    if backend == "memory":
        return CanonicalRepositories(
            ingestion=InMemoryIngestionRepository(),
            configuration=InMemoryAgentConfigRepository(),
            commands=InMemoryCommandRepository(),
        )
    if backend != "sqlite":
        raise ValueError(f"Unsupported canonical persistence backend {backend!r}")
    if canonical_path is None:
        raise ValueError("canonical_path is required for the sqlite backend")
    database = CanonicalSQLite(canonical_path, busy_timeout_ms=busy_timeout_ms)
    MigrationRunner(database, migrations_dir=migrations_dir).run()
    return CanonicalRepositories(
        ingestion=SQLiteIngestionRepository(database),
        configuration=SQLiteAgentConfigRepository(database),
        commands=SQLiteCommandRepository(database),
        database=database,
    )
