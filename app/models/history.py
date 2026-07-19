"""Authenticated REST contracts for history settings and paged message reads."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from app.protocol.common import (
    ConversationCoverage,
    MessageView,
    NonEmptyString,
    NonNegativeInt,
    ProjectionState,
    StrictModel,
    Timestamp,
)


class HistorySettingsResponse(StrictModel):
    creator_account_id: NonEmptyString
    settings_revision: NonNegativeInt
    consent_policy_version: NonEmptyString
    consent_revision: str | None
    authorized_platform_creator_id: str | None
    desired_state: Literal["not_started", "running", "paused", "revoked"]
    effective_state: Literal["not_applied", "running", "paused", "revoked"]
    effective_config_revision: str | None
    recent_window_days: Annotated[int, Field(ge=1, le=365)]
    page_size: Annotated[int, Field(ge=1, le=100)]
    pages_per_wake: Annotated[int, Field(ge=1)]
    request_interval_ms: NonNegativeInt
    retry_limit: NonNegativeInt
    updated_at: Timestamp


class UpdateHistorySettingsRequest(StrictModel):
    desired_state: Literal["running", "paused"]
    consent_policy_version: str | None
    accept_consent: bool
    recent_window_days: Annotated[int, Field(ge=1, le=365)]
    page_size: Annotated[int, Field(ge=1, le=100)]
    pages_per_wake: Annotated[int, Field(ge=1, le=100)]
    request_interval_ms: Annotated[int, Field(ge=100, le=60_000)]
    retry_limit: Annotated[int, Field(ge=0, le=20)]


class MessagePageResponse(StrictModel):
    creator_account_id: NonEmptyString
    conversation_id: NonEmptyString
    projection_generation: NonEmptyString
    read_revision: NonNegativeInt
    generated_at: Timestamp
    items: list[MessageView]
    older_cursor: str | None
    has_older_stored_items: bool
    conversation_coverage: ConversationCoverage
    projection: ProjectionState


class AgentPairingResponse(StrictModel):
    pairing_ticket: NonEmptyString
    expires_at: Timestamp
