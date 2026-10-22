"""Bounded private messages and derived-record contracts for message analysis."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, TypeVar

from pydantic import Field, model_validator

from app.nlp import contracts as v1
from app.nlp.contracts import Contract, Coverage, Digest, Identifier, NlpBoundaryError, SourceBinding, Span
from app.nlp.profiles import (AnalysisProfile, SubsetAnalysisProfile, ComponentDescriptor, Descriptor,
                              TASK_CONTRACTS, TaskName, canonical_bytes)

MAX_FRAME_BYTES = v1.MAX_FRAME_BYTES
MAX_RECORD_BYTES = 65_536
Finite = Annotated[float, Field(allow_inf_nan=False)]


class ProbabilityScore(Contract):
    kind: Literal["probability"]
    value: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class LogitScore(Contract):
    kind: Literal["logit"]
    value: Finite


class MarginScore(Contract):
    kind: Literal["margin"]
    value: Finite


Score = Annotated[ProbabilityScore | LogitScore | MarginScore, Field(discriminator="kind")]


class TopicFinding(Contract):
    topic_id: Identifier
    score: Score | None = None


class MentionFinding(Contract):
    concept_id: Identifier
    span: Span
    score: Score | None = None


class TaskOutcome(Contract):
    outcome: v1.Outcome
    coverage: Coverage
    reason: Literal["none", "below_threshold", "unsupported_input", "dependency_unavailable", "cancelled",
                    "deadline", "worker_failure", "limit_reached"]

    def check_outcome(self, findings: tuple):
        complete = self.coverage.complete
        if self.outcome == "assessed_findings" and (not findings or not complete):
            raise ValueError("assessed findings require complete coverage")
        if self.outcome == "assessed_no_match" and (findings or not complete):
            raise ValueError("no-match requires complete coverage")
        if self.outcome not in ("assessed_findings", "partial") and findings:
            raise ValueError("non-assessed outcome cannot publish findings")
        if self.outcome == "partial" and complete:
            raise ValueError("partial requires incomplete coverage")
        if self.outcome.startswith("assessed_") and self.reason != "none":
            raise ValueError("assessed outcome has failure reason")
        if len(set(self.coverage.limits_reached)) != len(self.coverage.limits_reached):
            raise ValueError("duplicate coverage limit")


class TopicResult(TaskOutcome):
    task: Literal["message_topics"]
    task_contract: Literal["message_topics.v1"] = "message_topics.v1"
    findings: Annotated[tuple[TopicFinding, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def coherent(self):
        self.check_outcome(self.findings)
        if len({finding.topic_id for finding in self.findings}) != len(self.findings):
            raise ValueError("duplicate topic")
        return self


class MentionResult(TaskOutcome):
    task: Literal["concept_mentions"]
    task_contract: Literal["concept_mentions.v1"] = "concept_mentions.v1"
    findings: Annotated[tuple[MentionFinding, ...], Field(max_length=512)] = ()

    @model_validator(mode="after")
    def coherent(self):
        self.check_outcome(self.findings)
        coordinates = [(finding.concept_id, finding.span.start, finding.span.end) for finding in self.findings]
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("duplicate occurrence coordinates")
        if any(not any(span.start <= finding.span.start < finding.span.end <= span.end
                       for span in self.coverage.examined) for finding in self.findings):
            raise ValueError("mention outside examined original source")
        return self


TaskResult = Annotated[TopicResult | MentionResult, Field(discriminator="task")]


class WorkerInput(Contract):
    request_item_id: Identifier
    text: Annotated[str, Field(max_length=131_072)]
    text_sha256: Digest

    @model_validator(mode="after")
    def source_digest(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("source digest mismatch")
        return self


class WorkerRequest(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    profile_sha256: Digest
    component_id: Identifier
    component_sha256: Digest
    tasks: Annotated[tuple[TaskName, ...], Field(min_length=1, max_length=2)]
    deadline_ms: Annotated[int, Field(ge=1, le=120_000)]
    items: Annotated[tuple[WorkerInput, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def unique_members(self):
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("duplicate requested task")
        if len({item.request_item_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate request item")
        return self


class BoundTaskResult(Contract):
    component_id: Identifier
    component_sha256: Digest
    text_sha256: Digest
    payload: TaskResult


class WorkerItemResult(Contract):
    request_item_id: Identifier
    tasks: Annotated[tuple[BoundTaskResult, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def unique_tasks(self):
        if len({task.payload.task for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate returned task")
        return self


class WorkerResponse(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    profile_sha256: Digest
    component_id: Identifier
    component_sha256: Digest
    items: Annotated[tuple[WorkerItemResult, ...], Field(min_length=1, max_length=16)]


def _validate_task(component: ComponentDescriptor, result: BoundTaskResult) -> None:
    if (result.component_id != component.component_id or
            result.component_sha256 != component.identity_digest()):
        raise NlpBoundaryError("nlp_component_mismatch")
    descriptors = {task.task: task for task in component.tasks}
    payload = result.payload
    if payload.task not in descriptors:
        raise NlpBoundaryError("nlp_task_set_mismatch")
    descriptor = descriptors[payload.task]
    categories = {finding.topic_id if isinstance(finding, TopicFinding) else finding.concept_id
                  for finding in payload.findings}
    if not categories <= set(descriptor.enabled_ids):
        raise NlpBoundaryError("nlp_category_mismatch")
    if len(payload.findings) > component.max_output_findings:
        raise NlpBoundaryError("nlp_output_limit")
    if payload.coverage.source_codepoints > component.max_source_codepoints:
        raise NlpBoundaryError("nlp_input_limit")
    for finding in payload.findings:
        if finding.score is not None and finding.score.kind != descriptor.score_policy.kind:
            raise NlpBoundaryError("nlp_score_policy_mismatch")


T = TypeVar("T", bound=Contract)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _decode(model: type[T], frame: bytes, kind: str, bound: int = MAX_FRAME_BYTES) -> T:
    if len(frame) > bound:
        raise NlpBoundaryError("nlp_frame_too_large")
    try:
        # Reject duplicate keys before typed parsing; JSON tuple semantics stay strict.
        json.loads(frame, object_pairs_hook=_unique_object)
        return model.model_validate_json(frame)
    except (ValueError, RecursionError, OverflowError):
        raise NlpBoundaryError("nlp_invalid_" + kind) from None


def decode_request(frame: bytes) -> WorkerRequest:
    return _decode(WorkerRequest, frame, "request")


def validate_request(profile: AnalysisProfile, frame: bytes) -> WorkerRequest:
    """Check an already admitted profile; this performs no authorization."""
    request = decode_request(frame)
    if request.profile_sha256 != profile.identity_digest():
        raise NlpBoundaryError("nlp_profile_mismatch")
    try:
        component = profile.component(request.component_id)
    except ValueError:
        raise NlpBoundaryError("nlp_component_mismatch") from None
    if request.component_sha256 != component.identity_digest():
        raise NlpBoundaryError("nlp_component_mismatch")
    if not set(request.tasks) <= {task.task for task in component.tasks}:
        raise NlpBoundaryError("nlp_task_set_mismatch")
    if len(request.items) > component.max_batch_items or any(
            len(item.text) > component.max_source_codepoints for item in request.items):
        raise NlpBoundaryError("nlp_input_limit")
    return request


def validate_response(profile: AnalysisProfile, request: WorkerRequest, frame: bytes) -> WorkerResponse:
    validate_request(profile, request.model_dump_json().encode())
    response = _decode(WorkerResponse, frame, "response")
    identity = (response.request_id, response.profile_sha256, response.component_id, response.component_sha256)
    if identity != (request.request_id, request.profile_sha256, request.component_id, request.component_sha256):
        raise NlpBoundaryError("nlp_execution_mismatch")
    originals = {item.request_item_id: item for item in request.items}
    returned = [item.request_item_id for item in response.items]
    if len(set(returned)) != len(returned) or set(returned) != set(originals):
        raise NlpBoundaryError("nlp_item_set_mismatch")
    component = profile.component(request.component_id)
    for item in response.items:
        if {task.payload.task for task in item.tasks} != set(request.tasks):
            raise NlpBoundaryError("nlp_task_set_mismatch")
        original = originals[item.request_item_id]
        if sum(len(task.payload.findings) for task in item.tasks) > component.max_output_findings:
            raise NlpBoundaryError("nlp_output_limit")
        for task in item.tasks:
            _validate_task(component, task)
            if task.text_sha256 != original.text_sha256:
                raise NlpBoundaryError("nlp_source_digest_mismatch")
            if task.payload.coverage.source_codepoints != len(original.text):
                raise NlpBoundaryError("nlp_source_length_mismatch")
    return response


class StoredTask(Contract):
    source: SourceBinding
    profile_sha256: Digest
    result: BoundTaskResult


class EnrichmentRecord(Contract):
    """A serialization contract; no storage writer is installed here."""
    schema_version: Literal["nlp-result.v2"] = "nlp-result.v2"
    source: SourceBinding
    profile: AnalysisProfile
    analyzed_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]
    tasks: Annotated[tuple[StoredTask, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def coherent(self):
        expected_profile = "nlp-profile.v3" if self.schema_version == "nlp-result.v3" else "nlp-profile.v2"
        if self.profile.schema_version != expected_profile:
            raise ValueError("record profile version mismatch")
        bindings = {binding.task: binding.component_id for binding in self.profile.bindings}
        if (len({task.result.payload.task for task in self.tasks}) != len(self.tasks) or
                {task.result.payload.task for task in self.tasks} != set(bindings)):
            raise ValueError("record task set mismatch")
        text_digests = {task.result.text_sha256 for task in self.tasks}
        lengths = {task.result.payload.coverage.source_codepoints for task in self.tasks}
        if len(text_digests) != 1 or len(lengths) != 1:
            raise ValueError("record source mismatch")
        for task in self.tasks:
            if task.source != self.source or task.profile_sha256 != self.profile.identity_digest():
                raise ValueError("record provenance mismatch")
            _validate_task(self.profile.component(bindings[task.result.payload.task]), task.result)
        for component in self.profile.components:
            if sum(len(task.result.payload.findings) for task in self.tasks
                   if task.result.component_id == component.component_id) > component.max_output_findings:
                raise ValueError("record output limit")
        return self


class SubsetEnrichmentRecord(EnrichmentRecord):
    schema_version: Literal["nlp-result.v3"] = "nlp-result.v3"
    profile: SubsetAnalysisProfile
    tasks: Annotated[tuple[StoredTask, ...], Field(min_length=1, max_length=2)]


class TaskSnapshot(Contract):
    task: TaskName
    task_contract: Literal["message_topics.v1", "concept_mentions.v1"]
    component_sha256: Digest
    availability: Literal["unavailable", "pending", "partial", "current", "stale"]
    completion_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    availability_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    findings_sha256: Digest
    coverage_sha256: Digest


class EnrichmentSnapshot(Descriptor):
    schema_version: Literal["nlp-snapshot.v2"] = "nlp-snapshot.v2"
    account_scope: Identifier
    canonical_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    completion_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    availability_revision: Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]
    profile: AnalysisProfile
    source_validity_sha256: Digest
    tasks: Annotated[tuple[TaskSnapshot, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def coherent(self):
        expected_profile = "nlp-profile.v3" if self.schema_version == "nlp-snapshot.v3" else "nlp-profile.v2"
        if self.profile.schema_version != expected_profile:
            raise ValueError("snapshot profile version mismatch")
        bindings = {binding.task: binding.component_id for binding in self.profile.bindings}
        if len({task.task for task in self.tasks}) != len(self.tasks) or {task.task for task in self.tasks} != set(bindings):
            raise ValueError("snapshot task set mismatch")
        for task in self.tasks:
            if (task.task_contract != TASK_CONTRACTS[task.task] or task.component_sha256 !=
                    self.profile.component(bindings[task.task]).identity_digest()):
                raise ValueError("snapshot provenance mismatch")
        return self


class SubsetEnrichmentSnapshot(EnrichmentSnapshot):
    schema_version: Literal["nlp-snapshot.v3"] = "nlp-snapshot.v3"
    profile: SubsetAnalysisProfile
    tasks: Annotated[tuple[TaskSnapshot, ...], Field(min_length=1, max_length=2)]


def decode_snapshot(frame: bytes) -> EnrichmentSnapshot | SubsetEnrichmentSnapshot:
    if len(frame) > MAX_RECORD_BYTES:
        raise NlpBoundaryError("nlp_frame_too_large")
    try:
        family = json.loads(frame, object_pairs_hook=_unique_object).get("schema_version", "nlp-snapshot.v2")
    except (ValueError, AttributeError, RecursionError):
        raise NlpBoundaryError("nlp_invalid_snapshot") from None
    if type(family) is not str:
        raise NlpBoundaryError("nlp_invalid_snapshot")
    model = {"nlp-snapshot.v2": EnrichmentSnapshot, "nlp-snapshot.v3": SubsetEnrichmentSnapshot}.get(family)
    if model is None:
        raise NlpBoundaryError("nlp_invalid_snapshot")
    return _decode(model, frame, "snapshot", MAX_RECORD_BYTES)


def read_record(frame: bytes) -> v1.EnrichmentRecord | EnrichmentRecord:
    """Read each family with its original semantics, without relabelling v1."""
    if len(frame) > MAX_RECORD_BYTES:
        raise NlpBoundaryError("nlp_frame_too_large")
    try:
        family = json.loads(frame, object_pairs_hook=_unique_object).get("schema_version")
    except (ValueError, AttributeError, RecursionError):
        raise NlpBoundaryError("nlp_invalid_record") from None
    if family == "nlp-result.v1":
        return _decode(v1.EnrichmentRecord, frame, "record", MAX_RECORD_BYTES)
    if family == "nlp-result.v2":
        return _decode(EnrichmentRecord, frame, "record", MAX_RECORD_BYTES)
    if family == "nlp-result.v3":
        return _decode(SubsetEnrichmentRecord, frame, "record", MAX_RECORD_BYTES)
    raise NlpBoundaryError("nlp_invalid_record")


def encode_record(record: EnrichmentRecord) -> bytes:
    frame = canonical_bytes(record)
    read_record(frame)
    return frame


def validate_record(profile: AnalysisProfile, source: SourceBinding, frame: bytes) -> EnrichmentRecord:
    """Conservative reuse requires the complete current profile and source binding."""
    record = read_record(frame)
    if not isinstance(record, EnrichmentRecord) or record.profile != profile:
        raise NlpBoundaryError("nlp_profile_mismatch")
    if record.source != source:
        raise NlpBoundaryError("nlp_source_binding_mismatch")
    return record
