"""Subprocess helper that terminates at a durable projection activation boundary."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence.factory import create_canonical_repositories


def main() -> None:
    stage, canonical_value, projections_value = sys.argv[1:4]
    canonical_path = Path(canonical_value)
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=canonical_path.with_name("history-projections.sqlite3"),
    )

    def identity_reader(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    def terminate(observed: str, generation_id: str) -> None:
        del generation_id
        if observed == stage:
            os._exit(91)

    store = SQLiteAnalyticsProjectionStore(
        Path(projections_value),
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader,
        crash_hook=terminate,
        # The worker can be CPU-starved while the parent runs the other
        # subprocess crash cases.  Keep the lease comfortably longer than a
        # single validation pass; the dead-owner probe still verifies eager
        # reclaim independently of lease expiry.
        lease_seconds=5.0,
    )
    AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
    ).project_account("account-a")
    raise RuntimeError(f"crash stage was not reached: {stage}")


if __name__ == "__main__":
    main()
