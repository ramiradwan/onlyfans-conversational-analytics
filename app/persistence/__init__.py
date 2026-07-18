"""Canonical SQLite persistence and backend selection."""

from app.persistence.database import CanonicalSQLite
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
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
