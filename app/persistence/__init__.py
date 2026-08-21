"""Canonical SQLite persistence and backend selection."""

from typing import Any

from app.persistence.database import AuthSQLite, CanonicalSQLite
from app.persistence.migrations import (
    MigrationChecksumError,
    MigrationError,
    MigrationLockError,
    MigrationRunner,
    SchemaCompatibilityError,
)

__all__ = [
    "CanonicalRepositories",
    "AuthSQLite",
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
_REPOSITORY_NAMES = {
    "SQLiteAgentConfigRepository",
    "SQLiteCommandRepository",
    "SQLiteIngestionRepository",
}


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
    # Repository re-exports resolve on first attribute access, so importing
    # this package does not import runtime configuration.
    if name in _REPOSITORY_NAMES:
        from app.persistence import repositories

        return getattr(repositories, name)
    raise AttributeError(name)
