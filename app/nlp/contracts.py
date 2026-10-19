"""Versioned optional NLP boundary. Brain alone admits and publishes findings."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_FRAME_BYTES = 1_048_576


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EngineIdentity(Contract):
    package_name: Identifier
    package_family: Identifier
    core_package_name: Identifier
    core_package_version: Identifier
    publisher: Annotated[str, Field(min_length=1, max_length=512)]
    package_version: Identifier
    related_set_id: Identifier
    engine_version: Identifier
    platform: Literal["windows-x64"]
    worker_protocol: Literal["nlp-worker.v1"] = "nlp-worker.v1"


class ExecutionIdentity(Contract):
    engine: EngineIdentity
    pack_id: Identifier
    pack_sha256: Digest
    checkpoint_revision: Identifier
    tokenizer_sha256: Digest
    recipe_id: Literal["topics_mentions_v1"]
    recipe_sha256: Digest
    taxonomy_version: Identifier
    taxonomy_sha256: Digest
    result_schema: Literal["nlp-result.v1"] = "nlp-result.v1"
    language_assumption: Literal["en"] = "en"


class PackCompatibility(Contract):
    """Authenticated exact allow-list, not arbitrary independently versioned engines."""
    pack_id: Identifier
    pack_sha256: Digest
    compatible_engines: Annotated[tuple[EngineIdentity, ...], Field(min_length=1, max_length=32)]
    download_bytes: Annotated[int, Field(ge=1, le=262_144_000)]
    installed_bytes: Annotated[int, Field(ge=1, le=536_870_912)]
    temporary_bytes: Annotated[int, Field(ge=0)]
    enabled_topic_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)]
    enabled_concept_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)]


class Span(Contract):
    """Half-open Unicode codepoint offsets into unchanged original source text."""
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("empty or reversed span")
        return self


class Coverage(Contract):
    source_codepoints: Annotated[int, Field(ge=0, le=131_072)]
    examined: Annotated[tuple[Span, ...], Field(max_length=256)]
    excluded: Annotated[tuple[Span, ...], Field(max_length=256)]
    limits_reached: Annotated[tuple[Literal["tokens", "windows", "candidates", "output"], ...], Field(max_length=4)]

    @model_validator(mode="after")
    def partition(self):
        spans = sorted(self.examined + self.excluded, key=lambda span: span.start)
        cursor = 0
        for span in spans:
            if span.start != cursor or span.end > self.source_codepoints:
                raise ValueError("coverage must partition original source")
            cursor = span.end
        if cursor != self.source_codepoints:
            raise ValueError("coverage must account for all source codepoints")
        return self

    @property
    def complete(self) -> bool:
        return not self.excluded and not self.limits_reached


class TopicFinding(Contract):
    topic_id: Identifier
    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class MentionFinding(Contract):
    concept_id: Identifier
    span: Span
    score: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


Outcome = Literal["assessed_findings", "assessed_no_match", "uncertain", "unsupported", "partial", "failed", "unprocessed"]


class TaskResult(Contract):
    outcome: Outcome
    coverage: Coverage
    reason: Literal["none", "below_threshold", "unsupported_input", "dependency_unavailable", "cancelled", "deadline", "worker_failure", "limit_reached"]
    topics: Annotated[tuple[TopicFinding, ...], Field(max_length=64)] = ()
    mentions: Annotated[tuple[MentionFinding, ...], Field(max_length=512)] = ()

    @model_validator(mode="after")
    def coherent(self):
        findings = self.topics + self.mentions
        if self.outcome == "assessed_findings" and (not findings or not self.coverage.complete):
            raise ValueError("assessed findings require complete coverage and findings")
        if self.outcome == "assessed_no_match" and (findings or not self.coverage.complete):
            raise ValueError("no-match requires complete coverage without findings")
        if self.outcome not in ("assessed_findings", "partial") and findings:
            raise ValueError("non-assessed outcome cannot publish findings")
        if self.outcome == "partial" and self.coverage.complete:
            raise ValueError("partial requires incomplete coverage")
        if self.outcome.startswith("assessed_") and self.reason != "none":
            raise ValueError("assessed outcome cannot carry failure reason")
        for mention in self.mentions:
            if not any(s.start <= mention.span.start < mention.span.end <= s.end for s in self.coverage.examined):
                raise ValueError("mention must belong to examined original source")
        keys = [(m.concept_id, m.span.start, m.span.end) for m in self.mentions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate occurrence coordinates")
        if len({t.topic_id for t in self.topics}) != len(self.topics):
            raise ValueError("duplicate topic")
        return self


class WorkerInput(Contract):
    request_item_id: Identifier
    text: Annotated[str, Field(max_length=131_072)]


class WorkerRequest(Contract):
    protocol: Literal["nlp-worker.v1"] = "nlp-worker.v1"
    request_id: Identifier
    execution: ExecutionIdentity
    deadline_ms: Annotated[int, Field(ge=1, le=120_000)]
    items: Annotated[tuple[WorkerInput, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def unique_items(self):
        if len({i.request_item_id for i in self.items}) != len(self.items):
            raise ValueError("duplicate request item")
        return self


class WorkerItemResult(Contract):
    request_item_id: Identifier
    topics: TaskResult
    mentions: TaskResult
    score_semantics: Literal["model_score_not_accuracy"] = "model_score_not_accuracy"
    calibration_status: Literal["unqualified", "qualified_per_category"]

    @model_validator(mode="after")
    def separate_tasks(self):
        if self.topics.mentions or self.mentions.topics:
            raise ValueError("findings belong to their independent task")
        if self.topics.coverage.source_codepoints != self.mentions.coverage.source_codepoints:
            raise ValueError("tasks must bind the same source length")
        return self


class WorkerResponse(Contract):
    protocol: Literal["nlp-worker.v1"] = "nlp-worker.v1"
    request_id: Identifier
    execution: ExecutionIdentity
    items: Annotated[tuple[WorkerItemResult, ...], Field(min_length=1, max_length=16)]


class SourceBinding(Contract):
    account_scope: Identifier
    source_ref: Identifier
    source_version: Digest
    speaker_ref: Identifier


class EnrichmentRecord(Contract):
    schema_version: Literal["nlp-result.v1"] = "nlp-result.v1"
    source: SourceBinding
    execution: ExecutionIdentity
    analyzed_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]
    result: WorkerItemResult


class EnrichmentSnapshot(Contract):
    """Immutable generation input; completion advances even without canonical edits."""
    account_scope: Identifier
    canonical_revision: Annotated[int, Field(ge=0)]
    completion_revision: Annotated[int, Field(ge=0)]
    availability_revision: Annotated[int, Field(ge=0)]
    execution: ExecutionIdentity
    findings_sha256: Digest
    source_validity_sha256: Digest

    def identity_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


class DeploymentResult(Contract):
    release_id: Identifier
    outcome: Literal["completed", "refused", "restart_required", "cancelled", "failed"]
    reason: Literal["none", "unavailable_channel", "identity_mismatch", "incompatible", "untrusted", "busy", "withdrawn", "platform_failure"]


class Readiness(Contract):
    engine_installation: Literal["absent", "installed"]
    verification: Literal["unverified", "verified", "rejected"]
    dependency: Literal["unavailable_channel", "absent", "acquiring", "incompatible", "ready", "withdrawn", "failed"]
    authorization: Literal["denied", "valid", "expired"]
    processing: Literal["disabled", "paused", "pending", "running", "idle", "refused"]
    results: Literal["unavailable", "pending", "partial", "current", "stale"]


def decode_request(frame: bytes) -> WorkerRequest:
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("worker frame exceeds byte limit")
    return WorkerRequest.model_validate_json(frame)


def validate_response(request: WorkerRequest, frame: bytes) -> WorkerResponse:
    """Brain checks the exact admitted request; no source text is persisted here."""
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("worker frame exceeds byte limit")
    response = WorkerResponse.model_validate_json(frame)
    if response.request_id != request.request_id or response.execution != request.execution:
        raise ValueError("stale or mismatched execution")
    originals = {i.request_item_id: i.text for i in request.items}
    returned = [i.request_item_id for i in response.items]
    if len(returned) != len(set(returned)) or set(returned) != set(originals):
        raise ValueError("missing, duplicate or unknown result item")
    for item in response.items:
        if item.topics.coverage.source_codepoints != len(originals[item.request_item_id]):
            raise ValueError("source codepoint length mismatch")
    return response
