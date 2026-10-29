"""Local source records and versioned references for analytics evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictStr, model_validator

from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import (
    Count, Instant, QuestionEvidence, QuestionRecord, SourceSpan,
)
from app.analytics.query_execution import QuestionBudget

SourceId = Annotated[StrictStr, Field(min_length=1, max_length=512)]
MAX_EVIDENCE_TEXT_CHARS = 65_536
MAX_EVIDENCE_CONTEXT_BYTES = 1_048_576
SafeGeneration = Annotated[int, Field(strict=True, ge=1, le=9_007_199_254_740_991)]


class EvidenceLocation(QuestionRecord):
    conversation_id: SourceId = Field(repr=False)
    message_id: SourceId = Field(repr=False)


class EvidenceMessage(QuestionRecord):
    account_id: SourceId = Field(repr=False)
    location: EvidenceLocation = Field(repr=False)
    source_revision: Count
    text: Annotated[StrictStr, Field(max_length=MAX_EVIDENCE_TEXT_CHARS, repr=False)]
    sent_at: Instant
    direction: Literal["inbound", "outbound"]
    sender_id: SourceId = Field(repr=False)
    upstream_updated_at: Instant | None = None
    content_hash: Annotated[StrictStr, Field(min_length=1, max_length=128, repr=False)]
    stream_epoch: Count
    source_sequence: Count
    source_state_token: Annotated[StrictStr, Field(max_length=128)] | None = Field(default=None, repr=False)

    def reference(self, span: SourceSpan | None = None) -> QuestionEvidence:
        """Describe this exact message version without copying its text."""

        payload = self.model_dump(mode="json", exclude={"source_revision", "source_state_token"})
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(b"ofca:evidence-message:v1\0" + encoded).hexdigest()
        return QuestionEvidence(
            account_ref=account_ref(self.account_id),
            conversation_ref=conversation_ref(self.account_id, self.location.conversation_id),
            message_ref=message_ref(self.account_id, self.location.conversation_id,
                                    self.location.message_id),
            source_revision=self.source_revision,
            source_version_digest="sha256:" + digest,
            sent_at=self.sent_at,
            span=span,
        )


class ResolvedEvidence(QuestionRecord):
    reference: QuestionEvidence
    location: EvidenceLocation = Field(repr=False)
    text: Annotated[StrictStr, Field(max_length=MAX_EVIDENCE_TEXT_CHARS, repr=False)]
    direction: Literal["inbound", "outbound"]
    checked_at: Instant
    browser_span: SourceSpan | None = None


class EvidenceContextRequest(QuestionRecord):
    reference: QuestionEvidence
    expected_generation: SafeGeneration
    expected_projection_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)]
    before: Annotated[int, Field(strict=True, ge=0, le=5)] = 5
    after: Annotated[int, Field(strict=True, ge=0, le=5)] = 5


class EvidenceContextItem(QuestionRecord):
    reference: QuestionEvidence
    text: Annotated[StrictStr, Field(max_length=MAX_EVIDENCE_TEXT_CHARS, repr=False)]
    direction: Literal["inbound", "outbound"]
    browser_span: SourceSpan | None = None


class ResolvedEvidenceContext(QuestionRecord):
    schema_version: Literal["analytics-evidence-context.v1"] = "analytics-evidence-context.v1"
    reference: QuestionEvidence
    projection_generation: SafeGeneration
    projection_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)]
    checked_at: Instant
    anchor_index: Annotated[int, Field(strict=True, ge=0, le=10)]
    items: Annotated[tuple[EvidenceContextItem, ...], Field(min_length=1, max_length=11)]
    has_earlier_stored_items: bool
    has_later_stored_items: bool

    @model_validator(mode="after")
    def coherent_context(self):
        if self.anchor_index >= len(self.items) or self.items[self.anchor_index].reference != self.reference:
            raise ValueError("evidence_context_anchor_invalid")
        seen, previous = set(), None
        for index, item in enumerate(self.items):
            ref = item.reference
            if (ref.account_ref != self.reference.account_ref
                    or ref.conversation_ref != self.reference.conversation_ref
                    or ref.source_revision != self.reference.source_revision
                    or ref.message_ref in seen or (previous is not None and ref.sent_at < previous)):
                raise ValueError("evidence_context_source_invalid")
            seen.add(ref.message_ref)
            previous = ref.sent_at
            span = ref.span
            if index != self.anchor_index and span is not None:
                raise ValueError("evidence_context_neighbor_span")
            expected = None if span is None else SourceSpan(
                start=len(item.text[:span.start].encode("utf-16-le")) // 2,
                end=len(item.text[:span.end].encode("utf-16-le")) // 2)
            if (span is not None and span.end > len(item.text)) or item.browser_span != expected:
                raise ValueError("evidence_context_span_invalid")
        if len(self.model_dump_json().encode("utf-8")) > MAX_EVIDENCE_CONTEXT_BYTES:
            raise ValueError("evidence_context_byte_limit")
        return self


class EvidenceSource(Protocol):
    def read_evidence_message(
        self, account_id: str, location: EvidenceLocation, budget: QuestionBudget,
    ) -> EvidenceMessage | None: ...
