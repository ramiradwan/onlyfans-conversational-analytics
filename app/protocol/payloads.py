"""Payload models for all 23 protocol v1 WebSocket operations."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .common import (
    AnalyticsView,
    CapabilityStatus,
    CommandAction,
    CommandError,
    CommandOutput,
    ConversationView,
    HealthSummary,
    LastPresenceObservation,
    NonEmptyString,
    NonNegativeInt,
    RawChat,
    RawIngestChange,
    RawMessage,
    Sha256Digest,
    StateChange,
    StrictModel,
)

AgentCapability = Literal[
    "capture.chats",
    "capture.messages",
    "capture.presence",
    "command.message.send",
]
BridgeCapability = Literal["state.snapshot", "state.delta", "presence.state"]


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
    required_config_revision: NonEmptyString
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
    negotiated_protocol_version: Literal["1"]
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


class SyncRequiredPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    reason: Literal["unknown_stream", "missing_checkpoint", "sequence_gap", "local_reset", "invariant_failed"]
    expected_agent_stream_id: UUID | None
    expected_next_source_seq: NonNegativeInt
    snapshot: SnapshotRequirements


class IngestSnapshotPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    agent_installation_id: UUID
    snapshot_id: UUID
    agent_stream_id: UUID
    through_seq: NonNegativeInt
    chats: list[RawChat]
    messages: list[RawMessage]


class IngestDeltaPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    agent_installation_id: UUID
    event_id: UUID
    agent_stream_id: UUID
    source_seq: Annotated[int, Field(gt=0)]
    change: RawIngestChange


class IngestAckPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    agent_stream_id: UUID
    snapshot_id: UUID | None
    committed_source_seq: NonNegativeInt


class IngestRejectedPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    rejected_message_id: UUID
    event_id: UUID | None
    code: Literal["invalid_payload", "identity_conflict", "stale_fence", "sequence_gap", "invariant_failed"]
    retryable: bool
    detail: NonEmptyString


class StateSnapshotPayload(StrictModel):
    creator_account_id: NonEmptyString
    view_revision: NonNegativeInt
    generated_at: datetime
    conversations: list[ConversationView]
    analytics: AnalyticsView


class StateDeltaPayload(StrictModel):
    creator_account_id: NonEmptyString
    view_revision: Annotated[int, Field(gt=0)]
    committed_at: datetime
    changes: Annotated[list[StateChange], Field(min_length=1)]


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
    observed_at: datetime
    online_platform_user_ids: list[NonEmptyString]


class PresenceStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    freshness: Literal["current", "unknown"]
    online_platform_user_ids: list[NonEmptyString]
    server_received_at: datetime | None
    expires_at: datetime | None
    last_observation: LastPresenceObservation | None


class AgentStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    status: Literal["connected", "stale", "disconnected"]
    agent_installation_id: UUID | None
    connection_id: UUID | None
    required_config_revision: NonEmptyString
    applied_config_revision: str | None
    last_heartbeat_at: datetime | None
    degraded_reason: str | None


class SystemStatePayload(StrictModel):
    creator_account_id: NonEmptyString
    processing_mode: Literal["processing_snapshot", "realtime", "resyncing"]
    readiness: Literal["ready", "degraded", "unavailable"]
    updated_at: datetime
    detail: str | None


class ProtocolErrorPayload(StrictModel):
    code: Literal[
        "unsupported_version",
        "wrong_role",
        "pre_handshake",
        "identity_conflict",
        "validation_failed",
        "unauthorized",
        "internal_error",
    ]
    related_message_id: UUID | None
    retryable: bool
    fatal: bool
    detail: NonEmptyString


class ConfigAvailablePayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    required_config_revision: NonEmptyString
    digest: Sha256Digest


class ConfigAppliedPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    config_revision: NonEmptyString
    digest: Sha256Digest
    outcome: Literal["applied", "degraded", "rejected"]
    capabilities: Annotated[list[CapabilityStatus], Field(min_length=1)]


class CommandExecutePayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    command_id: UUID
    deadline: datetime
    idempotency_policy: Literal["deduplicate"]
    action: CommandAction


class CommandResultPayload(StrictModel):
    connection_id: UUID
    fencing_token: NonEmptyString
    creator_account_id: NonEmptyString
    command_id: UUID
    result_id: UUID
    status: Literal["accepted", "succeeded", "failed"]
    completed_at: datetime
    output: CommandOutput | None
    error: CommandError | None


class CommandResultAckPayload(StrictModel):
    connection_id: UUID
    creator_account_id: NonEmptyString
    command_id: UUID
    result_id: UUID
    recorded_at: datetime
