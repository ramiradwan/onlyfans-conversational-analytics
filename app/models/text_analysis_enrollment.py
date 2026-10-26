"""Immutable enrollment lineage and versioned account commands."""
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.models.knowledge_contributions import Name
from app.models.text_analysis_jobs import EnrollmentIntent, ExecutionIntent
from app.nlp.contracts import Contract, Digest, Identifier
from app.nlp.profiles import canonical_bytes, TaskDescriptor, PolicyIdentity

Version = Annotated[int, Field(ge=0, le=9_007_199_254_740_991)]


class EnrollmentIdentity(Contract):
    principal_id: Name
    creator_account_id: Name
    role: Literal['creator', 'operator']
    organization_id: Name
    installation_id: Name
    installation_key_id: Name
    installation_key_jkt: Name
    grant_reference_ids: Annotated[tuple[Name, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode='after')
    def bounded(self):
        if len(set(self.grant_reference_ids)) != len(self.grant_reference_ids):
            raise ValueError('text_analysis_identity_duplicate')
        if len(canonical_bytes(self)) > 65536:
            raise ValueError('text_analysis_identity_limit')
        return self


class EnrollmentCommand(Contract):
    schema_version: Literal['text-analysis-enrollment-command.v1'] = 'text-analysis-enrollment-command.v1'
    command_key: str
    expected_enrollment_id: str | None
    expected_mode_version: Version
    action: Literal['enable', 'replace', 'pause', 'resume', 'retire']
    expected_profile_sha256: Digest | None = None

    _uuid = field_validator('command_key')(ExecutionIntent.uuid4.__func__)

    @field_validator('expected_enrollment_id')
    @classmethod
    def enrollment_uuid(cls, value):
        return None if value is None else ExecutionIntent.uuid4(value)

    @model_validator(mode='after')
    def closed_action(self):
        if (self.action in ('enable', 'replace')) != (self.expected_profile_sha256 is not None):
            raise ValueError('text_analysis_command_profile_invalid')
        if (self.action == 'enable') != (self.expected_enrollment_id is None):
            raise ValueError('text_analysis_command_target_invalid')
        return self


class CurrentEnrollment(Contract):
    installation_id: Name
    creator_account_id: Name
    principal_id: Name
    mode_version: Version
    enrollment_id: str | None
    state: Literal['enabled', 'paused', 'retired']
    capsule_available: bool


class EnrollmentCommandReceipt(Contract):
    command_key: str
    meaning_sha256: Digest
    mode_version: Version
    enrollment_id: str | None
    state: Literal['enabled', 'paused', 'retired']


class EnrollmentCommandResult(Contract):
    receipt: EnrollmentCommandReceipt
    current: CurrentEnrollment


class EnrollmentCapsule(Contract):
    enrollment_id: str
    intent: EnrollmentIntent
    identity: EnrollmentIdentity
    profile_bytes: Annotated[bytes, Field(min_length=1, max_length=1048576)]
    profile_bytes_sha256: Digest
    capsule_sha256: Digest


class EnrollmentLocator(Contract):
    installation_id: Name
    creator_account_id: Name
    principal_id: Name
    enrollment_id: str
    mode_version: Version

    _uuid = field_validator('enrollment_id')(ExecutionIntent.uuid4.__func__)


class EnrollmentProfileDisclosure(Contract):
    profile_sha256: Digest
    profile_id: Identifier
    revision: Identifier
    languages: Annotated[tuple[Literal['en'], ...], Field(min_length=1, max_length=1)]
    tasks: Annotated[tuple[TaskDescriptor, ...], Field(min_length=1, max_length=2)]
    presentation: PolicyIdentity

    @model_validator(mode='after')
    def declared_tasks(self):
        order = ('message_topics', 'concept_mentions')
        names = tuple(task.task for task in self.tasks)
        if names != tuple(task for task in order if task in names):
            raise ValueError('text_analysis_disclosure_tasks_invalid')
        return self


class EnrollmentView(Contract):
    current: CurrentEnrollment
    profile: EnrollmentProfileDisclosure | None

    @model_validator(mode='after')
    def available(self):
        if self.current.capsule_available != (self.profile is not None):
            raise ValueError('text_analysis_view_invalid')
        return self
