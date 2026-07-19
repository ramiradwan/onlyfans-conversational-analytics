"""Closed value objects for the single production protocol v2 schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "2"
MAX_SNAPSHOT_RECORDS_PER_CHUNK = 100
MAX_SNAPSHOT_FRAME_BYTES = 512 * 1024
MAX_SNAPSHOT_RECORD_BYTES = 384 * 1024

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonEmptyString = Annotated[str, Field(min_length=1)]


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    return value.astimezone(timezone.utc)


Timestamp = Annotated[datetime, AfterValidator(_normalized_utc)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RawChat(StrictModel):
    record_kind: Literal["placeholder", "full"]
    chat_id: NonEmptyString
    platform_user_id: NonEmptyString | None
    display_name: str | None
    updated_at: Timestamp | None

    @model_validator(mode="after")
    def full_chat_is_complete(self) -> "RawChat":
        if self.record_kind == "full" and (
            self.platform_user_id is None or self.updated_at is None
        ):
            raise ValueError("full chat records require platform_user_id and updated_at")
        return self


class RawMessage(StrictModel):
    message_id: NonEmptyString
    chat_id: NonEmptyString
    sender_platform_user_id: NonEmptyString
    text: str
    sent_at: Timestamp
    direction: Literal["inbound", "outbound"]


class SnapshotChatValue(StrictModel):
    tombstone: Literal[False]
    chat: RawChat


class SnapshotChatTombstone(StrictModel):
    tombstone: Literal[True]
    chat_id: NonEmptyString


class SnapshotMessageValue(StrictModel):
    tombstone: Literal[False]
    message: RawMessage


class SnapshotMessageTombstone(StrictModel):
    tombstone: Literal[True]
    message_id: NonEmptyString
    chat_id: NonEmptyString


SnapshotChatRecord = Annotated[
    Union[SnapshotChatValue, SnapshotChatTombstone], Field(discriminator="tombstone")
]
SnapshotMessageRecordUnion = Annotated[
    Union[SnapshotMessageValue, SnapshotMessageTombstone], Field(discriminator="tombstone")
]


class ChatUpsertChange(StrictModel):
    type: Literal["chat.upsert"]
    chat: RawChat


class ChatDeleteChange(StrictModel):
    type: Literal["chat.delete"]
    chat_id: NonEmptyString


class MessageUpsertChange(StrictModel):
    type: Literal["message.upsert"]
    message: RawMessage


class MessageDeleteChange(StrictModel):
    type: Literal["message.delete"]
    message_id: NonEmptyString
    chat_id: NonEmptyString


class CoverageGenerationStarted(StrictModel):
    type: Literal["generation.started"]
    generation_id: UUID
    as_of: Timestamp
    authorization_revision: NonEmptyString


class CoverageInventoryMember(StrictModel):
    type: Literal["inventory.member"]
    generation_id: UUID
    conversation_id: NonEmptyString


class CoverageInventoryEnded(StrictModel):
    type: Literal["inventory.ended"]
    generation_id: UUID
    observed_at: Timestamp


class CoverageConversationHistoryStarted(StrictModel):
    type: Literal["conversation.history_started"]
    generation_id: UUID
    conversation_id: NonEmptyString
    earliest_observed_at: Timestamp | None
    observed_at: Timestamp


class CoverageConversationHeadReconciled(StrictModel):
    type: Literal["conversation.head_reconciled"]
    generation_id: UUID
    conversation_id: NonEmptyString
    reconciled_through: Timestamp


class CoverageGenerationClosed(StrictModel):
    type: Literal["generation.closed"]
    generation_id: UUID
    closed_at: Timestamp


CoverageEvidence = Annotated[
    Union[
        CoverageGenerationStarted,
        CoverageInventoryMember,
        CoverageInventoryEnded,
        CoverageConversationHistoryStarted,
        CoverageConversationHeadReconciled,
        CoverageGenerationClosed,
    ],
    Field(discriminator="type"),
]


class CoverageObservedChange(StrictModel):
    type: Literal["coverage.observed"]
    evidence: CoverageEvidence


RawIngestChange = Annotated[
    Union[
        ChatUpsertChange,
        ChatDeleteChange,
        MessageUpsertChange,
        MessageDeleteChange,
        CoverageObservedChange,
    ],
    Field(discriminator="type"),
]


class MessageView(StrictModel):
    message_id: NonEmptyString
    text: str
    sent_at: Timestamp
    direction: Literal["inbound", "outbound"]
    sentiment: Literal["positive", "neutral", "negative", "unknown"] = "unknown"


class ConversationCoverage(StrictModel):
    status: Literal["unknown", "partial", "complete"]
    boundary: Literal["history_start"] | None
    earliest_available_at: Timestamp | None
    latest_acquired_at: Timestamp | None
    data_as_of: Timestamp | None
    reason_code: str | None


class ConversationSummary(StrictModel):
    conversation_id: NonEmptyString
    platform_user_id: str | None
    display_name: str | None
    unread_count: NonNegativeInt
    last_message_at: Timestamp | None
    latest_message: MessageView | None
    coverage: ConversationCoverage


class HistoricalCoverage(StrictModel):
    status: Literal["unknown", "partial", "complete"]
    phase: Literal[
        "not_started", "discovering", "backfilling", "paused", "repairing", "blocked", "complete"
    ]
    generation_id: UUID | None
    as_of: Timestamp | None
    discovered_conversations: NonNegativeInt | None
    complete_conversations: NonNegativeInt
    complete_as_of: Timestamp | None
    reason: str | None


class ProjectionState(StrictModel):
    status: Literal["pending", "current", "degraded", "unavailable"]
    canonical_revision: NonNegativeInt
    projected_revision: NonNegativeInt
    projected_at: Timestamp | None
    reason: str | None


class LiveFreshness(StrictModel):
    status: Literal["current", "delayed", "unknown"]
    last_observed_at: Timestamp | None
    last_committed_at: Timestamp | None
    expires_at: Timestamp | None
    pending_count: NonNegativeInt | None
    reason: str | None


class AnalyticsRange(StrictModel):
    start: Timestamp | None
    end: Timestamp | None


class AnalyticsMetric(StrictModel):
    value: NonNegativeInt | None
    basis: Literal["complete", "synced_subset"]
    observed_range: AnalyticsRange
    complete_range: AnalyticsRange | None
    sample_size: NonNegativeInt
    as_of: Timestamp
    projection_revision: NonNegativeInt


class AnalyticsView(StrictModel):
    total_conversations: AnalyticsMetric
    total_messages: AnalyticsMetric
    inbound_messages: AnalyticsMetric
    outbound_messages: AnalyticsMetric


class ConversationUpsertChange(StrictModel):
    type: Literal["conversation.upsert"]
    conversation: ConversationSummary


class ConversationDeleteChange(StrictModel):
    type: Literal["conversation.delete"]
    conversation_id: NonEmptyString


class ConversationCoverageReplaceChange(StrictModel):
    type: Literal["conversation.coverage.replace"]
    conversation_id: NonEmptyString
    coverage: ConversationCoverage


class MessageTailUpsertChange(StrictModel):
    type: Literal["message.tail.upsert"]
    conversation_id: NonEmptyString
    message: MessageView


class MessageTailDeleteChange(StrictModel):
    type: Literal["message.tail.delete"]
    conversation_id: NonEmptyString
    message_id: NonEmptyString


class AnalyticsReplaceChange(StrictModel):
    type: Literal["analytics.replace"]
    analytics: AnalyticsView


class CoverageReplaceChange(StrictModel):
    type: Literal["coverage.replace"]
    coverage: HistoricalCoverage


class ProjectionReplaceChange(StrictModel):
    type: Literal["projection.replace"]
    projection: ProjectionState


class LiveFreshnessReplaceChange(StrictModel):
    type: Literal["live_freshness.replace"]
    live_freshness: LiveFreshness


StateChange = Annotated[
    Union[
        ConversationUpsertChange,
        ConversationDeleteChange,
        ConversationCoverageReplaceChange,
        MessageTailUpsertChange,
        MessageTailDeleteChange,
        AnalyticsReplaceChange,
        CoverageReplaceChange,
        ProjectionReplaceChange,
        LiveFreshnessReplaceChange,
    ],
    Field(discriminator="type"),
]


class HealthSummary(StrictModel):
    status: Literal["healthy", "degraded"]
    detail: str | None


class LastPresenceObservation(StrictModel):
    observation_id: NonNegativeInt
    observed_at: Timestamp


class CapabilityStatus(StrictModel):
    capability: Literal[
        "capture.chats", "capture.messages", "capture.presence", "history.sync", "command.message.send"
    ]
    status: Literal["active", "degraded", "unsupported"]
    detail: str | None


class MessageSendAction(StrictModel):
    type: Literal["message.send"]
    conversation_id: NonEmptyString
    text: NonEmptyString
    media_url: str | None


CommandAction = Annotated[MessageSendAction, Field(discriminator="type")]


class CommandOutput(StrictModel):
    external_message_id: str | None


class CommandError(StrictModel):
    code: Literal["rejected", "deadline_exceeded", "platform_error", "execution_error"]
    detail: NonEmptyString
    retryable: bool


class CaptureRule(StrictModel):
    resource: Literal["chats", "messages", "presence"]
    url_pattern: NonEmptyString
    enabled: bool


class CapturePolicy(StrictModel):
    observation_interval_seconds: Annotated[int, Field(ge=5, le=3600)]
    rules: Annotated[list[CaptureRule], Field(min_length=1)]


class CommandPolicy(StrictModel):
    allowed_actions: list[Literal["message.send"]]
    max_text_length: PositiveInt
    require_idempotency: bool


class HistoryAcquisitionPolicy(StrictModel):
    enabled: bool
    consent_revision: NonEmptyString | None
    authorized_platform_creator_id: NonEmptyString | None
    recent_window_days: Annotated[int, Field(ge=1, le=365)]
    page_size: Annotated[int, Field(ge=1, le=100)]
    pages_per_wake: Annotated[int, Field(ge=1)]
    request_interval_ms: NonNegativeInt
    retry_limit: NonNegativeInt

    @model_validator(mode="after")
    def enabled_requires_authorization(self) -> "HistoryAcquisitionPolicy":
        if self.enabled and (
            not self.consent_revision or not self.authorized_platform_creator_id
        ):
            raise ValueError(
                "enabled history acquisition requires consent_revision and authorized_platform_creator_id"
            )
        return self
