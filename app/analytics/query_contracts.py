"""Closed records for read-only questions and their supporting sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.models.analytics import AccountRef, ConversationRef, MessageRef, Sha256Digest

QuestionId = Literal["no_later_creator_reply.v1", "pricing_discussions.v1"]
Coverage = Literal["complete", "partial", "unknown"]
Count = Annotated[StrictInt, Field(ge=0)]


def utc_instant(value: object) -> datetime:
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("question_timestamp_invalid") from None
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("question_timestamp_requires_offset")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError("question_timestamp_invalid") from None


Instant = Annotated[datetime, BeforeValidator(utc_instant)]


class QuestionRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class QuestionFilters(QuestionRecord):
    conversation_ref: ConversationRef | None = None
    language: Literal["en"] | None = None


class QuestionPlan(QuestionRecord):
    question: QuestionId
    start: Instant
    end: Instant
    timezone: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    filters: QuestionFilters = Field(default_factory=QuestionFilters)
    cutoff: Instant | None = None
    sort: Literal["evidence_time_desc"] = "evidence_time_desc"
    page_size: Annotated[StrictInt, Field(ge=1, le=200)] = 50
    cursor: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("question_timezone_unavailable") from None
        return value

    @model_validator(mode="after")
    def valid_selection(self) -> QuestionPlan:
        if not self.start < self.end or (
            self.cutoff is not None and self.end > self.cutoff
        ):
            raise ValueError("question_window_invalid")
        if (
            self.question == "no_later_creator_reply.v1"
            and self.filters.language is not None
        ):
            raise ValueError("question_filter_unsupported")
        return self


class SourceSpan(QuestionRecord):
    start: Count
    end: Count

    @model_validator(mode="after")
    def nonempty(self) -> SourceSpan:
        if self.start >= self.end:
            raise ValueError("question_span_invalid")
        return self


class QuestionEvidence(QuestionRecord):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    message_ref: MessageRef
    source_revision: Count
    source_version_digest: Sha256Digest
    sent_at: Instant
    span: SourceSpan | None = None


class QuestionRow(QuestionRecord):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    latest_evidence_at: Instant
    reason: Literal["no_later_creator_reply", "pricing_discussion"]
    coverage: Coverage
    evidence: Annotated[
        tuple[QuestionEvidence, ...], Field(min_length=1, max_length=32)
    ]
    evidence_truncated: StrictBool = False

    @model_validator(mode="after")
    def consistent_evidence(self) -> QuestionRow:
        if any(
            e.account_ref != self.account_ref
            or e.conversation_ref != self.conversation_ref
            for e in self.evidence
        ):
            raise ValueError("question_evidence_scope_invalid")
        if len({e.message_ref for e in self.evidence}) != len(self.evidence):
            raise ValueError("question_evidence_duplicate")
        if self.latest_evidence_at != max(e.sent_at for e in self.evidence):
            raise ValueError("question_evidence_time_invalid")
        return self


class PagePosition(QuestionRecord):
    evidence_at: Instant
    conversation_ref: ConversationRef

    def precedes(self, other: PagePosition) -> bool:
        return self.evidence_at > other.evidence_at or (
            self.evidence_at == other.evidence_at
            and self.conversation_ref < other.conversation_ref
        )


class QuestionSnapshot(QuestionRecord):
    account_ref: AccountRef
    source_revision: Count
    projection_generation: Annotated[StrictInt, Field(ge=1)]
    generation_id: Annotated[
        StrictStr, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")
    ]
    canonical_content_digest: Sha256Digest
    projection_digest: Sha256Digest
    derived_at: Instant
    source_message_count: Count
    retention_due_at: Instant | None

    @model_validator(mode="after")
    def retention_is_known(self) -> QuestionSnapshot:
        if (self.source_message_count == 0) != (self.retention_due_at is None):
            raise ValueError("question_retention_missing")
        return self


class QuestionCoverage(QuestionRecord):
    history: Coverage
    ordering: Literal["source", "inferred", "unknown"]
    evaluated_start: Instant | None = None
    evaluated_end: Instant | None = None
    eligible_classification_count: Count | None = None
    analyzed_classification_count: Count | None = None
    supported_languages: Annotated[tuple[Literal["en"], ...], Field(max_length=1)] = ()

    @model_validator(mode="after")
    def consistent_coverage(self) -> QuestionCoverage:
        if (self.evaluated_start is None) != (self.evaluated_end is None):
            raise ValueError("question_coverage_window_invalid")
        if (
            self.evaluated_start is not None
            and self.evaluated_start > self.evaluated_end
        ):
            raise ValueError("question_coverage_window_invalid")
        eligible = self.eligible_classification_count
        analyzed = self.analyzed_classification_count
        if (eligible is None) != (analyzed is None) or (
            eligible is not None and analyzed > eligible
        ):
            raise ValueError("question_classification_coverage_invalid")
        return self


class QuestionPage(QuestionRecord):
    rows: Annotated[tuple[QuestionRow, ...], Field(max_length=200)]
    coverage: QuestionCoverage
    evaluated_conversation_count: Count
    undetermined_conversation_count: Count
    total_matching_conversations: Count | None = None
    has_more: StrictBool = False
    truncated: StrictBool = False

    @model_validator(mode="after")
    def consistent_page(self) -> QuestionPage:
        if (
            self.evaluated_conversation_count
            < len(self.rows) + self.undetermined_conversation_count
        ):
            raise ValueError("question_counts_invalid")
        if self.has_more and not self.rows:
            raise ValueError("question_page_cannot_advance")
        if self.truncated and (
            self.total_matching_conversations is not None or self.has_more
        ):
            raise ValueError("question_truncated_total_invalid")
        if (
            self.total_matching_conversations is not None
            and self.total_matching_conversations < len(self.rows)
        ):
            raise ValueError("question_total_invalid")
        return self


class ResolvedQuestion(QuestionRecord):
    plan: QuestionPlan
    account_ref: AccountRef
    cutoff: Instant
    retention_cutoff_exclusive: Instant
    selection_clipped_by_retention: StrictBool
    definition_digest: Sha256Digest
    snapshot: QuestionSnapshot


class QuestionResult(QuestionRecord):
    schema_version: Literal["analytics-question-result.v1"] = (
        "analytics-question-result.v1"
    )
    availability: Literal["available"] = "available"
    counting_unit: Literal["conversations"] = "conversations"
    question: ResolvedQuestion
    page: QuestionPage
    checked_at: Instant
    next_cursor: Annotated[StrictStr, Field(max_length=4096)] | None = None
