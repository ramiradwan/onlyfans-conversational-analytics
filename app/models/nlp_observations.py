"""Typed source-incidence views over saved findings."""
import hashlib
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from app.analytics.query_contracts import Count, Instant, QuestionEvidence, QuestionRecord
from app.models.analytics import AccountRef, AnalyticsWindow, ConversationRef, Sha256Digest
from app.models.insights import FindingsCategoryFilter, FindingsTask, FindingsTaskAvailability
from app.models.nlp_literals import LiteralCoverage
from app.nlp.contracts import Identifier, SourceBinding
from app.nlp.normalization import ExtractionProvenance, literal_type_id
from app.nlp.profiles import PolicyIdentity, canonical_bytes


PROJECTION_POLICY = PolicyIdentity(policy_id='nlp-observation-links.v1', revision='1',
    sha256=hashlib.sha256(b'nlp-observation-links.v1\0source-incidence;complete-or-partial;'
                          b'topic-per-message;literal-per-span;original-population;mapped-support').hexdigest())


class CategoryObservationFocus(QuestionRecord):
    kind: Literal['category'] = 'category'
    selection: FindingsCategoryFilter


class LiteralObservationFocus(QuestionRecord):
    kind: Literal['literal'] = 'literal'
    node_id: Sha256Digest


ObservationFocus = Annotated[CategoryObservationFocus | LiteralObservationFocus, Field(discriminator='kind')]


class ObservationLinksRequest(QuestionRecord):
    expected_generation: Annotated[int, Field(strict=True, ge=1)]
    expected_projection_digest: Sha256Digest
    start_date: Instant | None = None
    end_date: Instant | None = None
    direction: Literal['all', 'inbound', 'outbound'] = 'all'
    conversation_ref: ConversationRef | None = None
    evidence_offset: Annotated[int, Field(strict=True, ge=0, le=1_000_000)] = 0
    evidence_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    expected_normalization_sha256: Sha256Digest | None = None
    expected_view_sha256: Sha256Digest | None = None
    focus: ObservationFocus

    @model_validator(mode='after')
    def pinned_selection(self):
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError('observation window invalid')
        topic = self.focus.kind == 'category' and self.focus.selection.task == 'message_topics'
        if topic and self.expected_normalization_sha256 is not None:
            raise ValueError('topic observation has no normalization')
        if self.evidence_offset and self.expected_view_sha256 is None:
            raise ValueError('observation page requires view identity')
        if (self.focus.kind == 'literal' or (self.evidence_offset and not topic)) and self.expected_normalization_sha256 is None:
            raise ValueError('observation selection requires normalization identity')
        return self


class CategoryRef(QuestionRecord):
    task: Literal['message_topics'] = 'message_topics'
    task_contract: Literal['message_topics.v1'] = 'message_topics.v1'
    taxonomy: PolicyIdentity
    category_id: Identifier
    display_label: Annotated[str, Field(min_length=1, max_length=256)]
    label_status: Literal['resolved', 'id_only']
    label_identity_sha256: Sha256Digest


class LiteralRef(CategoryRef):
    task: Literal['concept_mentions'] = 'concept_mentions'
    task_contract: Literal['concept_mentions.v1'] = 'concept_mentions.v1'
    node_id: Sha256Digest
    type_id: Identifier
    literal_key: Annotated[str, Field(min_length=1, max_length=256)]
    resolution: Literal['provisional'] = 'provisional'

    @model_validator(mode='after')
    def exact_type_and_key(self):
        if (self.type_id != literal_type_id(self.task_contract, self.taxonomy, self.category_id)
                or self.literal_key != unicodedata.normalize('NFC', self.literal_key.strip())):
            raise ValueError('observation literal identity invalid')
        return self


class TopicProvenance(ExtractionProvenance):
    task_contract: Literal['message_topics.v1'] = 'message_topics.v1'


class MessageTopicLink(QuestionRecord):
    kind: Literal['message_topic'] = 'message_topic'
    source: SourceBinding
    target: CategoryRef
    assessment: Literal['complete', 'partial']
    provenance: TopicProvenance
    reference: QuestionEvidence

    @model_validator(mode='after')
    def no_trigger_span(self):
        if self.reference.span is not None:
            raise ValueError('topic source has no trigger span')
        return self


