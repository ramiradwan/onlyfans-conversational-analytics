"""Canonical analytics consumer retained under the former service module name.

Raw snapshot/delta sequencing is authoritative in
``app.persistence.history.HistoryRepository`` (the signer-v2 chunked
begin/chunk/commit canonical write path). This module deliberately owns no
queue, cache, or alternate ingestion state; it only replays committed
canonical account state into analytics projections.
"""

from __future__ import annotations

from app.analytics.pipeline import (
    AnalyticsPipeline,
    CanonicalReadModelSource,
    PipelineRun,
)


class CanonicalAnalyticsConsumer:
    """Refresh or rebuild analytics from committed canonical read-model state."""

    def __init__(
        self,
        source: CanonicalReadModelSource,
        *,
        pipeline: AnalyticsPipeline | None = None,
    ) -> None:
        self.pipeline = pipeline or AnalyticsPipeline(source)

    def refresh(self, creator_account_id: str) -> PipelineRun:
        return self.pipeline.project_account(creator_account_id)

    def rebuild(self, creator_account_id: str) -> PipelineRun:
        return self.pipeline.rebuild_account(creator_account_id)


DataIngestService = CanonicalAnalyticsConsumer

__all__ = ["CanonicalAnalyticsConsumer", "DataIngestService"]
