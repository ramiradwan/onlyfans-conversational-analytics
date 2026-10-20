"""Immutable descriptors for the closed message-analysis task family."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.nlp.contracts import Contract, Digest, EngineIdentity as V1EngineIdentity, Identifier

TaskName = Literal["message_topics", "concept_mentions"]
TaskContract = Literal["message_topics.v1", "concept_mentions.v1"]
ScoreKind = Literal["probability", "logit", "margin", "none"]
TASK_CONTRACTS = {"message_topics": "message_topics.v1", "concept_mentions": "concept_mentions.v1"}
MAX_DOWNLOAD_BYTES = 262_144_000
MAX_INSTALLED_BYTES = 536_870_912
MAX_MEMORY_BYTES = 536_870_912
SafeInteger = Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]


def canonical_bytes(value: Contract) -> bytes:
    """Sorted ASCII JSON, all defaults included, no insignificant whitespace.

    Identity descriptors contain only strings, bounded integers and arrays/objects.
    Array order is significant; Unicode is preserved without normalization.
    """
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


class Descriptor(Contract):
    canonicalization: Literal["nlp-canonical.v1"] = "nlp-canonical.v1"

    def identity_digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self)).hexdigest()


class EngineIdentity(V1EngineIdentity):
    worker_protocol: Literal["nlp-worker.v2"] = "nlp-worker.v2"
    engine_build_sha256: Digest


class AdapterIdentity(Contract):
    adapter_id: Identifier
    adapter_version: Identifier
    adapter_build_sha256: Digest
    abi: Literal["nlp-adapter.v1"] = "nlp-adapter.v1"
    architecture: Identifier
    artifact_format: Identifier
    tokenizer_format: Identifier


class PolicyIdentity(Contract):
    policy_id: Identifier
    revision: Identifier
    sha256: Digest


class ScorePolicy(Contract):
    kind: ScoreKind
    semantics_id: Identifier
    calibration: Literal["not_calibrated", "calibrated"] = "not_calibrated"
    calibration_sha256: Digest | None = None

    @model_validator(mode="after")
    def calibration_identity(self):
        if (self.calibration == "calibrated") != (self.calibration_sha256 is not None):
            raise ValueError("calibration identity mismatch")
        if self.kind == "none" and self.calibration != "not_calibrated":
            raise ValueError("absent scores cannot be calibrated")
        return self


class TaskDescriptor(Contract):
    task: TaskName
    task_contract: TaskContract
    taxonomy: PolicyIdentity
    enabled_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)]
    score_policy: ScorePolicy

    @model_validator(mode="after")
    def closed_task(self):
        if self.task_contract != TASK_CONTRACTS[self.task]:
            raise ValueError("task contract mismatch")
        if len(set(self.enabled_ids)) != len(self.enabled_ids):
            raise ValueError("duplicate enabled category")
        return self


class Artifact(Contract):
    """A logical file identity; no caller-controlled filesystem path."""
    artifact_id: Identifier
    sha256: Digest
    size_bytes: Annotated[int, Field(ge=1, le=MAX_INSTALLED_BYTES)]
    role: Literal["engine", "runtime", "weights", "tokenizer", "configuration"]
    format_id: Identifier


def artifact_closure_digest(artifacts: tuple[Artifact, ...]) -> str:
    values = [asset.model_dump(mode="json") for asset in sorted(artifacts, key=lambda asset: asset.artifact_id)]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode("ascii")).hexdigest()


class ConditioningLabel(Contract):
    task: TaskName
    label_id: Identifier
    description: Annotated[str, Field(max_length=2048)]


class ComponentDescriptor(Descriptor):
    component_id: Identifier
    engine: EngineIdentity
    adapter: AdapterIdentity
    pack_id: Identifier
    pack_revision: Identifier
    pack_sha256: Digest
    model_revision: Identifier
    artifact_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=128)]
    artifact_closure_sha256: Digest
    tokenizer_sha256: Digest
    runtime_requirements: PolicyIdentity
    tasks: Annotated[tuple[TaskDescriptor, ...], Field(min_length=1, max_length=2)]
    executed_labels: Annotated[tuple[ConditioningLabel, ...], Field(max_length=128)]
    preprocessing: PolicyIdentity
    windowing: PolicyIdentity
    decoding: PolicyIdentity
    output_mapping: PolicyIdentity
    decision: PolicyIdentity
    language: Literal["en"] = "en"
    max_source_codepoints: Annotated[int, Field(ge=1, le=131_072)]
    max_batch_items: Annotated[int, Field(ge=1, le=16)]
    max_windows: Annotated[int, Field(ge=1, le=256)]
    max_candidates: Annotated[int, Field(ge=1, le=8192)]
    max_output_findings: Annotated[int, Field(ge=1, le=576)]
    peak_memory_bytes: Annotated[int, Field(ge=1, le=MAX_MEMORY_BYTES)]
    inference_threads: Annotated[int, Field(ge=1, le=2)]

    @model_validator(mode="after")
    def closed_component(self):
        names = [task.task for task in self.tasks]
        if len(set(names)) != len(names) or len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("duplicate component member")
        labels = [(label.task, label.label_id) for label in self.executed_labels]
        if len(set(labels)) != len(labels):
            raise ValueError("duplicate conditioning label")
        for task in self.tasks:
            if not set(task.enabled_ids) <= {label for name, label in labels if name == task.task}:
                raise ValueError("enabled category absent from executed schema")
        return self


class TaskBinding(Contract):
    task: TaskName
    component_id: Identifier


class ResourceAccounting(Contract):
    """Declared complete-closure costs; qualification measures actual costs."""
    download_bytes: Annotated[int, Field(ge=1, le=MAX_DOWNLOAD_BYTES)]
    installed_bytes: Annotated[int, Field(ge=1, le=MAX_INSTALLED_BYTES)]
    rollback_bytes: Annotated[int, Field(ge=0, le=MAX_INSTALLED_BYTES)]
    temporary_bytes: SafeInteger
    transition_peak_memory_bytes: Annotated[int, Field(ge=1, le=MAX_MEMORY_BYTES)]
    inference_threads: Annotated[int, Field(ge=1, le=2)]

    @model_validator(mode="after")
    def installed_budget(self):
        if self.installed_bytes + self.rollback_bytes > MAX_INSTALLED_BYTES:
            raise ValueError("aggregate installed budget exceeded")
        return self


class AnalysisProfile(Descriptor):
    schema_version: Literal["nlp-profile.v2"] = "nlp-profile.v2"
    profile_id: Identifier
    revision: Identifier
    components: Annotated[tuple[ComponentDescriptor, ...], Field(min_length=1, max_length=2)]
    bindings: Annotated[tuple[TaskBinding, ...], Field(min_length=2, max_length=2)]
    artifacts: Annotated[tuple[Artifact, ...], Field(min_length=1, max_length=256)]
    aggregation: PolicyIdentity
    presentation: PolicyIdentity
    resources: ResourceAccounting
    execution_mode: Literal["serial_unload"] = "serial_unload"

    @model_validator(mode="after")
    def closed_composition(self):
        components = {component.component_id: component for component in self.components}
        artifacts = {artifact.artifact_id: artifact for artifact in self.artifacts}
        if len(components) != len(self.components) or len(artifacts) != len(self.artifacts):
            raise ValueError("duplicate profile member")
        if any(component.engine != self.components[0].engine for component in self.components):
            raise ValueError("profile requires one engine")
        if {binding.task for binding in self.bindings} != set(TASK_CONTRACTS):
            raise ValueError("profile requires both tasks exactly once")
        expected = {(binding.component_id, binding.task) for binding in self.bindings}
        actual = {(component.component_id, task.task) for component in self.components for task in component.tasks}
        if actual != expected:
            raise ValueError("task binding mismatch")
        required = {asset for component in self.components for asset in component.artifact_ids}
        if not required <= set(artifacts):
            raise ValueError("incomplete artifact closure")
        # Engine/runtime files also belong to the complete closure.
        if any(asset.artifact_id not in required and asset.role not in ("engine", "runtime")
               for asset in self.artifacts):
            raise ValueError("unreferenced data artifact")
        if not {"engine", "runtime"} <= {asset.role for asset in self.artifacts}:
            raise ValueError("missing executable closure")
        for component in self.components:
            closure = tuple(asset for asset in self.artifacts
                            if asset.artifact_id in component.artifact_ids or asset.role in ("engine", "runtime"))
            if component.artifact_closure_sha256 != artifact_closure_digest(closure):
                raise ValueError("component artifact closure mismatch")
            data = [artifacts[asset_id] for asset_id in component.artifact_ids]
            if "weights" not in {asset.role for asset in data}:
                raise ValueError("missing component data")
            tokenizers = tuple(asset for asset in data if asset.role == "tokenizer")
            if (component.adapter.tokenizer_format == "none") != (not tokenizers):
                raise ValueError("tokenizer format mismatch")
            if component.tokenizer_sha256 != artifact_closure_digest(tokenizers):
                raise ValueError("tokenizer closure mismatch")
            if any(asset.format_id != component.adapter.artifact_format for asset in data if asset.role == "weights"):
                raise ValueError("unsupported artifact format")
            if any(asset.format_id != component.adapter.tokenizer_format for asset in data if asset.role == "tokenizer"):
                raise ValueError("unsupported tokenizer format")
        # Shared files use one artifact identity rather than optimistic hash deduplication.
        installed = sum(asset.size_bytes for asset in self.artifacts)
        if installed > self.resources.installed_bytes:
            raise ValueError("artifact accounting mismatch")
        if any(component.peak_memory_bytes > self.resources.transition_peak_memory_bytes or
               component.inference_threads > self.resources.inference_threads for component in self.components):
            raise ValueError("component exceeds profile resources")
        return self

    def component(self, component_id: str) -> ComponentDescriptor:
        for component in self.components:
            if component.component_id == component_id:
                return component
        raise ValueError("unknown component")
