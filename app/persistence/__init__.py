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
    # Factory exports remain lazy so importing persistence history does not
    # construct repositories or load runtime configuration as a side effect.
    if name in _FACTORY_NAMES:
        from app.persistence import factory

        return getattr(factory, name)
    # Repository re-exports resolve on first attribute access, so importing
    # this package does not import runtime configuration.
    if name in _REPOSITORY_NAMES:
        from app.persistence import repositories

        return getattr(repositories, name)
    raise AttributeError(name)
