"""Versioned records for a bounded, source-bound analytics exchange."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr

from app.analytics.query_contracts import (
    Coverage, Instant, QuestionEvidence, QuestionRecord, QuestionSnapshot,
)
from app.models.analytics import (
    AccountRef, ConversationRef, RebuildArtifact, Sha256Digest,
)


class ExchangeClassification(QuestionRecord):
    status: Literal["positive", "negative", "uncertain", "unsupported", "missing", "error"]
    input_version_digest: Sha256Digest | None
    language: Annotated[StrictStr, Field(min_length=1, max_length=64)] | None
    method: Literal["declared-fixture.v1"] = "declared-fixture.v1"
    configuration_digest: Sha256Digest


class ExchangeObservation(QuestionRecord):
    evidence: QuestionEvidence
    role: Literal["participant", "creator", "system", "unknown"]
    kind: Literal["message", "attachment", "system", "unknown"]
    order: Annotated[StrictInt, Field(ge=0)] | None
    ordering: Literal["source", "inferred"]
    classification: ExchangeClassification

    @property
    def message_ref(self) -> str:
        return self.evidence.message_ref


class ExchangeConversation(QuestionRecord):
    conversation_ref: ConversationRef
    coverage: Coverage


class ExchangeFacts(QuestionRecord):
    schema_version: Literal["analytics-question-facts.v1"] = "analytics-question-facts.v1"
    conversations: Annotated[tuple[ExchangeConversation, ...], Field(max_length=10000)]
    observations: Annotated[tuple[ExchangeObservation, ...], Field(max_length=10000)]


class ExchangeBundle(QuestionRecord):
    schema_version: Literal["analytics-exchange.v1"] = "analytics-exchange.v1"
    profile: Literal["synthetic-conformance.v1"] = "synthetic-conformance.v1"
    artifact: RebuildArtifact
    snapshot: QuestionSnapshot
    facts: ExchangeFacts
    source_facts_digest: Sha256Digest
    question_definitions: dict[StrictStr, Sha256Digest]
    content_digest: Sha256Digest


class ExchangeSourceBinding(QuestionRecord):
    """Expected authority supplied independently of the imported document."""

    account_ref: AccountRef
    source_revision: Annotated[StrictInt, Field(ge=0)]
    canonical_content_digest: Sha256Digest
    source_facts_digest: Sha256Digest


class PublishedExchange(QuestionRecord):
    account_ref: AccountRef
    generation_id: StrictStr
    content_digest: Sha256Digest
    source: ExchangeSourceBinding
    retention_due_at: Instant | None
