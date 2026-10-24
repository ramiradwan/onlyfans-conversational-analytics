"""Public read models for canonical analytics projections."""

from __future__ import annotations

from typing import Annotated, Iterator, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.models.analytics import (
    AccountRef,
    ConversationRef,
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
from app.analytics.query_contracts import Count, Instant, QuestionEvidence, QuestionRecord
from app.nlp.contracts import Identifier
from app.nlp.profiles import PolicyIdentity, TaskName
from app.nlp.navigation import CategoryNavigationDescriptor, navigation_identity


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


class FindingsCategoryFilter(QuestionRecord):
    task: TaskName
    category_id: Identifier


class FindingsRequest(QuestionRecord):
    expected_generation: Annotated[int, Field(strict=True, ge=1)]
    expected_projection_digest: Sha256Digest
    start_date: Instant | None = None
    end_date: Instant | None = None
    direction: Literal['all', 'inbound', 'outbound'] = 'all'
    evidence_offset: Annotated[int, Field(strict=True, ge=0, le=1_000_000)] = 0
    evidence_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    conversation_ref: ConversationRef | None = None
    conversation_offset: Annotated[int, Field(strict=True, ge=0, le=100_000)] = 0
    conversation_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    category_filter: FindingsCategoryFilter | None = None

    @model_validator(mode='after')
    def ordered_window(self):
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError('analytics_findings_request_invalid')
        return self


class FindingsRow(QuestionRecord):
    category_id: Identifier
    display_label: Annotated[str, Field(min_length=1, max_length=256)]
    label_status: Literal['resolved', 'id_only']
    label_identity_sha256: Sha256Digest
    supporting_messages: Count
    supporting_conversations: Count
    occurrences: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count
    share_of_complete_messages: Annotated[float, Field(ge=0, le=1)] | None


class FindingsTask(QuestionRecord):
    task: TaskName
    task_contract: Identifier
    component_id: Identifier
    component_sha256: Sha256Digest
    taxonomy: PolicyIdentity
    availability: Literal['current', 'partial', 'pending']
    eligible_messages: Count
    complete_messages: Count
    partial_messages: Count
    pending_messages: Count
    failed_messages: Count
    unsupported_messages: Count
    uncertain_messages: Count
    scan_complete: bool
    rows: Annotated[tuple[FindingsRow, ...], Field(max_length=64)]


class FindingEvidence(QuestionRecord):
    task: TaskName
    category_id: Identifier
    assessment: Literal['complete', 'partial']
    reference: QuestionEvidence


class FindingsEvidencePage(QuestionRecord):
    offset: Count
    limit: Annotated[int, Field(ge=1, le=100)]
    total: Count
    has_more: bool
    items: Annotated[tuple[FindingEvidence, ...], Field(max_length=100)]


class FindingsComparison(QuestionRecord):
    availability: Literal['available', 'unavailable']
    unavailable_reason: Literal['unbounded_window', 'zero_width_window', 'window_out_of_range'] | None
    boundary_policy: Literal['previous_end_exclusive'] = 'previous_end_exclusive'
    requested_window: AnalyticsWindow | None
    effective_window: AnalyticsWindow | None
    tasks: Annotated[tuple[FindingsTask, ...], Field(max_length=2)]


class FindingsConversation(QuestionRecord):
    conversation_ref: ConversationRef
    eligible_messages: Count


class FindingsConversationPage(QuestionRecord):
    offset: Count
    limit: Annotated[int, Field(ge=1, le=100)]
    total: Count
    has_more: bool
    items: Annotated[tuple[FindingsConversation, ...], Field(max_length=100)]


class FindingsResponse(QuestionRecord):
    schema_version: Literal['analytics-findings.v1'] = 'analytics-findings.v1'
    account_ref: AccountRef
    source_revision: Count
    projection_generation: Annotated[int, Field(ge=1)]
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    snapshot_sha256: Sha256Digest
    profile_sha256: Sha256Digest
    presentation: PolicyIdentity
    requested_window: AnalyticsWindow
    effective_window: AnalyticsWindow
    direction: Literal['all', 'inbound', 'outbound']
    conversation_ref: ConversationRef | None
    conversation_page: FindingsConversationPage
    comparison: FindingsComparison
    processing_mode: Literal['enabled', 'paused']
    tasks: Annotated[tuple[FindingsTask, ...], Field(min_length=2, max_length=2)]
    evidence_page: FindingsEvidencePage


class FindingsTaskAvailability(QuestionRecord):
    task: TaskName
    availability: Literal['declared', 'not_declared']


class SubsetFindingsResponse(FindingsResponse):
    """Only declared tasks carry results; absent capabilities have no outcome."""
    schema_version: Literal['analytics-findings.v2'] = 'analytics-findings.v2'
    task_availability: Annotated[tuple[FindingsTaskAvailability, ...], Field(min_length=2, max_length=2)]
    tasks: Annotated[tuple[FindingsTask, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode='after')
    def declared_tasks(self):
        names = {item.task for item in self.task_availability}
        if names != {'message_topics', 'concept_mentions'}:
            raise ValueError('findings task availability mismatch')
        declared = {item.task for item in self.task_availability if item.availability == 'declared'}
        if {item.task for item in self.tasks} != declared or len(self.tasks) != len(declared):
            raise ValueError('findings declared task mismatch')
        previous = self.comparison.tasks
        if self.comparison.availability == 'available':
            if {item.task for item in previous} != declared or len(previous) != len(declared):
                raise ValueError('findings comparison task mismatch')
        elif previous:
            raise ValueError('unavailable comparison has tasks')
        if any(item.task not in declared for item in self.evidence_page.items):
            raise ValueError('findings evidence task mismatch')
        return self


class FilteredFindingsResponse(SubsetFindingsResponse):
    """Category drilldown keeps the unfiltered task summaries."""
    schema_version: Literal['analytics-findings.v3'] = 'analytics-findings.v3'
    category_filter: FindingsCategoryFilter

    @model_validator(mode='after')
    def selected_category(self):
        selected = self.category_filter
        if not any(task.task == selected.task and
                   any(row.category_id == selected.category_id for row in task.rows)
                   for task in self.tasks):
            raise ValueError('findings category mismatch')
        if any(item.task != selected.task or item.category_id != selected.category_id
               for item in self.evidence_page.items):
            raise ValueError('findings category evidence mismatch')
        return self


class GroupedFindingsRequest(FindingsRequest):
    view: Literal['grouped']
    expected_navigation_sha256: Sha256Digest | None = None

    @model_validator(mode='after')
    def pinned_pages(self):
        if (self.evidence_offset or self.conversation_offset) and self.expected_navigation_sha256 is None:
            raise ValueError('navigation pin required for pagination')
        return self


class NavigationGroupRow(QuestionRecord):
    group_id: Identifier
    task: TaskName
    supporting_messages: Count
    supporting_conversations: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count
    share_of_complete_messages: Annotated[float, Field(ge=0, le=1)] | None

    @model_validator(mode='after')
    def support_partition(self):
        if (self.supporting_messages != self.complete_supporting_messages + self.partial_supporting_messages
                or self.supporting_conversations > self.supporting_messages
                or bool(self.supporting_conversations) != bool(self.supporting_messages)):
            raise ValueError('navigation support counts mismatch')
        return self


class NavigationRead(QuestionRecord):
    availability: Literal['available', 'not_declared']
    policy: PolicyIdentity | None
    declaration: CategoryNavigationDescriptor | None
    current: Annotated[tuple[NavigationGroupRow, ...], Field(max_length=16)]
    previous: Annotated[tuple[NavigationGroupRow, ...], Field(max_length=16)] | None

    @model_validator(mode='after')
    def exact_declaration(self):
        if self.availability == 'not_declared':
            if self.policy is not None or self.declaration is not None or self.current or self.previous is not None:
                raise ValueError('undeclared navigation has results')
            return self
        if self.declaration is None or self.policy != navigation_identity(self.declaration):
            raise ValueError('navigation identity mismatch')
        expected = [(group.group_id, group.task) for group in self.declaration.groups]
        for rows in (self.current, self.previous):
            if rows is not None and [(row.group_id, row.task) for row in rows] != expected:
                raise ValueError('navigation row order mismatch')
        return self


class GroupedFindingsResponse(SubsetFindingsResponse):
    schema_version: Literal['analytics-findings.v4'] = 'analytics-findings.v4'
    view: Literal['grouped'] = 'grouped'
    category_filter: FindingsCategoryFilter | None
    navigation: NavigationRead

    @model_validator(mode='after')
    def grouped_population(self):
        selected = self.category_filter
        if selected is not None:
            if not any(task.task == selected.task and any(row.category_id == selected.category_id for row in task.rows)
                       for task in self.tasks):
                raise ValueError('findings category mismatch')
            if any(item.task != selected.task or item.category_id != selected.category_id
                   for item in self.evidence_page.items):
                raise ValueError('findings category evidence mismatch')
        navigation = self.navigation
        if navigation.availability == 'not_declared':
            return self
        declaration = navigation.declaration
        if declaration.source_presentation != self.presentation:
            raise ValueError('navigation presentation mismatch')
        bindings = {binding.task: binding for binding in declaration.bindings}
        if set(bindings) != {task.task for task in self.tasks}:
            raise ValueError('navigation declared task mismatch')
        for task in self.tasks:
            binding = bindings[task.task]
            if (binding.task_contract != task.task_contract or binding.taxonomy != task.taxonomy
                    or binding.enabled_ids != tuple(sorted(row.category_id for row in task.rows))):
                raise ValueError('navigation category binding mismatch')
        if (navigation.previous is not None) != (self.comparison.availability == 'available'):
            raise ValueError('navigation comparison mismatch')
        for rows, tasks in ((navigation.current, self.tasks), (navigation.previous, self.comparison.tasks)):
            if rows is None:
                continue
            by_task = {task.task: task for task in tasks}
            for row in rows:
                task = by_task[row.task]
                expected = row.complete_supporting_messages / task.complete_messages if task.complete_messages else None
                if (row.complete_supporting_messages > task.complete_messages
                        or row.partial_supporting_messages > task.partial_messages
                        or row.share_of_complete_messages != expected):
                    raise ValueError('navigation denominator mismatch')
        return self


FindingsReadResponse = Annotated[FindingsResponse | SubsetFindingsResponse | FilteredFindingsResponse | GroupedFindingsResponse, Field(discriminator='schema_version')]
