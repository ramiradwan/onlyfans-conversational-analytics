"""Closed shown-only preview output, separate from complete worker records."""
from __future__ import annotations

from collections import Counter
from typing import Annotated, ClassVar, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.models.question_evidence import QuestionEvidence
from app.models.knowledge_contributions import UUIDRecord
from app.nlp.contracts import Contract, Coverage, Digest, Identifier, SourceBinding
from app.nlp.normalization import normalize_literal_key
from app.nlp.preview_parameters import RECURRENCE_SELECTION_POLICY
from app.nlp.profiles import PolicyIdentity, TaskName

MAX_PREVIEW_SOURCES = 4096
MAX_PREVIEW_BYTES = 1_048_576
MAX_PREVIEW_CANDIDATES = 65_536
TASKS = ("message_topics", "concept_mentions")
KINDS = ("topic_category", "mention_category")
Count = Annotated[int, Field(ge=0, le=MAX_PREVIEW_CANDIDATES)]
Ordinal = Annotated[int, Field(ge=0, lt=MAX_PREVIEW_SOURCES)]
Assessment = Literal["assessed", "partial", "uncertain", "unsupported", "failed", "unprocessed"]
Reason = Literal["none", "below_threshold", "unsupported_input", "dependency_unavailable",
                 "cancelled", "deadline", "worker_failure", "limit_reached"]


class PreviewExamination(Contract):
    ordinal: Ordinal
    task: TaskName
    assessment: Assessment
    reason: Reason
    coverage: Coverage | None

    @model_validator(mode="after")
    def coherent(self):
        if self.coverage is not None and len(set(self.coverage.limits_reached)) != len(self.coverage.limits_reached):
            raise ValueError("preview_coverage_invalid")
        if self.assessment == "assessed" and (
                self.coverage is None or not self.coverage.complete or self.reason != "none"):
            raise ValueError("preview_assessment_invalid")
        if self.assessment == "partial" and (self.coverage is None or self.coverage.complete):
            raise ValueError("preview_assessment_invalid")
        if self.coverage is None and self.assessment != "unprocessed":
            raise ValueError("preview_coverage_missing")
        return self


class PreviewTaskCoverage(Contract):
    task: TaskName
    eligible: Annotated[int, Field(ge=0, le=MAX_PREVIEW_SOURCES)]
    assessed: Count
    partial: Count
    uncertain: Count
    unsupported: Count
    failed: Count
    unprocessed: Count

    @model_validator(mode="after")
    def partition(self):
        if sum(getattr(self, name) for name in
               ("assessed", "partial", "uncertain", "unsupported", "failed", "unprocessed")) != self.eligible:
            raise ValueError("preview_coverage_invalid")
        return self

    @property
    def resolved(self):
        return self.assessed == self.eligible


class PreviewLabel(Contract):
    presentation: PolicyIdentity
    task_contract: Literal["message_topics.v1", "concept_mentions.v1"]
    taxonomy: PolicyIdentity
    category_id: Identifier
    display_label: Annotated[str, Field(min_length=1, max_length=256)]
    label_status: Literal["resolved", "id_only"]
    label_identity_sha256: Digest

    @model_validator(mode="after")
    def text(self):
        self.display_label.encode("utf-8", errors="strict")
        if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in self.display_label):
            raise ValueError("preview_label_invalid")
        if self.label_status == "id_only" and self.display_label != self.category_id:
            raise ValueError("preview_label_invalid")
        return self


class PreviewEvidence(Contract):
    ordinal: Ordinal
    source: SourceBinding
    reference: QuestionEvidence
    assessment: Literal["complete", "partial"]

    @model_validator(mode="after")
    def binding(self):
        if (self.reference.account_ref != self.source.account_scope
                or self.reference.message_ref != self.source.source_ref
                or self.reference.source_version_digest != "sha256:" + self.source.source_version):
            raise ValueError("preview_evidence_binding")
        return self


def _evidence_support(evidence_rows, *, require_span):
    messages, conversations, senders, complete, partial, occurrences = set(), set(), set(), set(), set(), set()
    for evidence in evidence_rows:
        span = evidence.reference.span
        if (span is not None) != require_span:
            raise ValueError("preview_evidence_span")
        key = (evidence.ordinal, None if span is None else (span.start, span.end))
        if key in occurrences:
            raise ValueError("preview_evidence_duplicate")
        occurrences.add(key)
        messages.add(evidence.ordinal)
        conversations.add(evidence.reference.conversation_ref)
        senders.add(evidence.source.speaker_ref)
        (complete if evidence.assessment == "complete" else partial).add(evidence.ordinal)
    if complete & partial:
        raise ValueError("preview_support_invalid")
    return len(messages), len(conversations), len(senders), len(occurrences), len(complete), len(partial)


class PreviewSummary(Contract):
    kind: Literal["topic_category", "mention_category"]
    label: PreviewLabel
    evidence: Annotated[tuple[PreviewEvidence, ...], Field(min_length=1, max_length=MAX_PREVIEW_CANDIDATES)]
    supporting_conversations: Count
    supporting_messages: Count
    occurrences: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count
    share_of_complete_messages: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None
    ranking_scope: Literal["frozen_scope", "assessed_portion"]
    additional_categories: Annotated[int, Field(ge=0, le=63)] | None

    @property
    def task(self):
        return TASKS[KINDS.index(self.kind)]

    @model_validator(mode="after")
    def support(self):
        if self.label.task_contract != self.task + ".v1":
            raise ValueError("preview_summary_task")
        messages, conversations, _, occurrences, complete, partial = _evidence_support(
            self.evidence, require_span=self.kind == "mention_category")
        if (self.supporting_messages, self.supporting_conversations, self.occurrences,
                self.complete_supporting_messages, self.partial_supporting_messages) != (
                    messages, conversations, occurrences, complete, partial):
            raise ValueError("preview_support_invalid")
        return self


