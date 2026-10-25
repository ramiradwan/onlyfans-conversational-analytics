"""Immutable local execution plans and content-free durable receipts."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.models.knowledge_contributions import InputIdentity, UUIDRecord
from app.nlp.contracts import Contract, Digest
from app.nlp.profiles import PolicyIdentity


class ExecutionParameters(Contract):
    parameter_sha256: Digest
    algorithm_profile_sha256: Digest
    policy_identity_sha256: Digest
    algorithm_artifacts_sha256: Digest
    model_or_rules_identity_sha256: Digest


class JobPlan(ExecutionParameters):
    input: InputIdentity


class ExecutionIntent(Contract):
    external_key: str
    authority_kind: Literal['license', 'preview']
    run_context: Annotated[bytes, Field(min_length=1, max_length=16384)]
    scope_sha256: Digest
    preview_selection_policy: PolicyIdentity | None = None

    @model_validator(mode='after')
    def selected_policy(self):
        if (self.authority_kind == 'preview') != (self.preview_selection_policy is not None):
            raise ValueError('text_analysis_preview_policy_required')
        return self

    @field_validator('external_key')
    @classmethod
    def uuid4(cls, value):
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError('text_analysis_external_key_invalid')
        return value


class EnrollmentIntent(Contract):
    external_key: str
    run_context: Annotated[bytes, Field(min_length=1, max_length=16384)]
    parameters: ExecutionParameters

    _uuid4 = field_validator('external_key')(ExecutionIntent.uuid4.__func__)


class ExecutionReceipt(UUIDRecord):
    job_id: str
    contribution_id: str
    state: Literal['prepared', 'pending', 'running', 'committed', 'failed', 'result_deleted']
    confirmation: bytes | None
    enrollment_id: str | None = None
    terminal_reason: Literal['empty', 'incomplete', 'overflow', 'source_retired'] | None = None


class EnrollmentReceipt(Contract):
    enrollment_id: str
    state: Literal['enabled', 'paused', 'retired']


class PreviewConfirmation(UUIDRecord):
    schema_version: Literal['text-analysis-confirmation.v1'] = 'text-analysis-confirmation.v1'
    external_key: str
    job_id: str
    contribution_id: str
    spec_sha256: Digest
    context_sha256: Digest
    scope_sha256: Digest
    parameter_sha256: Digest
    input_inventory_sha256: Digest
    reference_id: Annotated[str, Field(min_length=1, max_length=512)]
    compact_jws_sha256: Digest
    preview_selection_policy: PolicyIdentity
