"""Bounded reads of source-backed literal values in saved findings."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.analytics.query_contracts import Count, QuestionEvidence, QuestionRecord
from app.models.analytics import AccountRef, AnalyticsWindow, ConversationRef, Sha256Digest
from app.models.insights import FindingsCategoryFilter, FindingsRequest, FindingsTask
from app.nlp.contracts import Identifier
from app.nlp.profiles import PolicyIdentity


class LiteralFindingsRequest(FindingsRequest):
    row_offset: Annotated[int, Field(strict=True, ge=0, le=100_000)] = 0
    row_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    node_id: Sha256Digest | None = None
    expected_normalization_sha256: Sha256Digest | None = None

    @model_validator(mode='after')
    def pinned_pages(self):
        if self.conversation_offset != 0 or self.conversation_limit != 50:
            raise ValueError('literal reads do not page conversations')
        if (self.node_id is not None or self.row_offset or self.evidence_offset or self.conversation_offset) and self.expected_normalization_sha256 is None:
            raise ValueError('literal publication required for selected values or pages')
        return self


class LiteralFindingsRow(QuestionRecord):
    node_id: Sha256Digest
    type_id: Identifier
    category_id: Identifier
    display_label: Annotated[str, Field(min_length=1, max_length=256)]
    label_status: Literal['resolved', 'id_only']
    label_identity_sha256: Sha256Digest
    literal_key: Annotated[str, Field(min_length=1, max_length=256)]
    supporting_conversations: Count
    supporting_messages: Count
    occurrences: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count

    @model_validator(mode='after')
    def counts(self):
        if (not 0 < self.supporting_conversations <= self.supporting_messages <= self.occurrences
                or self.complete_supporting_messages + self.partial_supporting_messages != self.supporting_messages):
            raise ValueError('literal support counts invalid')
        return self


class LiteralRowsPage(QuestionRecord):
    offset: Count
    limit: Annotated[int, Field(ge=1, le=100)]
    total: Count
    has_more: bool
    items: Annotated[tuple[LiteralFindingsRow, ...], Field(max_length=100)]


class LiteralEvidence(QuestionRecord):
    node_id: Sha256Digest
    category_id: Identifier
    assessment: Literal['complete', 'partial']
    reference: QuestionEvidence

    @model_validator(mode='after')
    def exact_occurrence(self):
        if self.reference.span is None:
            raise ValueError('literal evidence requires an exact span')
        return self


class LiteralEvidencePage(QuestionRecord):
    offset: Count
    limit: Annotated[int, Field(ge=1, le=100)]
    total: Count
    has_more: bool
    items: Annotated[tuple[LiteralEvidence, ...], Field(max_length=100)]


class LiteralCoverage(QuestionRecord):
    eligible_occurrences: Count
    mapped_occurrences: Count
    unrepresentable_occurrences: Count
    supporting_messages: Count

    @model_validator(mode='after')
    def accounted(self):
        if self.mapped_occurrences + self.unrepresentable_occurrences != self.eligible_occurrences:
            raise ValueError('literal occurrence accounting invalid')
        return self


class LiteralFindingsResponse(QuestionRecord):
    schema_version: Literal['analytics-literals.v1'] = 'analytics-literals.v1'
    account_ref: AccountRef
    source_revision: Count
    projection_generation: Annotated[int, Field(ge=1)]
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    snapshot_sha256: Sha256Digest
    profile_sha256: Sha256Digest
    presentation: PolicyIdentity
    requested_window: AnalyticsWindow
    effective_window: AnalyticsWindow
    direction: Literal['all', 'inbound', 'outbound']
    conversation_ref: ConversationRef | None
    category_filter: FindingsCategoryFilter | None
    node_id: Sha256Digest | None
    availability: Literal['available', 'unavailable']
    unavailable_reason: Literal['task_not_declared', 'not_prepared', 'capacity'] | None
    normalization_sha256: Sha256Digest | None
    normalization_policy: PolicyIdentity | None
    extraction: FindingsTask | None
    normalization_coverage: LiteralCoverage | None
    row_page: LiteralRowsPage
    evidence_page: LiteralEvidencePage

    @model_validator(mode='after')
    def availability_fields(self):
        if self.extraction is not None and self.extraction.task != 'concept_mentions':
            raise ValueError('literal extraction task mismatch')
        if self.availability == 'available':
            if (self.unavailable_reason is not None or self.normalization_sha256 is None
                    or self.normalization_policy is None or self.normalization_coverage is None
                    or self.extraction is None):
                raise ValueError('literal publication missing')
        elif (self.unavailable_reason is None or self.normalization_sha256 is not None
                or self.normalization_policy is not None or self.normalization_coverage is not None
                or self.row_page.total or self.row_page.items or self.evidence_page.total or self.evidence_page.items):
            raise ValueError('unavailable literal publication has results')
        if (self.unavailable_reason == 'task_not_declared') != (self.extraction is None):
            raise ValueError('literal task availability mismatch')
        if self.node_id is not None and any(item.node_id != self.node_id for item in self.evidence_page.items):
            raise ValueError('literal evidence selection mismatch')
        return self