class RecurringMentionSummary(Contract):
    kind: Literal['recurring_mention'] = 'recurring_mention'
    literal_key: Annotated[str, Field(min_length=1, max_length=256)]
    supporting_senders: Annotated[int, Field(ge=2, le=100)]
    supporting_messages: Count
    occurrences: Count
    complete_supporting_messages: Count
    partial_supporting_messages: Count
    ranking_scope: Literal['frozen_scope', 'assessed_portion']
    additional_phrases: Count | None
    evidence: Annotated[tuple[PreviewEvidence, ...], Field(min_length=2, max_length=MAX_PREVIEW_CANDIDATES)]

    @property
    def task(self):
        return 'concept_mentions'

    @model_validator(mode='after')
    def support(self):
        self.literal_key.encode('utf-8', errors='strict')
        if self.literal_key != normalize_literal_key(self.literal_key):
            raise ValueError('preview_literal_invalid')
        messages, _, senders, occurrences, complete, partial = _evidence_support(self.evidence, require_span=True)
        if (self.supporting_messages, self.supporting_senders, self.occurrences,
                self.complete_supporting_messages, self.partial_supporting_messages) != (
                    messages, senders, occurrences, complete, partial):
            raise ValueError('preview_support_invalid')
        return self


class PreviewIdentity(UUIDRecord):
    job_id: str
    contribution_id: str
    spec_sha256: Digest
    input_inventory_sha256: Digest
    profile_sha256: Digest
    selection_policy: PolicyIdentity


class _PreviewResult(PreviewIdentity):
    task_order: ClassVar[tuple[str, ...]] = TASKS
    kind_order: ClassVar[tuple[str, ...]] = KINDS
    schema_version: str
    source_count: Annotated[int, Field(ge=0, le=MAX_PREVIEW_SOURCES)]
    examination: Annotated[tuple[PreviewExamination, ...], Field(max_length=2 * MAX_PREVIEW_SOURCES)]
    tasks: Annotated[tuple[PreviewTaskCoverage, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def completeness(self):
        tasks = [entry.task for entry in self.tasks]
        if tasks != [task for task in self.task_order if task in tasks]:
            raise ValueError("preview_task_set")
        coordinates = [(entry.ordinal, entry.task) for entry in self.examination]
        if coordinates != [(ordinal, task) for ordinal in range(self.source_count) for task in tasks]:
            raise ValueError("preview_examination_set")
        for coverage in self.tasks:
            counts = Counter(entry.assessment for entry in self.examination if entry.task == coverage.task)
            if coverage.eligible != self.source_count or any(
                    getattr(coverage, name) != counts[name] for name in
                    ("assessed", "partial", "uncertain", "unsupported", "failed", "unprocessed")):
                raise ValueError("preview_coverage_invalid")
        kinds = [summary.kind for summary in self.summaries]
        if kinds != [kind for kind in self.kind_order if kind in kinds]:
            raise ValueError("preview_summary_set")
        exams = {(entry.ordinal, entry.task): entry for entry in self.examination}
        for summary in self.summaries:
            coverage = next((entry for entry in self.tasks if entry.task == summary.task), None)
            if coverage is None or summary.ranking_scope != (
                    "frozen_scope" if coverage.resolved else "assessed_portion"):
                raise ValueError("preview_ranking_scope")
            if isinstance(summary, PreviewSummary):
                if (summary.additional_categories is not None) != coverage.resolved:
                    raise ValueError("preview_additional_categories")
                share = summary.complete_supporting_messages / coverage.assessed if coverage.assessed else None
                if summary.share_of_complete_messages != share:
                    raise ValueError("preview_denominator")
            elif (summary.additional_phrases is not None) != coverage.resolved:
                raise ValueError("preview_additional_phrases")
            for evidence in summary.evidence:
                exam = exams.get((evidence.ordinal, summary.task))
                expected = "complete" if exam is not None and exam.assessment == "assessed" else "partial"
                if exam is None or exam.assessment not in ("assessed", "partial") or evidence.assessment != expected:
                    raise ValueError("preview_evidence_assessment")
                span = evidence.reference.span
                if span is not None and not any(
                        interval.start <= span.start < span.end <= interval.end for interval in exam.coverage.examined):
                    raise ValueError("preview_evidence_coverage")
        return self


class PreviewContribution(_PreviewResult):
    schema_version: Literal["text-analysis-preview.v1"] = "text-analysis-preview.v1"
    summaries: Annotated[tuple[PreviewSummary, ...], Field(min_length=1, max_length=2)]


class RecurringMentionPreview(_PreviewResult):
    task_order = ('concept_mentions',)
    kind_order = ('recurring_mention',)
    schema_version: Literal['text-analysis-preview.v2'] = 'text-analysis-preview.v2'
    source_count: Annotated[int, Field(ge=2, le=100)]
    summaries: tuple[RecurringMentionSummary]

    @model_validator(mode='after')
    def mention_policy(self):
        if self.selection_policy != RECURRENCE_SELECTION_POLICY:
            raise ValueError('preview_selection_mismatch')
        return self


ShownPreview = Annotated[PreviewContribution | RecurringMentionPreview, Field(discriminator='schema_version')]
SHOWN_PREVIEW_ADAPTER = TypeAdapter(ShownPreview)


class NoPreviewOutput(PreviewIdentity):
    schema_version: Literal["text-analysis-preview-no-output.v1"] = "text-analysis-preview-no-output.v1"
    disposition: Literal["empty", "incomplete", "overflow"]
