"""Typed domain models for deterministic conversational analytics projections."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.analytics.graph_identity import require_graph_id
from app.analytics.opaque_refs import require_opaque_ref, validated_account_ref
from app.analytics.graph_schema import (
    validate_edge_properties,
    validate_node_properties,
)


Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
AccountRef = Annotated[str, Field(pattern=r"^a1:[0-9a-f]{64}$")]
ConversationRef = Annotated[str, Field(pattern=r"^c1:[0-9a-f]{64}$")]
ParticipantRef = Annotated[str, Field(pattern=r"^p1:[0-9a-f]{64}$")]
MessageRef = Annotated[str, Field(pattern=r"^m1:[0-9a-f]{64}$")]
TopicRef = Annotated[str, Field(pattern=r"^t1:[0-9a-f]{64}$")]
EntityRef = Annotated[str, Field(pattern=r"^x1:[0-9a-f]{64}$")]


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("graph_time_timezone_required")
    return value.astimezone(timezone.utc)


class AnalyticsModel(BaseModel):
    """Closed base model used by the derived analytics plane."""

    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SentimentLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class EngagementState(str, Enum):
    ACKNOWLEDGEMENT = "acknowledgement"
    COMMITMENT = "commitment"
    CONSTRAINT = "constraint"
    COORDINATION = "coordination"
    INFORMATION = "information"
    INQUIRY = "inquiry"
    MINIMAL = "minimal"
    TRANSACTIONAL = "transactional"


class EntityType(str, Enum):
    AMOUNT = "amount"
    HASHTAG = "hashtag"
    MENTION = "mention"
    URL = "url"


class AnalysisMode(str, Enum):
    """Truthful capability class for an analyzer or deterministic formula."""

    BASELINE = "baseline"
    MODEL = "model"


class CalibrationStatus(str, Enum):
    NOT_CALIBRATED = "not_calibrated"
    CALIBRATED = "calibrated"
    UNAVAILABLE = "unavailable"


class AvailabilityStatus(str, Enum):
    UNAVAILABLE = "unavailable"
    BUILDING = "building"
    ERROR = "error"
    AVAILABLE = "available"


class WindowScope(str, Enum):
    ALL_TIME = "all_time"
    REQUESTED = "requested"
    EFFECTIVE = "effective"


class AnalyticsWindow(AnalyticsModel):
    """Explicit time basis for a returned analytics slice."""

    scope: WindowScope
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None


class AnalyzerProvenance(AnalyticsModel):
    """Stable adapter identity plus truthful calibration and sample coverage."""

    analyzer_name: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    config_digest: Sha256Digest
    mode: AnalysisMode
    calibration_status: CalibrationStatus
    analyzed_sample_count: int = Field(default=0, ge=0)
    eligible_sample_count: int = Field(default=0, ge=0)
    sample_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    mean_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    unavailable_reason: str | None = None


class MetricProvenance(AnalyticsModel):
    """Stable identity for a deterministic aggregate or ranking formula."""

    metric_name: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    config_digest: Sha256Digest
    mode: AnalysisMode = AnalysisMode.BASELINE
    calibration_status: CalibrationStatus = CalibrationStatus.NOT_CALIBRATED
    sample_count: int = Field(default=0, ge=0)
    sample_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    unavailable_reason: str | None = None


class GraphNodeKind(str, Enum):
    PARTICIPANT = "participant"
    CONVERSATION = "conversation"
    MESSAGE = "message"
    TOPIC = "topic"
    ENTITY = "entity"
    AFFECT_STATE = "affect_state"
    ENGAGEMENT_STATE = "engagement_state"


class GraphRelation(str, Enum):
    PARTICIPATES_IN = "participates_in"
    CONTAINS = "contains"
    SENT = "sent"
    RECEIVED_BY = "received_by"
    EXPRESSES_AFFECT = "expresses_affect"
    HAS_ENGAGEMENT_STATE = "has_engagement_state"
    MENTIONS_TOPIC = "mentions_topic"
    MENTIONS_ENTITY = "mentions_entity"
    PRECEDES = "precedes"


class MessageAnalysisInput(AnalyticsModel):
    """Minimal canonical message view accepted by every analyzer seam."""

    creator_account_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    participant_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    text: str
    sent_at: AwareDatetime
    direction: MessageDirection


class SentimentResult(AnalyticsModel):
    label: SentimentLabel
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(default=0, ge=0)
    analyzer_name: str = Field(min_length=1)
    analyzer_revision: str = Field(min_length=1)
    analyzer_config_digest: Sha256Digest
    analysis_mode: AnalysisMode
    calibration_status: CalibrationStatus


class TopicMention(AnalyticsModel):
    topic_ref: TopicRef
    taxonomy_id: Literal[
        "feedback", "greeting", "media", "pricing", "scheduling", "support"
    ]
    label: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(default=0, ge=0)


class EntityMention(AnalyticsModel):
    entity_ref: EntityRef
    entity_type: EntityType
    confidence: float = Field(ge=0.0, le=1.0)


class TopicEntityResult(AnalyticsModel):
    topics: list[TopicMention] = Field(default_factory=list)
    entities: list[EntityMention] = Field(default_factory=list)
    analyzer_name: str = Field(min_length=1)
    analyzer_revision: str = Field(min_length=1)
    analyzer_config_digest: Sha256Digest
    analysis_mode: AnalysisMode
    calibration_status: CalibrationStatus


class EngagementResult(AnalyticsModel):
    state: EngagementState
    confidence: float = Field(ge=0.0, le=1.0)
    signal_count: int = Field(default=0, ge=0)
    analyzer_name: str = Field(min_length=1)
    analyzer_revision: str = Field(min_length=1)
    analyzer_config_digest: Sha256Digest
    analysis_mode: AnalysisMode
    calibration_status: CalibrationStatus


class MessageEnrichment(AnalyticsModel):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    participant_ref: ParticipantRef
    message_ref: MessageRef
    source_ordinal: int = Field(ge=0)
    sent_at: AwareDatetime
    direction: MessageDirection
    sentiment: SentimentResult
    topic_entities: TopicEntityResult
    engagement: EngagementResult


class ConversationMetrics(AnalyticsModel):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    participant_ref: ParticipantRef
    unread_count: int = Field(ge=0)
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    duration_seconds: float = Field(ge=0.0)
    message_count: int = Field(ge=0)
    inbound_message_count: int = Field(ge=0)
    outbound_message_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    response_opportunity_count: int = Field(ge=0)
    responded_count: int = Field(ge=0)
    response_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    average_response_seconds: float | None = Field(default=None, ge=0.0)
    median_response_seconds: float | None = Field(default=None, ge=0.0)
    maximum_silence_seconds: float | None = Field(default=None, ge=0.0)
    average_sentiment_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    sentiment_counts: dict[str, int] = Field(default_factory=dict)
    topic_counts: dict[str, int] = Field(default_factory=dict)
    entity_counts: dict[str, int] = Field(default_factory=dict)
    engagement_counts: dict[str, int] = Field(default_factory=dict)
    provenance: MetricProvenance
    window: AnalyticsWindow
    unavailable_reasons: dict[str, str] = Field(default_factory=dict)


class CreatorMetrics(AnalyticsModel):
    account_ref: AccountRef
    conversation_count: int = Field(ge=0)
    participant_count: int = Field(ge=0)
    message_count: int = Field(ge=0)
    inbound_message_count: int = Field(ge=0)
    outbound_message_count: int = Field(ge=0)
    active_from: AwareDatetime | None = None
    active_until: AwareDatetime | None = None
    average_messages_per_conversation: float | None = Field(default=None, ge=0.0)
    response_opportunity_count: int = Field(ge=0)
    responded_count: int = Field(ge=0)
    response_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    average_response_seconds: float | None = Field(default=None, ge=0.0)
    average_sentiment_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    sentiment_counts: dict[str, int] = Field(default_factory=dict)
    topic_counts: dict[str, int] = Field(default_factory=dict)
    entity_counts: dict[str, int] = Field(default_factory=dict)
    engagement_counts: dict[str, int] = Field(default_factory=dict)
    provenance: MetricProvenance
    window: AnalyticsWindow
    unavailable_reasons: dict[str, str] = Field(default_factory=dict)


GraphScalar = StrictStr | StrictInt | FiniteFloat | StrictBool | None
GraphProperty = GraphScalar | list[GraphScalar]


class GraphNode(AnalyticsModel):
    """Engine-neutral property-graph node with a stable idempotency key."""

    node_id: str = Field(min_length=1)
    account_ref: AccountRef = Field(
        validation_alias=AliasChoices("account_ref", "partition_key")
    )
    kind: GraphNodeKind
    occurred_at: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("account_ref", mode="before")
    @classmethod
    def normalize_partition(cls, value: Any) -> str:
        return validated_account_ref(value)

    @model_validator(mode="after")
    def validate_graph_identity_and_properties(self) -> "GraphNode":
        require_graph_id(self.node_id, expected_kind=self.kind.value)
        require_opaque_ref(self.account_ref, "account")
        if self.occurred_at is not None:
            self.occurred_at = _utc_datetime(self.occurred_at)
        self.properties = validate_node_properties(self.kind.value, self.properties)
        return self

    @property
    def partition_key(self) -> str:
        """Internal compatibility name; serialized records expose account_ref."""

        return self.account_ref


class GraphEdge(AnalyticsModel):
    """Engine-neutral directed edge with a stable idempotency key."""

    edge_id: str = Field(min_length=1)
    account_ref: AccountRef = Field(
        validation_alias=AliasChoices("account_ref", "partition_key")
    )
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    relation: GraphRelation
    occurred_at: datetime | None = None
    sequence: int | None = Field(default=None, ge=0)
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("account_ref", mode="before")
    @classmethod
    def normalize_partition(cls, value: Any) -> str:
        return validated_account_ref(value)

    @model_validator(mode="after")
    def validate_graph_identity_and_properties(self) -> "GraphEdge":
        require_graph_id(self.edge_id, expected_kind="edge")
        require_graph_id(self.source_id)
        require_graph_id(self.target_id)
        require_opaque_ref(self.account_ref, "account")
        if self.occurred_at is not None:
            self.occurred_at = _utc_datetime(self.occurred_at)
        self.properties = validate_edge_properties(self.relation.value, self.properties)
        return self

    @property
    def partition_key(self) -> str:
        """Internal compatibility name; serialized records expose account_ref."""

        return self.account_ref


class GraphNeighborhood(AnalyticsModel):
    root_node_id: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False
    visited_count: int = Field(default=0, ge=0)


class GraphTraversalBounds(AnalyticsModel):
    """Complete account, type, time, queue, and wall-clock traversal envelope."""

    account_ref: AccountRef = Field(
        validation_alias=AliasChoices("account_ref", "creator_account_id")
    )
    start_time: datetime
    end_time: datetime
    max_hops: int = Field(ge=0, le=16)
    max_results: int = Field(gt=0, le=10_000)
    max_visited: int = Field(gt=0, le=100_000)
    max_queue: int = Field(default=10_000, gt=0, le=100_000)
    max_edges_examined: int = Field(default=200_000, gt=0, le=1_000_000)
    wall_clock_ms: int = Field(default=1_000, gt=0, le=30_000)
    node_kinds: set[GraphNodeKind] = Field(
        default_factory=lambda: set(GraphNodeKind)
    )
    edge_kinds: set[GraphRelation] = Field(
        default_factory=lambda: set(GraphRelation)
    )
    include_timeless: bool = False
    root_policy: Literal["require_in_scope", "include_only"] = "require_in_scope"

    @field_validator("account_ref", mode="before")
    @classmethod
    def normalize_partition(cls, value: Any) -> str:
        return validated_account_ref(value)

    @model_validator(mode="after")
    def validate_window(self) -> "GraphTraversalBounds":
        require_opaque_ref(self.account_ref, "account")
        self.start_time = _utc_datetime(self.start_time)
        self.end_time = _utc_datetime(self.end_time)
        if self.start_time > self.end_time:
            raise ValueError("graph_time_window_invalid")
        return self

    @property
    def creator_account_id(self) -> str:
        return self.account_ref


class GraphAlgorithmBounds(AnalyticsModel):
    """Complete account/type/time/work envelope for NetworkX materialization."""

    account_ref: AccountRef = Field(
        validation_alias=AliasChoices("account_ref", "creator_account_id")
    )
    start_time: datetime
    end_time: datetime
    max_hops: int = Field(ge=0, le=16)
    max_nodes: int = Field(gt=0, le=50_000)
    max_edges: int = Field(gt=0, le=200_000)
    max_queue: int = Field(default=50_000, gt=0, le=100_000)
    wall_clock_ms: int = Field(default=5_000, gt=0, le=30_000)
    node_kinds: set[GraphNodeKind] = Field(
        default_factory=lambda: set(GraphNodeKind)
    )
    edge_kinds: set[GraphRelation] = Field(
        default_factory=lambda: set(GraphRelation)
    )
    include_timeless: bool = False
    root_node_id: str | None = None
    root_policy: Literal["require_in_scope", "include_only"] = "require_in_scope"

    @field_validator("account_ref", mode="before")
    @classmethod
    def normalize_partition(cls, value: Any) -> str:
        return validated_account_ref(value)

    @model_validator(mode="after")
    def validate_window(self) -> "GraphAlgorithmBounds":
        require_opaque_ref(self.account_ref, "account")
        self.start_time = _utc_datetime(self.start_time)
        self.end_time = _utc_datetime(self.end_time)
        if self.start_time > self.end_time:
            raise ValueError("graph_time_window_invalid")
        if self.max_queue < min(self.max_nodes, 1):
            raise ValueError("graph_bounds_invalid")
        return self

    @property
    def creator_account_id(self) -> str:
        return self.account_ref


class GraphPath(AnalyticsModel):
    node_ids: list[str] = Field(min_length=1)
    edge_ids: list[str]


class GraphPathResult(AnalyticsModel):
    paths: list[GraphPath]
    truncated: bool = False
    visited_count: int = Field(default=0, ge=0)


class GraphDegreeResult(AnalyticsModel):
    degree: int = Field(ge=0)
    truncated: bool = False


class GraphCommunity(AnalyticsModel):
    community_id: str
    node_ids: list[str]


class GraphCentralityResult(AnalyticsModel):
    algorithm: str
    parameter_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    values: dict[str, float]
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    source_edge_count: int = Field(ge=0)
    algorithm_edge_count: int = Field(ge=0)
    truncated: bool = False


class GraphCommunityResult(AnalyticsModel):
    algorithm: str
    parameter_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    communities: list[GraphCommunity]
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    source_edge_count: int = Field(ge=0)
    algorithm_edge_count: int = Field(ge=0)
    truncated: bool = False


class GraphProjectionSummary(AnalyticsModel):
    account_ref: AccountRef
    source_revision: int = Field(ge=0)
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    node_counts_by_kind: dict[str, int] = Field(default_factory=dict)
    edge_counts_by_relation: dict[str, int] = Field(default_factory=dict)


class AnalyticsProjection(AnalyticsModel):
    """Complete replayable projection of one canonical account revision."""

    schema_version: Literal["3"] = "3"
    availability: Literal[AvailabilityStatus.AVAILABLE] = AvailabilityStatus.AVAILABLE
    pipeline_revision: str = Field(min_length=1)
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    account_ref: AccountRef = Field(
        validation_alias=AliasChoices("account_ref", "creator_account_id")
    )
    source_revision: int = Field(ge=0)
    projection_generation: int = Field(ge=1)
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    analyzers: list[AnalyzerProvenance]
    window: AnalyticsWindow
    message_enrichments: list[MessageEnrichment]
    conversation_metrics: list[ConversationMetrics]
    creator_metrics: CreatorMetrics
    graph: GraphProjectionSummary
    projection_digest: Sha256Digest = Field(
        validation_alias=AliasChoices("projection_digest", "content_digest")
    )

    @property
    def content_digest(self) -> str:
        """Internal compatibility name for callers predating the public rename."""

        return self.projection_digest


class RebuildArtifact(AnalyticsModel):
    """Stable serialization returned by the analytics rebuild entry point."""

    projection: AnalyticsProjection
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class CanonicalMessage(AnalyticsModel):
    """Validated analytics view of a canonical read-model message."""

    message_id: str
    source_ordinal: int = Field(ge=0)
    text: str
    sent_at: AwareDatetime
    direction: MessageDirection
    sentiment: str | None = None


class CanonicalConversation(AnalyticsModel):
    """Validated analytics view of a canonical read-model conversation."""

    conversation_id: str
    platform_user_id: str
    display_name: str | None = None
    unread_count: int = Field(default=0, ge=0)
    last_message_at: AwareDatetime | None = None
    messages: list[CanonicalMessage] = Field(default_factory=list)


def analytics_json_schema() -> dict[str, Any]:
    """Return the standalone projection schema for adapters and diagnostics."""

    return AnalyticsProjection.model_json_schema()