class LiteralOccurrenceLink(QuestionRecord):
    kind: Literal['literal_occurrence'] = 'literal_occurrence'
    source: SourceBinding
    target: LiteralRef
    assessment: Literal['complete', 'partial']
    provenance: ExtractionProvenance
    reference: QuestionEvidence

    @model_validator(mode='after')
    def exact_span(self):
        if self.reference.span is None:
            raise ValueError('literal source requires an exact span')
        return self


ObservationLink = Annotated[MessageTopicLink | LiteralOccurrenceLink, Field(discriminator='kind')]


class ObservationSupport(QuestionRecord):
    supporting_conversations: Count
    supporting_messages: Count
    occurrences: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count

    @model_validator(mode='after')
    def partitions(self):
        if (not self.supporting_conversations <= self.supporting_messages <= self.occurrences
                or self.complete_supporting_messages + self.partial_supporting_messages != self.supporting_messages
                or bool(self.supporting_conversations) != bool(self.supporting_messages)
                or bool(self.occurrences) != bool(self.supporting_messages)):
            raise ValueError('observation support invalid')
        return self


class ObservationLinkPage(QuestionRecord):
    offset: Count
    limit: Annotated[int, Field(strict=True, ge=1, le=100)]
    total: Count
    has_more: StrictBool
    items: Annotated[tuple[ObservationLink, ...], Field(max_length=100)]

    @model_validator(mode='after')
    def exact_page(self):
        if (self.offset > self.total or len(self.items) != min(self.limit, self.total - self.offset)
                or self.has_more != (self.offset + len(self.items) < self.total)):
            raise ValueError('observation page invalid')
        return self


class ObservationContext(QuestionRecord):
    schema_version: Literal['analytics-observation-links.v1'] = 'analytics-observation-links.v1'
    account_ref: AccountRef
    source_revision: Count
    projection_generation: Annotated[int, Field(strict=True, ge=1)]
    projection_digest: Sha256Digest
    canonical_content_digest: Sha256Digest
    graph_digest: Sha256Digest
    pipeline_revision: str
    pipeline_config_digest: Sha256Digest
    pipeline_identity_digest: Sha256Digest
    snapshot_sha256: Sha256Digest
    profile_sha256: Sha256Digest
    presentation: PolicyIdentity
    requested_window: AnalyticsWindow
    effective_window: AnalyticsWindow
    direction: Literal['all', 'inbound', 'outbound']
    conversation_ref: ConversationRef | None
    focus: ObservationFocus
    projection_policy: PolicyIdentity = PROJECTION_POLICY
    availability: Literal['available', 'unavailable']
    unavailable_reason: Literal['task_not_declared', 'not_prepared', 'capacity'] | None
    normalization_status: Literal['not_required', 'available', 'unavailable']
    normalization_sha256: Sha256Digest | None
    normalization_policy: PolicyIdentity | None


def view_identity(value):
    """Page size and offset do not change the selected observation view."""
    body = value.model_dump(mode='json') if isinstance(value, QuestionRecord) else value
    context = ObservationContext.model_validate({key: body[key] for key in ObservationContext.model_fields if key in body})
    return 'sha256:' + hashlib.sha256(b'analytics-observation-links.v1\0' + canonical_bytes(context)).hexdigest()


