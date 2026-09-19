"""Local source records and versioned references for analytics evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictStr

from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import (
    Count, Instant, QuestionEvidence, QuestionRecord, SourceSpan,
)
from app.analytics.query_execution import QuestionBudget

SourceId = Annotated[StrictStr, Field(min_length=1, max_length=512)]
MAX_EVIDENCE_TEXT_CHARS = 65_536


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

    def reference(self, span: SourceSpan | None = None) -> QuestionEvidence:
        """Describe this exact message version without copying its text."""

        payload = self.model_dump(mode="json", exclude={"source_revision"})
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


class EvidenceSource(Protocol):
    def read_evidence_message(
        self, account_id: str, location: EvidenceLocation, budget: QuestionBudget,
    ) -> EvidenceMessage | None: ...
