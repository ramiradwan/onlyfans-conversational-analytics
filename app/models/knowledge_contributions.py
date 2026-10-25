"""Closed records for durable, source-bound enrichment contributions."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.nlp.contracts import Contract, Digest, Identifier, SourceBinding
from app.nlp.profiles import canonical_bytes

Name = Annotated[str, Field(min_length=1, max_length=512)]
Count = Annotated[int, Field(ge=0, le=100_000)]
Fence = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]


class UUIDRecord(Contract):
    @field_validator('request_id', 'job_id', 'contribution_id', 'job_attempt_id',
                     'producing_job_attempt_id', check_fields=False)
    @classmethod
    def uuid4(cls, value: str) -> str:
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError('contribution_uuid_invalid')
        return value


class CapabilityBinding(Contract):
    capability_id: Identifier
    artifact_family: Identifier
    artifact_major: Annotated[int, Field(ge=1)]
    object_sha256: Digest
    object_reference_id: Name
    profile_id: Identifier
    profile_sha256: Digest


class InputIdentity(Contract):
    canonical_revision: Annotated[int, Field(ge=0)]
    canonical_content_sha256: Digest
    inventory_sha256: Digest
    source_count: Count


class JobSpec(UUIDRecord):
    schema_version: Literal['knowledge-job.v1'] = 'knowledge-job.v1'
    request_id: str
    job_id: str
    contribution_id: str
    installation_id: Name
    organization_id: Name
    creator_account_id: Name
    capability: CapabilityBinding
    parameter_sha256: Digest
    input: InputIdentity
    algorithm_profile_sha256: Digest
    policy_identity_sha256: Digest
    algorithm_artifacts_sha256: Digest
    model_or_rules_identity_sha256: Digest

    @model_validator(mode='after')
    def durable_intent(self):
        if self.request_id != self.job_id:
            raise ValueError('contribution_intent_mismatch')
        return self


class SourceLocation(Contract):
    conversation_id: Name
    message_id: Name


class SourceMemberSpec(Contract):
    ordinal: Count
    location: SourceLocation
    source: SourceBinding
    source_revision: Annotated[int, Field(ge=0)]
    text_sha256: Digest


class AttemptToken(UUIDRecord):
    job_id: str
    attempt_fence: Fence
    job_attempt_id: str
    lease_expires_at: Annotated[float, Field(allow_inf_nan=False)]


class MemberIdentity(UUIDRecord):
    ordinal: Count
    source_member_sha256: Digest
    record_sha256: Digest
    producing_attempt_fence: Fence
    producing_job_attempt_id: str


class ContributionManifest(UUIDRecord):
    schema_version: Literal['knowledge-contribution.v1'] = 'knowledge-contribution.v1'
    authority: Literal['capability_contribution'] = 'capability_contribution'
    contribution_id: str
    job_id: str
    attempt_fence: Fence
    job_attempt_id: str
    scope_sha256: Digest
    member_count: Count
    ordered_members_sha256: Digest
    profile_sha256: Digest
    authorization_epoch: Annotated[int, Field(ge=0)]
    source_valid_until: Annotated[float, Field(allow_inf_nan=False)] | None
    supersedes: tuple[()] = ()


def record_digest(record: Contract) -> str:
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def inventory_digest(members) -> str:
    return _ordered_digest(b'knowledge-source-inventory.v1\0',
                           (json.loads(canonical_bytes(member)) for member in members))


def members_digest(members) -> str:
    return _ordered_digest(b'knowledge-contribution-members.v1\0',
        ([m.ordinal, m.source_member_sha256, m.record_sha256,
          m.producing_attempt_fence, m.producing_job_attempt_id] for m in members))


def _ordered_digest(domain, values):
    result = hashlib.sha256(domain)
    for value in values:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('ascii')
        result.update(len(raw).to_bytes(8, 'big'))
        result.update(raw)
    return result.hexdigest()
