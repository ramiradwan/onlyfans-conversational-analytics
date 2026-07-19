"""Payload models for the single WebSocket protocol v2 schema."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from .common import (
    AnalyticsView, CapabilityStatus, CommandAction, CommandError, CommandOutput,
    ConversationSummary, HealthSummary, HistoricalCoverage, LastPresenceObservation,
    LiveFreshness, MAX_SNAPSHOT_FRAME_BYTES, MAX_SNAPSHOT_RECORD_BYTES,
    MAX_SNAPSHOT_RECORDS_PER_CHUNK, NonEmptyString, NonNegativeInt, ProjectionState,
    RawIngestChange, SnapshotChatRecord, SnapshotMessageRecordUnion, StateChange,
    StrictModel, Timestamp,
)

AgentCapability = Literal[
    "capture.chats", "capture.messages", "capture.presence", "history.sync", "command.message.send"
]
BridgeCapability = Literal["state.snapshot", "state.delta", "presence.state", "message.page"]


class AgentHelloPayload(StrictModel):
    auth_ticket: NonEmptyString
    agent_installation_id: UUID
    requested_creator_account_id: NonEmptyString
    capabilities: Annotated[list[AgentCapability], Field(min_length=1)]
    extension_version: NonEmptyString
    agent_stream_id: UUID
    last_acknowledged_source_seq: NonNegativeInt
    applied_config_revision: str | None


class LeaseParameters(StrictModel):
    heartbeat_interval_seconds: Annotated[int, Field(gt=0, le=300)]
    lease_timeout_seconds: Annotated[int, Field(gt=0, le=900)]


class AgentSessionPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    agent_installation_id: UUID
    agent_stream_id: UUID
    committed_source_seq: NonNegativeInt
    resume_action: Literal["resume", "snapshot_required"]
    pending_snapshot_id: UUID | None
    next_expected_chunk_index: NonNegativeInt
    required_config_revision: NonEmptyString
    reconnect_auth_ticket: NonEmptyString
    config_auth_ticket: NonEmptyString
    lease: LeaseParameters


class BridgeHelloPayload(StrictModel):
    auth_ticket: NonEmptyString
    bridge_session_id: UUID
    requested_creator_account_id: NonEmptyString
    capabilities: Annotated[list[BridgeCapability], Field(min_length=1)]
    client_version: NonEmptyString
    last_view_revision: NonNegativeInt | None


class BridgeSessionPayload(StrictModel):
    connection_id: UUID
    bridge_session_id: UUID
    creator_account_id: NonEmptyString
    negotiated_protocol_version: Literal["2"]
    server_version: NonEmptyString


class AgentHeartbeatPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    applied_config_revision: str | None
    health: HealthSummary


class SnapshotRequirements(StrictModel):
    include_chats: Literal[True]
    include_messages: Literal[True]
    include_coverage_evidence: Literal[True]
    max_records_per_chunk: Literal[100]
    max_frame_bytes: Literal[524288]


class SyncRequiredPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    reason: Literal["unknown_stream", "missing_checkpoint", "sequence_gap", "local_reset", "invariant_failed"]
    expected_agent_stream_id: UUID | None
    expected_next_source_seq: NonNegativeInt
    pending_snapshot_id: UUID | None
    next_expected_chunk_index: NonNegativeInt
    snapshot: SnapshotRequirements


class SnapshotRecordCounts(StrictModel):
    chats: NonNegativeInt
    messages: NonNegativeInt
    coverage_evidence: NonNegativeInt


class SnapshotIdentity(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    agent_installation_id: UUID
    agent_stream_id: UUID
    snapshot_id: UUID


class IngestSnapshotBeginPayload(SnapshotIdentity):
    frame_kind: Literal["begin"]
    through_seq: NonNegativeInt
    chunk_count: NonNegativeInt
    record_counts: SnapshotRecordCounts
    max_frame_bytes: Literal[524288]


class IngestSnapshotChunkPayload(SnapshotIdentity):
    frame_kind: Literal["chunk"]
    chunk_index: NonNegativeInt
    entity_kind: Literal["chat", "message", "coverage_evidence"]
    records: list[dict]

    @model_validator(mode="after")
    def validate_records(self) -> "IngestSnapshotChunkPayload":
        if not 1 <= len(self.records) <= MAX_SNAPSHOT_RECORDS_PER_CHUNK:
            raise ValueError("snapshot chunks require 1..100 records")
        if self.entity_kind == "chat":
            adapter = TypeAdapter(list[SnapshotChatRecord])
        elif self.entity_kind == "message":
            adapter = TypeAdapter(list[SnapshotMessageRecordUnion])
        else:
            from .common import CoverageEvidence

            adapter = TypeAdapter(list[CoverageEvidence])
        # The records arrive as JSON. Validate in JSON mode so strict timestamp
        # fields can parse their RFC 3339 representation before UTC normalization.
        validated = adapter.validate_json(json.dumps(self.records))
        normalized = [item.model_dump(mode="json") for item in validated]
        for record in normalized:
            size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            if size > MAX_SNAPSHOT_RECORD_BYTES:
                raise ValueError("individual snapshot record exceeds 384 KiB")
        self.records = normalized
        return self


class IngestSnapshotCommitPayload(SnapshotIdentity):
    frame_kind: Literal["commit"]
    chunk_count: NonNegativeInt


IngestSnapshotPayload = Annotated[
    Union[IngestSnapshotBeginPayload, IngestSnapshotChunkPayload, IngestSnapshotCommitPayload],
    Field(discriminator="frame_kind"),
]


class IngestDeltaPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    agent_installation_id: UUID
    event_id: UUID
    agent_stream_id: UUID
    source_seq: Annotated[int, Field(gt=0)]
    acquisition_origin: Literal["passive", "signer"]
    change: RawIngestChange


class SnapshotProgress(StrictModel):
    snapshot_id: UUID
    next_expected_chunk_index: NonNegativeInt
    committed: bool


class IngestAckPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    agent_stream_id: UUID
    snapshot_id: UUID | None
    committed_source_seq: NonNegativeInt
    snapshot_progress: SnapshotProgress | None


class IngestRejectedPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    rejected_message_id: UUID
    event_id: UUID | None
    code: Literal[
        "invalid_payload", "identity_conflict", "stale_fence", "sequence_gap", "invariant_failed",
        "chunk_conflict", "snapshot_incomplete", "frame_too_large"
    ]
    retryable: bool
    detail: NonEmptyString


class StateSnapshotPayload(StrictModel):
    creator_account_id: NonEmptyString
    view_revision: NonNegativeInt
    generated_at: Timestamp
    conversations: list[ConversationSummary]
    analytics: AnalyticsView
    coverage: HistoricalCoverage
    projection: ProjectionState
    live_freshness: LiveFreshness


class StateDeltaPayload(StrictModel):
    creator_account_id: NonEmptyString
    view_revision: Annotated[int, Field(gt=0)]
    committed_at: Timestamp
    changes: Annotated[list[StateChange], Field(min_length=1, max_length=100)]


class StateResyncPayload(StrictModel):
    connection_id: UUID
    bridge_session_id: UUID
    creator_account_id: NonEmptyString
    last_applied_view_revision: NonNegativeInt
    reason: Literal["revision_gap", "invalid_delta", "reconnect", "manual"]


class PresenceObservedPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    observation_id: NonNegativeInt
    observed_at: Timestamp
    online_platform_user_ids: list[NonEmptyString]


class PresenceStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    freshness: Literal["current", "unknown"]
    online_platform_user_ids: list[NonEmptyString]
    server_received_at: Timestamp | None
    expires_at: Timestamp | None
    last_observation: LastPresenceObservation | None


class AgentStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    status: Literal["connected", "stale", "disconnected"]
    agent_installation_id: UUID | None
    connection_id: UUID | None
    required_config_revision: NonEmptyString
    applied_config_revision: str | None
    required_history_settings_revision: NonNegativeInt
    applied_history_settings_revision: NonNegativeInt | None
    last_heartbeat_at: Timestamp | None
    degraded_reason: str | None


class SystemStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    processing_mode: Literal["processing_snapshot", "realtime", "resyncing"]
    readiness: Literal["ready", "degraded", "unavailable"]
    updated_at: Timestamp
    detail: str | None


class ProtocolErrorPayload(StrictModel):
    code: Literal[
        "unsupported_version", "wrong_role", "pre_handshake", "identity_conflict",
        "validation_failed", "unauthorized", "internal_error"
    ]
    related_message_id: UUID | None
    retryable: bool
    fatal: bool
    detail: NonEmptyString


class ConfigAvailablePayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    required_config_revision: NonEmptyString
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ConfigAppliedPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    config_revision: NonEmptyString
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    outcome: Literal["applied", "degraded", "rejected"]
    capabilities: Annotated[list[CapabilityStatus], Field(min_length=1)]


class CommandExecutePayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    command_id: UUID
    deadline: Timestamp
    idempotency_policy: Literal["deduplicate"]
    action: CommandAction


class CommandResultPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    command_id: UUID
    result_id: UUID
    status: Literal["accepted", "succeeded", "failed"]
    completed_at: Timestamp
    output: CommandOutput | None
    error: CommandError | None


class CommandResultAckPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    command_id: UUID
    result_id: UUID
    recorded_at: Timestamp
