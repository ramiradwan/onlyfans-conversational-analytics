"""Public read models for canonical analytics projections."""

from __future__ import annotations

from typing import Iterator, Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.models.analytics import (
    AccountRef,
    AnalyzerProvenance,
    AnalyticsWindow,
    AvailabilityStatus,
    ConversationMetrics,
    CreatorMetrics,
    GraphProjectionSummary,
    MessageEnrichment,
    MetricProvenance,
    Sha256Digest,
)
from app.models.graph import ExtendedConversationNode


class AnalyticsErrorDetail(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    availability: str | None = None
    retryable: bool | None = None


class AnalyticsErrorResponse(BaseModel):
    detail: AnalyticsErrorDetail


class TopicMetricsResponse(BaseModel):
    """Aggregated message-level topic frequency for the selected range."""

    topic: str
    volume: int = Field(ge=0)
    percentage_of_total: float = Field(ge=0.0, le=100.0)
    trend: float | None = None
    trend_unavailable_reason: str | None = None


class SliceProvenance(BaseModel):
    """Range, coverage, and generation identity for one returned slice."""

    account_ref: AccountRef
    requested_window: AnalyticsWindow
    effective_window: AnalyticsWindow
    sample_count: int = Field(ge=0)
    eligible_sample_count: int = Field(ge=0)
    sample_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    unavailable_reason: str | None = None
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest


class TopicMetricsCollection(BaseModel):
    """Topic metrics plus source, window, and adapter provenance."""

    account_ref: AccountRef
    availability: Literal[AvailabilityStatus.AVAILABLE] = AvailabilityStatus.AVAILABLE
    topics: list[TopicMetricsResponse] = Field(default_factory=list)
    window: AnalyticsWindow
    provenance: AnalyzerProvenance
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    range_provenance: SliceProvenance

    def __iter__(self) -> Iterator[TopicMetricsResponse]:
        return iter(self.topics)

    def __len__(self) -> int:
        return len(self.topics)


class SentimentTrendPoint(BaseModel):
    """Daily mean sentiment with its sample count."""

    date: AwareDatetime
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    message_count: int = Field(default=0, ge=0)


class SentimentTrendResponse(BaseModel):
    account_ref: AccountRef
    availability: Literal[AvailabilityStatus.AVAILABLE] = AvailabilityStatus.AVAILABLE
    trend: list[SentimentTrendPoint] = Field(default_factory=list)
    window: AnalyticsWindow
    provenance: AnalyzerProvenance
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    range_provenance: SliceProvenance


class ResponseTimeMetricsResponse(BaseModel):
    """Uncalibrated response-latency baseline for the selected range."""

    account_ref: AccountRef
    availability: Literal[AvailabilityStatus.AVAILABLE] = AvailabilityStatus.AVAILABLE
    average_handling_time_minutes: float | None = Field(default=None, ge=0.0)
    silence_percentage: float | None = Field(default=None, ge=0.0, le=100.0)
    turns: float | None = Field(default=None, ge=0.0)
    response_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    response_opportunity_count: int = Field(default=0, ge=0)
    responded_count: int = Field(default=0, ge=0)
    window: AnalyticsWindow
    provenance: MetricProvenance
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    range_provenance: SliceProvenance
    unavailable_reasons: dict[str, str] = Field(default_factory=dict)


class AnalyticsUpdate(BaseModel):
    """Account-bound analytics snapshot with explicit slice provenance."""

    availability: Literal[AvailabilityStatus.AVAILABLE] = AvailabilityStatus.AVAILABLE
    topics: list[TopicMetricsResponse] = Field(default_factory=list)
    sentiment_trend: SentimentTrendResponse
    response_time_metrics: ResponseTimeMetricsResponse
    priorityScores: dict[str, float] = Field(default_factory=dict)
    unreadCounts: dict[str, int] = Field(default_factory=dict)
    account_ref: AccountRef
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    requested_window: AnalyticsWindow
    slice_windows: dict[str, AnalyticsWindow]
    slice_provenance: dict[str, SliceProvenance]
    analyzer_provenance: list[AnalyzerProvenance]
    metric_provenance: dict[str, MetricProvenance]
    conversation_metrics: list[ConversationMetrics] = Field(default_factory=list)
    creator_metrics: CreatorMetrics
    message_enrichments: list[MessageEnrichment] = Field(default_factory=list)
    graph: GraphProjectionSummary


class FullSyncResponse(BaseModel):
    """Session-bound bootstrap envelope backed by an active projection."""

    account_ref: AccountRef
    conversations: list[ExtendedConversationNode] = Field(default_factory=list)
    analytics: AnalyticsUpdate
    conversation_window: AnalyticsWindow
    conversation_metric_provenance: MetricProvenance
    conversation_range_provenance: SliceProvenance
