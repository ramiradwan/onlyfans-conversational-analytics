"""Local saved report over incoming, source-backed literal mentions."""
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import Field, model_validator

from app.models.analytics import AnalyticsWindow
from app.models.question_evidence import AccountRef, Count, QuestionEvidence, QuestionRecord, Sha256Digest


class RecurringMentionsRequest(QuestionRecord):
    expected_generation: Annotated[int, Field(strict=True, ge=1)]
    expected_projection_digest: Sha256Digest
    phrase_id: Sha256Digest | None = None
    expected_normalization_sha256: Sha256Digest | None = None
    row_offset: Annotated[int, Field(strict=True, ge=0, le=100_000)] = 0
    row_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    evidence_offset: Annotated[int, Field(strict=True, ge=0, le=1_000_000)] = 0
    evidence_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50

    @model_validator(mode="after")
    def pinned_selection(self):
        if (self.phrase_id or self.row_offset or self.evidence_offset) and not self.expected_normalization_sha256:
            raise ValueError("A selected or paged report needs its original publication.")
        return self


class RecurringMention(QuestionRecord):
    phrase_id: Sha256Digest
    literal_key: Annotated[str, Field(min_length=1, max_length=256)]
    supporting_senders: Annotated[int, Field(strict=True, ge=2)]
    supporting_messages: Count
    occurrences: Count

    @model_validator(mode="after")
    def distinct_support(self):
        if not self.supporting_senders <= self.supporting_messages <= self.occurrences:
            raise ValueError("Invalid recurring mention counts.")
        return self


class MentionEvidence(QuestionRecord):
    phrase_id: Sha256Digest
    assessment: Literal["complete", "partial"]
    reference: QuestionEvidence

    @model_validator(mode="after")
    def exact_span(self):
        if self.reference.span is None:
            raise ValueError("An exact source span is required.")
        return self


class MentionCoverage(QuestionRecord):
    eligible_messages: Count
    complete_messages: Count
    partial_messages: Count
    pending_messages: Count
    failed_messages: Count
    uncertain_messages: Count
    unsupported_messages: Count
    scan_complete: bool
    unrepresentable_occurrences: Count


T = TypeVar("T")


class ReportPage(QuestionRecord, Generic[T]):
    offset: Count
    limit: Annotated[int, Field(ge=1, le=100)]
    total: Count
    has_more: bool
    items: Annotated[tuple[T, ...], Field(max_length=100)]


class RecurringMentionsReport(QuestionRecord):
    schema_version: Literal["analytics-recurring-mentions.v1"] = "analytics-recurring-mentions.v1"
    account_ref: AccountRef
    source_revision: Count
    projection_generation: Annotated[int, Field(ge=1)]
    projection_digest: Sha256Digest
    snapshot_sha256: Sha256Digest
    normalization_sha256: Sha256Digest | None
    phrase_id: Sha256Digest | None
    requested_window: AnalyticsWindow
    effective_window: AnalyticsWindow
    availability: Literal["available", "unavailable"]
    unavailable_reason: Literal["task_not_declared", "not_prepared", "capacity"] | None
    coverage: MentionCoverage | None
    row_page: ReportPage[RecurringMention]
    evidence_page: ReportPage[MentionEvidence]
