"""HTTPS Agent configuration request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .common import (
    CapturePolicy,
    CommandPolicy,
    NonEmptyString,
    Sha256Digest,
    StrictModel,
)


class AgentConfigGetRequest(StrictModel):
    operation: Literal["agent.config.get"]
    protocol_version: Literal["1"]
    auth_ticket: NonEmptyString
    agent_installation_id: UUID
    creator_account_id: NonEmptyString
    current_etag: str | None
    current_config_revision: str | None
    supported_config_schema_versions: Annotated[list[Literal["1"]], Field(min_length=1)]


class AgentConfigDocumentResponse(StrictModel):
    operation: Literal["agent.config.document"]
    protocol_version: Literal["1"]
    creator_account_id: NonEmptyString
    config_revision: NonEmptyString
    config_schema_version: Literal["1"]
    digest: Sha256Digest
    etag: NonEmptyString
    issued_at: datetime
    capture_policy: CapturePolicy
    command_policy: CommandPolicy


