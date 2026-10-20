"""Compiled-adapter ABI declarations and fixed-message validation."""
from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from app.nlp.contracts import Contract, Digest, Identifier, NlpBoundaryError
from app.nlp.profiles import (AdapterIdentity, AnalysisProfile, Artifact, EngineIdentity,
                              ScoreKind, TaskContract)
from app.nlp.v2 import WorkerRequest, WorkerResponse, _decode


class AdapterCapability(Contract):
    adapter: AdapterIdentity
    task_contracts: Annotated[tuple[TaskContract, ...], Field(min_length=1, max_length=2)]
    score_kinds: Annotated[tuple[ScoreKind, ...], Field(min_length=1, max_length=4)]
    max_source_codepoints: Annotated[int, Field(ge=1, le=131_072)]
    max_batch_items: Annotated[int, Field(ge=1, le=16)]
    max_windows: Annotated[int, Field(ge=1, le=256)]
    max_candidates: Annotated[int, Field(ge=1, le=8192)]
    max_output_findings: Annotated[int, Field(ge=1, le=576)]
    runtime_requirements_sha256: Digest

    @model_validator(mode="after")
    def unique_members(self):
        if len(set(self.task_contracts)) != len(self.task_contracts) or len(set(self.score_kinds)) != len(self.score_kinds):
            raise ValueError("duplicate capability member")
        return self


