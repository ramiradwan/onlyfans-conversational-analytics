"""HTTPS Agent configuration schema v2."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .common import (
    CapturePolicy,
    CommandPolicy,
    HistoryAcquisitionPolicy,
    NonEmptyString,
    StrictModel,
    Timestamp,
)


class AgentConfigGetRequest(StrictModel):
    operation: Literal["agent.config.get"]
    protocol_version: Literal["2"]
    auth_ticket: NonEmptyString
    agent_installation_id: UUID
    creator_account_id: NonEmptyString
    current_etag: str | None
    current_config_revision: str | None
    supported_config_schema_versions: Annotated[list[Literal["2"]], Field(min_length=1)]


class AgentConfigDocumentResponse(StrictModel):
    operation: Literal["agent.config.document"]
    protocol_version: Literal["2"]
    creator_account_id: NonEmptyString
    config_revision: NonEmptyString
    config_schema_version: Literal["2"]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    etag: NonEmptyString
    issued_at: Timestamp
    capture_policy: CapturePolicy
    command_policy: CommandPolicy
    history_acquisition: HistoryAcquisitionPolicy
