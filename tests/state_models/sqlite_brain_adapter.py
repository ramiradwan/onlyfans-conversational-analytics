"""File-backed HistoryRepository adapter for restart tests."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.persistence.database import CanonicalSQLite
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from tests.state_models.production_brain_adapter import ProductionBrainAdapter


class SQLiteBrainAdapter(ProductionBrainAdapter):
    """Thin file-backed adapter whose reopen replaces all repository objects."""

    def __init__(
        self,
        canonical_path: str | Path,
        *,
        projection_path: str | Path | None = None,
        connection_id: UUID | None = None,
        fencing_token: str = "fence-tier-b-adapter",
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.canonical_path = Path(canonical_path).expanduser().resolve()
        self.projection_path = (
            None if projection_path is None else Path(projection_path).expanduser().resolve()
        )
        self.busy_timeout_ms = busy_timeout_ms
        self._fixed_connection_id = connection_id
        self._fixed_fencing_token = fencing_token
        self.repositories: CanonicalRepositories
        self.reopen_count = 0
        self._closed = True
        self.reopen()

    def reopen(self) -> None:
        """Close the old object graph and construct a new one over the same file."""
        if not self._closed:
            self.close()
        self.repositories = create_canonical_repositories(
            "sqlite",
            canonical_path=self.canonical_path,
            projection_path=self.projection_path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        super().__init__(
            self.repositories.history,
            self.repositories.database,
            connection_id=self._fixed_connection_id,
            fencing_token=self._fixed_fencing_token,
        )
        self.reopen_count += 1
        self._closed = False

    def close(self) -> None:
        """Assert the production connection model has no retained connection."""
        if self._closed:
            return
        if CanonicalSQLite.open_connection_count(self.canonical_path):
            raise RuntimeError("Tier B reopen requested while a canonical connection is live")
        self._closed = True

    def close_and_reopen(self) -> None:
        self.close()
        self.reopen()


class BrokenReopenAdapter(SQLiteBrainAdapter):
    """Persistent negative control that loses a committed checkpoint on reopen."""

    def close_and_reopen(self) -> None:
        super().close_and_reopen()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM ingest_checkpoints")
