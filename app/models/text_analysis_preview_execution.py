"""Closed preview requests, retained scope and internal execution bindings."""
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from app.analytics.query_contracts import Instant
from app.models.knowledge_contributions import JobSpec, Name, SourceMemberSpec, UUIDRecord
from app.models.text_analysis_enrollment import EnrollmentIdentity
from app.models.text_analysis_preview import PreviewContribution
from app.models.text_analysis_jobs import ExecutionIntent, ExecutionParameters, ExecutionReceipt, PreviewConfirmation
from app.nlp.contracts import Contract, Digest
from app.nlp.profiles import AnalysisProfile, SubsetAnalysisProfile, PolicyIdentity, TaskDescriptor
from app.security.runtime_policy import RuntimePolicy
from app.security.text_analysis_policy import TextAnalysisRunContext

MAX_PREVIEW_CAPSULE_BYTES = 131072
MAX_PREVIEW_REQUEST_BYTES = 1024
PREVIEW_MESSAGE_CAP = 32


class PreviewPrepareRequest(Contract):
    schema_version: Literal['text-analysis-preview-prepare.v1'] = 'text-analysis-preview-prepare.v1'
    external_key: str
    expected_profile_sha256: Digest

    _uuid = field_validator('external_key')(ExecutionIntent.uuid4.__func__)


class PreviewScope(Contract):
    schema_version: Literal['text-analysis-preview-scope.v1'] = 'text-analysis-preview-scope.v1'
    cutoff: Instant
    lower_exclusive: Instant
    directions: Literal['both'] = 'both'
    message_cap: Literal[32] = 32
    source_count: Annotated[int, Field(ge=0, le=32)]
    earliest: Instant | None
    latest: Instant | None
    inventory_sha256: Digest

    @model_validator(mode='after')
    def coherent(self):
        from datetime import timedelta
        if self.lower_exclusive != self.cutoff - timedelta(days=90):
            raise ValueError('preview_scope_invalid')
        if self.source_count == 0:
            if self.earliest is not None or self.latest is not None:
                raise ValueError('preview_scope_invalid')
        elif (self.earliest is None or self.latest is None
              or not self.lower_exclusive < self.earliest <= self.latest <= self.cutoff):
            raise ValueError('preview_scope_invalid')
        return self


class PreviewExecutionCapsule(UUIDRecord):
    schema_version: Literal['text-analysis-preview-capsule.v1'] = 'text-analysis-preview-capsule.v1'
    job_id: str
    contribution_id: str
    request: PreviewPrepareRequest
    identity: EnrollmentIdentity
    scope: PreviewScope
    parameters: ExecutionParameters
    selection_policy: PolicyIdentity
    spec_sha256: Digest
    profile_bytes_sha256: Digest
    context_sha256: Digest
    confirmation_sha256: Digest


class PreviewLocator(UUIDRecord):
    installation_id: Name
    principal_id: Name
    creator_account_id: Name
    job_id: str
    contribution_id: str
    capsule_sha256: Digest


class PreviewDisclosure(Contract):
    profile_sha256: Digest
    languages: Annotated[tuple[Literal['en'], ...], Field(min_length=1, max_length=1)]
    tasks: Annotated[tuple[TaskDescriptor, ...], Field(min_length=1, max_length=2)]
    presentation: PolicyIdentity
    selection_policy: PolicyIdentity
    output_profile: Literal['urn:bridge-clean:text-analysis-preview-output-policy:v1'] = 'urn:bridge-clean:text-analysis-preview-output-policy:v1'
    output_version: Literal[1] = 1

    @model_validator(mode='after')
    def declared_tasks(self):
        order = ('message_topics', 'concept_mentions')
        names = tuple(task.task for task in self.tasks)
        if names != tuple(task for task in order if task in names):
            raise ValueError('preview_disclosure_tasks_invalid')
        return self


class PreviewReceipt(UUIDRecord):
    job_id: str
    contribution_id: str
    state: Literal['prepared', 'pending', 'running', 'committed', 'failed', 'result_deleted']
    terminal_reason: Literal['empty', 'incomplete', 'overflow', 'source_retired'] | None