class ObservationLinksResponse(ObservationContext):
    view_sha256: Sha256Digest
    processing_mode: Literal['enabled', 'paused']
    task_availability: Annotated[tuple[FindingsTaskAvailability, ...], Field(min_length=2, max_length=2)]
    tasks: Annotated[tuple[FindingsTask, ...], Field(min_length=1, max_length=2)]
    normalization_coverage: LiteralCoverage | None
    focus_coverage: LiteralCoverage | None
    support: ObservationSupport
    link_page: ObservationLinkPage

    @model_validator(mode='after')
    def coherent_links(self):
        tasks = {task.task: task for task in self.tasks}
        declared = {item.task for item in self.task_availability if item.availability == 'declared'}
        if (len(tasks) != len(self.tasks) or len({item.task for item in self.task_availability}) != 2
                or {item.task for item in self.task_availability} != {'message_topics', 'concept_mentions'}
                or set(tasks) != declared or self.projection_policy != PROJECTION_POLICY
                or self.view_sha256 != view_identity(self)):
            raise ValueError('observation identity invalid')
        topic = self.focus.kind == 'category' and self.focus.selection.task == 'message_topics'
        selected_task = 'message_topics' if topic else 'concept_mentions'
        if ((self.unavailable_reason == 'task_not_declared') != (selected_task not in tasks)
                or self.support.occurrences != self.link_page.total):
            raise ValueError('observation task accounting invalid')
        if self.focus.kind == 'category' and selected_task in tasks and not any(
                row.category_id == self.focus.selection.category_id for row in tasks[selected_task].rows):
            raise ValueError('observation category focus invalid')
        if selected_task in tasks:
            task = tasks[selected_task]
            if (self.support.supporting_messages > task.eligible_messages
                    or self.support.complete_supporting_messages > task.complete_messages
                    or self.support.partial_supporting_messages > task.partial_messages):
                raise ValueError('observation support exceeds extraction')
        if self.availability == 'available':
            if self.unavailable_reason is not None:
                raise ValueError('available observation has refusal')
        elif (self.unavailable_reason is None or self.link_page.total or self.support.occurrences):
            raise ValueError('unavailable observation has results')
        if topic:
            if (self.normalization_status != 'not_required' or self.normalization_sha256 is not None
                    or self.normalization_policy is not None or self.normalization_coverage is not None
                    or self.focus_coverage is not None):
                raise ValueError('topic observation has normalization')
        elif self.availability == 'available':
            if (self.normalization_status != 'available' or self.normalization_sha256 is None
                    or self.normalization_policy is None or self.normalization_coverage is None
                    or self.focus_coverage is None):
                raise ValueError('observation normalization missing')
        elif (self.normalization_status != 'unavailable' or self.normalization_sha256 is not None
                or self.normalization_policy is not None or self.normalization_coverage is not None
                or self.focus_coverage is not None):
            raise ValueError('unavailable observation has normalization')
        if self.focus_coverage is not None:
            if (any(getattr(self.focus_coverage, key) > getattr(self.normalization_coverage, key)
                    for key in LiteralCoverage.model_fields)
                    or self.support.occurrences > self.focus_coverage.mapped_occurrences
                    or (self.focus.kind == 'category' and self.support.occurrences != self.focus_coverage.mapped_occurrences)):
                raise ValueError('observation focus coverage invalid')
        seen = set()
        for link in self.link_page.items:
            source, reference, target, provenance = link.source, link.reference, link.target, link.provenance
            task = tasks.get(target.task)
            if ((link.assessment == 'complete' and self.support.complete_supporting_messages == 0)
                    or (link.assessment == 'partial' and self.support.partial_supporting_messages == 0)):
                raise ValueError('observation assessment accounting invalid')
            if (task is None or source.account_scope != self.account_ref or reference.account_ref != self.account_ref
                    or source.source_ref != reference.message_ref
                    or 'sha256:' + source.source_version != reference.source_version_digest
                    or reference.source_revision != self.source_revision
                    or self.conversation_ref not in (None, reference.conversation_ref)
                    or 'sha256:' + provenance.extraction_snapshot_sha256 != self.snapshot_sha256
                    or 'sha256:' + provenance.profile_sha256 != self.profile_sha256
                    or provenance.component_id != task.component_id
                    or 'sha256:' + provenance.component_sha256 != task.component_sha256
                    or provenance.taxonomy != task.taxonomy or target.taxonomy != task.taxonomy
                    or target.task_contract != task.task_contract or provenance.task_contract != task.task_contract):
                raise ValueError('observation source provenance invalid')
            for window in (self.requested_window, self.effective_window):
                if ((window.start is not None and reference.sent_at < window.start)
                        or (window.end is not None and reference.sent_at > window.end)):
                    raise ValueError('observation source window invalid')
            label = next((row for row in task.rows if row.category_id == target.category_id), None)
            if label is None or any(getattr(label, key) != getattr(target, key) for key in
                    ('display_label', 'label_status', 'label_identity_sha256')):
                raise ValueError('observation category invalid')
            if self.focus.kind == 'category':
                if (target.task, target.category_id) != (self.focus.selection.task, self.focus.selection.category_id):
                    raise ValueError('observation category focus mismatch')
            elif link.kind != 'literal_occurrence' or target.node_id != self.focus.node_id:
                raise ValueError('observation literal focus mismatch')
            span = reference.span
            key = (reference.message_ref, target.task, target.category_id, None if span is None else (span.start, span.end))
            if key in seen:
                raise ValueError('duplicate observation link')
            seen.add(key)
        return self
