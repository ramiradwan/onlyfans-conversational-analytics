"""Hold a durable generation boundary while another process publishes."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence.factory import create_canonical_repositories
from app.persistence.projection_activation import ProjectionActivationConflict


def main() -> int:
    canonical_value, projections_value, ready_value, release_value = sys.argv[1:5]
    ready = Path(ready_value)
    release = Path(release_value)
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

    def hold(stage: str, generation_id: str) -> None:
        del generation_id
        if stage != "built":
            return
        ready.write_text("ready\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("concurrent writer release was not observed")
            time.sleep(0.01)

    store = SQLiteAnalyticsProjectionStore(
        Path(projections_value),
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader,
        crash_hook=hold,
        lease_seconds=10,
    )
    try:
        AnalyticsPipeline(
            repositories.ingestion,
            projections=store,
            graph=store.graph,
        ).project_account("account-a")
    except ProjectionActivationConflict:
        return 92
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
