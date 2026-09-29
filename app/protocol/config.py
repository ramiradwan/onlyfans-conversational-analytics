"""HTTPS Agent configuration schema v2."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator
from datetime import date

from .common import (
    CapturePolicy,
    CommandPolicy,
    HistoryAcquisitionPolicy,
    NonEmptyString,
    StrictModel,
    Timestamp,
    NonNegativeInt,
    PositiveInt,
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


class CatchupRequest(StrictModel):
    protocol_version: Literal["2"]
    auth_ticket: NonEmptyString
    agent_installation_id: UUID
    creator_account_id: NonEmptyString


class CaptureTabs(StrictModel):
    armed: NonNegativeInt
    frozen: NonNegativeInt
    discarded: NonNegativeInt


class CaptureDrops(StrictModel):
    expired: NonNegativeInt
    rejected: NonNegativeInt


class CaptureRequests(StrictModel):
    canary_list: NonNegativeInt
    catchup_list: NonNegativeInt
    catchup_messages: NonNegativeInt
    history_list: NonNegativeInt
    history_messages: NonNegativeInt
    identity: NonNegativeInt
    retries: NonNegativeInt


class CaptureStateReportRequest(CatchupRequest):
    operation: Literal["capture.state.report"]
    worker_instance_id: UUID
    report_seq: PositiveInt
    observing: bool
    reason: Literal["ok", "capture_off", "consent_needed", "no_onlyfans_tab", "tab_frozen", "tab_discarded", "hook_not_armed", "reload_required", "page_socket_closed", "account_mismatch", "storage_locked", "paused"]
    tabs: CaptureTabs
    page_socket_open: bool
    drops_since_last: CaptureDrops
    requests_since_last: CaptureRequests
    utc_day: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    automatic_pages_today: NonNegativeInt

    @field_validator("utc_day")
    @classmethod
    def valid_utc_day(cls, value):
        date.fromisoformat(value)
        return value


class CaptureStateReportResponse(StrictModel):
    acknowledged_seq: PositiveInt


class HistoryCheckBeginRequest(CatchupRequest):
    operation: Literal["history.check.begin"]
    request_id: UUID
    worker_instance_id: UUID
    trigger: Literal["admission", "tab_runnable", "observing", "alarm", "renew"]
    config_revision: NonEmptyString
    head_evidence: Literal["none", "timestamp", "full"]
    active_check_id: UUID | None


class HistoryCheckNotNeeded(StrictModel):
    result: Literal["not_needed"]


class HistoryCheckDeferred(StrictModel):
    result: Literal["deferred"]
    retry_after_seconds: PositiveInt
    reason: Literal["grant_interval", "daily_cap", "not_runnable", "history_incomplete", "check_active"]


class HistoryCheckGranted(StrictModel):
    result: Literal["granted"]
    check_id: UUID
    kind: Literal["catch_up", "canary"]
    gap_epoch: NonNegativeInt
    uncertain_since: Timestamp | None
    granted_at: Timestamp
    blind: bool
    page_budget: PositiveInt
    lease_expires_at: Timestamp
    resume: bool


HistoryCheckBeginResponse = Annotated[
    HistoryCheckNotNeeded | HistoryCheckDeferred | HistoryCheckGranted,
    Field(discriminator="result"),
]
