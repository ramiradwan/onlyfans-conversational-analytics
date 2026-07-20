"""Canonical SQLite persistence and backend selection."""

from typing import Any

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import (
    MigrationChecksumError,
    MigrationError,
    MigrationLockError,
    MigrationRunner,
    SchemaCompatibilityError,
)
from app.persistence.repositories import (
    SQLiteAgentConfigRepository,
    SQLiteCommandRepository,
    SQLiteIngestionRepository,
)

__all__ = [
    "CanonicalRepositories",
    "CanonicalSQLite",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationLockError",
    "MigrationRunner",
    "SQLiteAgentConfigRepository",
    "SQLiteCommandRepository",
    "SQLiteIngestionRepository",
    "SchemaCompatibilityError",
    "create_canonical_repositories",
]

_FACTORY_NAMES = {"CanonicalRepositories", "create_canonical_repositories"}


def __getattr__(name: str) -> Any:
    # app.persistence.factory imports app.analytics.canonical_source, which
    # imports app.persistence.history, which imports this very package.
    # Re-exporting it eagerly here made any module that reaches
    # app.persistence.history before app.analytics.canonical_source has
    # finished loading (for example running `python -m app.analytics.rebuild`
    # directly) fail with a circular ImportError. Deferring the factory
    # import to first attribute access breaks that cycle.
    if name in _FACTORY_NAMES:
        from app.persistence import factory

        return getattr(factory, name)
    raise AttributeError(name)