class HandshakeRequest(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    expected_engine: EngineIdentity


class WorkerHandshake(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    engine: EngineIdentity
    adapters: Annotated[tuple[AdapterCapability, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def unique_adapters(self):
        identities = [(cap.adapter.adapter_id, cap.adapter.adapter_version) for cap in self.adapters]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate adapter capability")
        return self


class ProfileCompatibility(Contract):
    """An exact compatibility declaration, with no metadata authentication claim."""
    profile_sha256: Digest
    compatible_engines: Annotated[tuple[EngineIdentity, ...], Field(min_length=1, max_length=32)]
    adapters: Annotated[tuple[AdapterIdentity, ...], Field(min_length=1, max_length=2)]


def validate_compatibility(profile: AnalysisProfile, declaration: ProfileCompatibility) -> None:
    if declaration.profile_sha256 != profile.identity_digest():
        raise NlpBoundaryError("nlp_profile_mismatch")
    if profile.components[0].engine not in declaration.compatible_engines:
        raise NlpBoundaryError("nlp_engine_mismatch")
    expected = {component.adapter.model_dump_json() for component in profile.components}
    actual = {item.model_dump_json() for item in declaration.adapters}
    if actual != expected or len(actual) != len(declaration.adapters):
        raise NlpBoundaryError("nlp_adapter_incompatible")


def validate_handshake(profile: AnalysisProfile, request: HandshakeRequest, frame: bytes) -> WorkerHandshake:
    """Capabilities narrow an admitted descriptor; they cannot approve one."""
    handshake = _decode(WorkerHandshake, frame, "handshake")
    if handshake.request_id != request.request_id or handshake.engine != request.expected_engine:
        raise NlpBoundaryError("nlp_engine_mismatch")
    for component in profile.components:
        if component.engine != handshake.engine:
            raise NlpBoundaryError("nlp_engine_mismatch")
        capabilities = [cap for cap in handshake.adapters if cap.adapter == component.adapter]
        if len(capabilities) != 1:
            raise NlpBoundaryError("nlp_adapter_incompatible")
        capability = capabilities[0]
        if capability.runtime_requirements_sha256 != component.runtime_requirements.sha256:
            raise NlpBoundaryError("nlp_adapter_incompatible")
        if (not {task.task_contract for task in component.tasks} <= set(capability.task_contracts) or
                not {task.score_policy.kind for task in component.tasks} <= set(capability.score_kinds)):
            raise NlpBoundaryError("nlp_adapter_incompatible")
        for limit in ("max_source_codepoints", "max_batch_items", "max_windows", "max_candidates", "max_output_findings"):
            if getattr(component, limit) > getattr(capability, limit):
                raise NlpBoundaryError("nlp_adapter_incompatible")
    return handshake


class PrepareRequest(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    profile_sha256: Digest
    component_id: Identifier
    component_sha256: Digest
    artifacts: Annotated[tuple[Artifact, ...], Field(min_length=1, max_length=256)]
    max_memory_bytes: Annotated[int, Field(ge=1, le=536_870_912)]
    inference_threads: Annotated[int, Field(ge=1, le=2)]


class PreparationResult(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    request_id: Identifier
    profile_sha256: Digest
    component_id: Identifier
    component_sha256: Digest
    outcome: Literal["ready", "refused", "cancelled", "failed"]
    reason: Literal["none", "incompatible", "artifact_mismatch", "resource_limit", "cancelled", "adapter_failure"]

    @model_validator(mode="after")
    def coherent(self):
        if (self.outcome == "ready") != (self.reason == "none"):
            raise ValueError("preparation outcome mismatch")
        return self


def validate_prepare_request(profile: AnalysisProfile, frame: bytes) -> PrepareRequest:
    request = _decode(PrepareRequest, frame, "prepare_request")
    if request.profile_sha256 != profile.identity_digest():
        raise NlpBoundaryError("nlp_profile_mismatch")
    try:
        component = profile.component(request.component_id)
    except ValueError:
        raise NlpBoundaryError("nlp_component_mismatch") from None
    if request.component_sha256 != component.identity_digest():
        raise NlpBoundaryError("nlp_component_mismatch")
    expected = {artifact.artifact_id: artifact for artifact in profile.artifacts
                if artifact.artifact_id in component.artifact_ids or artifact.role in ("engine", "runtime")}
    actual = {artifact.artifact_id: artifact for artifact in request.artifacts}
    if len(actual) != len(request.artifacts) or actual != expected:
        raise NlpBoundaryError("nlp_artifact_mismatch")
    if (request.max_memory_bytes != profile.resources.transition_peak_memory_bytes or
            request.inference_threads != profile.resources.inference_threads):
        raise NlpBoundaryError("nlp_resource_mismatch")
    return request


def validate_preparation(request: PrepareRequest, frame: bytes) -> PreparationResult:
    response = _decode(PreparationResult, frame, "preparation")
    for field in ("request_id", "profile_sha256", "component_id", "component_sha256"):
        if getattr(response, field) != getattr(request, field):
            raise NlpBoundaryError("nlp_preparation_mismatch")
    return response


class CancelRequest(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    operation: Literal["cancel"] = "cancel"
    request_id: Identifier
    target_request_id: Identifier
    profile_sha256: Digest
    component_sha256: Digest


class UnloadRequest(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    operation: Literal["unload"] = "unload"
    request_id: Identifier
    profile_sha256: Digest
    component_sha256: Digest


class LifecycleResult(Contract):
    protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    operation: Literal["cancel", "unload"]
    request_id: Identifier
    profile_sha256: Digest
    component_sha256: Digest
    target_request_id: Identifier | None = None
    outcome: Literal["completed", "busy", "failed"]
    health: Literal["ready", "unloaded", "failed"]

    @model_validator(mode="after")
    def coherent(self):
        if (self.operation == "cancel") != (self.target_request_id is not None):
            raise ValueError("lifecycle target mismatch")
        if self.operation == "unload" and self.outcome == "completed" and self.health != "unloaded":
            raise ValueError("completed unload must release residency")
        return self


def validate_lifecycle(request: CancelRequest | UnloadRequest, frame: bytes) -> LifecycleResult:
    response = _decode(LifecycleResult, frame, "lifecycle")
    for field in ("operation", "request_id", "profile_sha256", "component_sha256"):
        if getattr(response, field) != getattr(request, field):
            raise NlpBoundaryError("nlp_lifecycle_mismatch")
    if response.target_request_id != getattr(request, "target_request_id", None):
        raise NlpBoundaryError("nlp_lifecycle_mismatch")
    return response


class CompiledAdapter(Protocol):
    """Structural interface only; artifact verification and admission are caller-owned."""

    def describe(self, request: HandshakeRequest) -> WorkerHandshake: ...
    def prepare(self, request: PrepareRequest) -> PreparationResult: ...
    def analyze(self, request: WorkerRequest) -> WorkerResponse: ...
    def cancel(self, request: CancelRequest) -> LifecycleResult: ...
    def unload(self, request: UnloadRequest) -> LifecycleResult: ...