class PreviewOutcome(Contract):
    request: PreviewPrepareRequest
    receipt: PreviewReceipt
    scope: PreviewScope | None
    confirmation: PreviewConfirmation | None
    detailed_scope_available: bool
    disclosure: PreviewDisclosure | None

    @model_validator(mode='after')
    def coherent(self):
        if self.detailed_scope_available != (
            self.scope is not None and self.disclosure is not None
        ):
            raise ValueError('preview_outcome_invalid')
        if self.receipt.state == 'result_deleted' and (
            self.scope is not None
            or self.confirmation is not None
            or self.disclosure is not None
        ):
            raise ValueError('preview_outcome_invalid')
        if (
            self.disclosure is not None
            and self.disclosure.profile_sha256 != self.request.expected_profile_sha256
        ):
            raise ValueError('preview_outcome_profile_invalid')
        if self.confirmation is not None:
            import hashlib
            from app.nlp.profiles import canonical_bytes

            confirmation = self.confirmation
            if (
                not self.detailed_scope_available
                or self.scope is None
                or self.disclosure is None
                or confirmation.external_key != self.request.external_key
                or confirmation.job_id != self.receipt.job_id
                or confirmation.contribution_id != self.receipt.contribution_id
                or confirmation.input_inventory_sha256 != self.scope.inventory_sha256
                or confirmation.scope_sha256
                != hashlib.sha256(canonical_bytes(self.scope)).hexdigest()
                or confirmation.preview_selection_policy
                != self.disclosure.selection_policy
            ):
                raise ValueError('preview_outcome_confirmation_invalid')
        return self


@dataclass(frozen=True, slots=True, weakref_slot=True)
class PreviewFindingsBinding:
    locator: PreviewLocator
    receipt: ExecutionReceipt
    capsule: PreviewExecutionCapsule
    spec: JobSpec
    profile: AnalysisProfile | SubsetAnalysisProfile
    sources: tuple[SourceMemberSpec, ...]
    context: TextAnalysisRunContext
    policy: RuntimePolicy


class SavedPreviewDisclosure(PreviewDisclosure):
    languages: Annotated[tuple[Literal['en'], ...], Field(min_length=1, max_length=1)]


class PreviewSavedEnvelope(Contract):
    schema_version: Literal['text-analysis-preview-saved.v1'] = (
        'text-analysis-preview-saved.v1'
    )
    scope: PreviewScope
    disclosure: SavedPreviewDisclosure
    output: PreviewContribution

    @model_validator(mode='after')
    def original(self):
        if (
            self.scope.inventory_sha256 != self.output.input_inventory_sha256
            or self.scope.source_count != self.output.source_count
            or self.disclosure.profile_sha256 != self.output.profile_sha256
            or self.disclosure.selection_policy != self.output.selection_policy
        ):
            raise ValueError('preview_saved_binding_invalid')
        if tuple(task.task for task in self.disclosure.tasks) != tuple(
            task.task for task in self.output.tasks
        ):
            raise ValueError('preview_saved_tasks_invalid')
        descriptors = {task.task: task for task in self.disclosure.tasks}
        for summary in self.output.summaries:
            descriptor = descriptors[summary.task]
            if (
                summary.label.presentation != self.disclosure.presentation
                or summary.label.taxonomy != descriptor.taxonomy
                or summary.label.category_id not in descriptor.enabled_ids
                or summary.label.task_contract != descriptor.task_contract
            ):
                raise ValueError('preview_saved_label_invalid')
        return self


class PreviewSavedId(UUIDRecord):
    contribution_id: str


class PreviewSavedPage(Contract):
    items: Annotated[tuple[PreviewSavedId, ...], Field(max_length=64)]
    next_after: str | None

    @field_validator('next_after')
    @classmethod
    def cursor(cls, value):
        return None if value is None else ExecutionIntent.uuid4(value)

    @model_validator(mode='after')
    def ordered(self):
        keys = [item.contribution_id for item in self.items]
        if keys != sorted(set(keys)) or (
            self.next_after is not None and (not keys or self.next_after != keys[-1])
        ):
            raise ValueError('preview_saved_page_invalid')
        return self
