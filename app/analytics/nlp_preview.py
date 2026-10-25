"""Bounded preview selection and pure validation, without durable side effects."""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from app.analytics.evidence_contracts import EvidenceMessage
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.analytics.opaque_refs import account_ref, conversation_ref, participant_ref
from app.analytics.query_contracts import SourceSpan
from app.models.knowledge_contributions import JobSpec, SourceMemberSpec, inventory_digest, record_digest
from app.models.text_analysis_preview import (
    KINDS, TASKS, MAX_PREVIEW_BYTES, MAX_PREVIEW_CANDIDATES, MAX_PREVIEW_SOURCES,
    NoPreviewOutput, PreviewContribution, PreviewEvidence, PreviewExamination,
    PreviewLabel, PreviewSummary, PreviewTaskCoverage,
)
from app.nlp.contracts import SourceBinding
from app.nlp.profiles import AnalysisProfile, SubsetAnalysisProfile, PolicyIdentity, canonical_bytes, read_profile


SELECTION_POLICY = PolicyIdentity(policy_id="text-analysis-preview-selection.v1", revision="1",
    sha256=hashlib.sha256(b"text-analysis-preview-selection.v1\0category-summary;conversations-desc;"
        b"messages-desc;mention-occurrences-desc;category-id-asc;complete-observed-evidence;"
        b"resolved-complete-additional-categories;shown-only").hexdigest())


class PreviewError(ValueError):
    """Preview input, binding or output failed its closed boundary."""


def _need(ok, reason):
    if not ok:
        raise PreviewError(reason)


def _typed(model, value):
    _need(type(value) is model, "preview_invalid_type")
    return model.model_validate(value.model_dump(mode="python", warnings=False))


def _context(spec, profile, sources):
    spec = _typed(JobSpec, spec)
    profile = _profile(profile)
    _need(type(sources) is tuple and len(sources) <= MAX_PREVIEW_SOURCES, "preview_source_limit")
    sources = tuple(_typed(SourceMemberSpec, member) for member in sources)
    _need(spec.input.source_count == len(sources)
          and [member.ordinal for member in sources] == list(range(len(sources)))
          and len({member.source.source_ref for member in sources}) == len(sources)
          and inventory_digest(sources) == spec.input.inventory_sha256, "preview_inventory_mismatch")
    _need(spec.algorithm_profile_sha256 == profile.identity_digest(), "preview_profile_mismatch")
    _need(all(member.source.account_scope == account_ref(spec.creator_account_id) for member in sources),
          "preview_account_mismatch")
    return spec, profile, sources


def _profile(profile):
    _need(type(profile) in (AnalysisProfile, SubsetAnalysisProfile), "preview_invalid_profile")
    profile = _typed(type(profile), profile)
    return read_profile(profile.model_dump_json(warnings=False))


def _identity(spec, profile):
    return dict(job_id=spec.job_id, contribution_id=spec.contribution_id, spec_sha256=record_digest(spec),
        input_inventory_sha256=spec.input.inventory_sha256, profile_sha256=profile.identity_digest(),
        selection_policy=SELECTION_POLICY)


def _descriptors(profile):
    return {binding.task: next(task for task in profile.component(binding.component_id).tasks
                              if task.task == binding.task) for binding in profile.bindings}


def _label_body(label):
    return label.model_dump(mode="json", exclude={"label_identity_sha256"})


def _label_digest(value):
    raw = json.dumps(value, default=lambda model: model.model_dump(mode="json"),
                     sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _labels(profile, supplied=None):
    descriptors = _descriptors(profile)
    result = {}
    if supplied is None:
        for task, descriptor in descriptors.items():
            for category in descriptor.enabled_ids:
                value = dict(presentation=profile.presentation, task_contract=descriptor.task_contract,
                    taxonomy=descriptor.taxonomy, category_id=category, display_label=category, label_status="id_only")
                result[(task, category)] = PreviewLabel(**value,
                    label_identity_sha256=_label_digest(value))
        return result
    _need(type(supplied) is tuple, "preview_labels_invalid")
    for label in supplied:
        label = _typed(PreviewLabel, label)
        task = label.task_contract.removesuffix(".v1")
        descriptor = descriptors.get(task)
        key = task, label.category_id
        _need(descriptor is not None and label.category_id in descriptor.enabled_ids
              and label.presentation == profile.presentation and label.taxonomy == descriptor.taxonomy
              and key not in result and label.label_identity_sha256 == _label_digest(_label_body(label)),
              "preview_labels_invalid")
        result[key] = label
    _need(set(result) == {(task, category) for task, descriptor in descriptors.items()
                         for category in descriptor.enabled_ids}, "preview_labels_invalid")
    return result


def _evidence_key(evidence):
    span = evidence.reference.span
    return (evidence.source.source_ref, evidence.source.source_version,
            -1 if span is None else span.start, -1 if span is None else span.end)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "preview_duplicate_json")
        result[key] = value
    return result


def encode_preview(output: PreviewContribution) -> bytes:
    output = _typed(PreviewContribution, output)
    frame = canonical_bytes(output)
    _need(len(frame) <= MAX_PREVIEW_BYTES, "preview_output_limit")
    return frame


def decode_preview(frame: bytes) -> PreviewContribution:
    _need(type(frame) is bytes and 0 < len(frame) <= MAX_PREVIEW_BYTES, "preview_output_limit")
    try:
        json.loads(frame, object_pairs_hook=_unique,
                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        output = PreviewContribution.model_validate_json(frame)
        _need(canonical_bytes(output) == frame, "preview_noncanonical")
        return output
    except (ValueError, RecursionError, OverflowError):
        raise PreviewError("preview_invalid_output") from None


def validate_preview_output(spec, profile, sources, frame: bytes) -> PreviewContribution:
    """Validate output against stored intent; performs no live source lookup."""
    spec, profile, sources = _context(spec, profile, sources)
    output = decode_preview(frame)
    _need(all(getattr(output, key) == value for key, value in _identity(spec, profile).items())
          and output.source_count == len(sources), "preview_intent_mismatch")
    descriptors = _descriptors(profile)
    _need([entry.task for entry in output.tasks] == [task for task in TASKS if task in descriptors],
          "preview_task_set")
    for summary in output.summaries:
        descriptor = descriptors.get(summary.task)
        label = summary.label
        _need(descriptor is not None and label.category_id in descriptor.enabled_ids
              and label.taxonomy == descriptor.taxonomy and label.presentation == profile.presentation
              and label.task_contract == descriptor.task_contract
              and label.label_identity_sha256 == _label_digest(_label_body(label)), "preview_labels_invalid")
        if summary.additional_categories is not None:
            _need(summary.additional_categories < len(descriptor.enabled_ids), "preview_additional_categories")
        _need(list(summary.evidence) == sorted(summary.evidence, key=_evidence_key), "preview_evidence_order")
        for evidence in summary.evidence:
            _need(evidence.ordinal < len(sources), "preview_evidence_source")
            member = sources[evidence.ordinal]
            _need(evidence.source == member.source
                  and evidence.reference.source_revision == member.source_revision
                  and evidence.reference.conversation_ref == conversation_ref(
                      spec.creator_account_id, member.location.conversation_id), "preview_evidence_source")
    return output


def _source(member, source):
    member = _typed(SourceMemberSpec, member)
    _need(type(source) is EvidenceMessage, "preview_source_invalid")
    source = EvidenceMessage.model_validate(source.model_dump(mode="python", warnings=False))
    reference = source.reference()
    binding = SourceBinding(account_scope=reference.account_ref, source_ref=reference.message_ref,
        source_version=reference.source_version_digest.removeprefix("sha256:"),
        speaker_ref=participant_ref(source.account_id, source.sender_id))
    try:
        text_sha256 = hashlib.sha256(source.text.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise PreviewError("preview_source_invalid") from None
    _need(member.source == binding and member.location.conversation_id == source.location.conversation_id
          and member.location.message_id == source.location.message_id
          and member.text_sha256 == text_sha256,
          "preview_source_mismatch")
    return source


def validate_preview_source(output: PreviewContribution, profile, member: SourceMemberSpec,
                            source: EvidenceMessage) -> None:
    """Check one actual guarded source without retaining its body or rerunning NLP."""
    output = _typed(PreviewContribution, output)
    profile = _profile(profile)
    _need(output.profile_sha256 == profile.identity_digest() and member.ordinal < output.source_count,
          "preview_source_mismatch")
    source = _source(member, source)
    expected = source.reference().model_copy(update={"source_revision": member.source_revision})
    for exam in output.examination:
        if exam.ordinal == member.ordinal and exam.coverage is not None:
            _need(exam.coverage.source_codepoints == len(source.text), "preview_source_length")
    for summary in output.summaries:
        for evidence in summary.evidence:
            if evidence.ordinal != member.ordinal:
                continue
            span = evidence.reference.span
            reference = expected.model_copy(update={"span": span})
            _need(evidence.source == member.source and evidence.reference == reference
                  and (span is None or span.end <= len(source.text)), "preview_evidence_source")


def validate_preview_no_output(spec, profile, sources, outcome: NoPreviewOutput) -> NoPreviewOutput:
    spec, profile, _ = _context(spec, profile, sources)
    outcome = _typed(NoPreviewOutput, outcome)
    _need(all(getattr(outcome, key) == value for key, value in _identity(spec, profile).items()),
          "preview_intent_mismatch")
    return outcome


class PreviewAccumulator:
    """Transient candidates for one frozen intent; never a persistence writer."""

    def __init__(self, spec, profile, sources, *, labels=None):
        self.spec, self.profile, self.sources = _context(spec, profile, sources)
        self._descriptors = _descriptors(self.profile)
        self._tasks = tuple(task for task in TASKS if task in self._descriptors)
        self._labels = _labels(self.profile, labels)
        self._exams = {}
        self._candidates = {}
        self._seen = set()
        self._candidate_count = 0
        self._metadata_bytes = 0
        self._overflow = False
        self._finished = False

    def _member(self, ordinal):
        _need(not self._finished and not self._overflow, "preview_accumulator_closed")
        _need(type(ordinal) is int and 0 <= ordinal < len(self.sources) and ordinal not in self._seen,
              "preview_member_invalid")
        return self.sources[ordinal]

    def _examine(self, entry):
        self._exams[(entry.ordinal, entry.task)] = entry
        self._metadata_bytes += len(canonical_bytes(entry))
        if self._metadata_bytes > MAX_PREVIEW_BYTES:
            self._overflow = True

    def unprocessed(self, ordinal):
        self._member(ordinal)
        for task in self._tasks:
            self._examine(PreviewExamination(ordinal=ordinal, task=task, assessment="unprocessed",
                                           reason="dependency_unavailable", coverage=None))
        self._seen.add(ordinal)
        if self._overflow:
            self._discard()

    def add(self, ordinal, source: EvidenceMessage, frame: bytes):
        member = self._member(ordinal)
        source = _source(member, source)
        record = validate_source_bound_record(self.profile, source, frame)
        _need(record.source == member.source, "preview_source_mismatch")
        for stored in record.tasks:
            payload = stored.result.payload
            assessment = "assessed" if payload.outcome.startswith("assessed_") else payload.outcome
            self._examine(PreviewExamination(ordinal=ordinal, task=payload.task, assessment=assessment,
                                            reason=payload.reason, coverage=payload.coverage))
            for finding in payload.findings:
                self._candidate_count += 1
                if self._candidate_count > MAX_PREVIEW_CANDIDATES:
                    self._overflow = True
                    break
                category = finding.topic_id if payload.task == "message_topics" else finding.concept_id
                span = None if payload.task == "message_topics" else SourceSpan(
                    start=finding.span.start, end=finding.span.end)
                reference = source.reference(span).model_copy(update={"source_revision": member.source_revision})
                evidence = PreviewEvidence(ordinal=ordinal, source=member.source, reference=reference,
                    assessment="complete" if assessment == "assessed" else "partial")
                self._candidates.setdefault((payload.task, category), []).append(evidence)
            if self._overflow:
                break
        self._seen.add(ordinal)
        if self._overflow:
            self._discard()

    def _discard(self):
        self._candidates.clear()
        self._exams.clear()

    def finish(self) -> PreviewContribution | NoPreviewOutput:
        _need(not self._finished, "preview_accumulator_closed")
        if self._overflow:
            self._finished = True
            return NoPreviewOutput(**_identity(self.spec, self.profile), disposition="overflow")
        _need(len(self._seen) == len(self.sources), "preview_members_missing")
        self._finished = True
        try:
            tasks = []
            for task in self._tasks:
                counts = Counter(entry.assessment for entry in self._exams.values() if entry.task == task)
                tasks.append(PreviewTaskCoverage(task=task, eligible=len(self.sources),
                    **{name: counts[name] for name in
                       ("assessed", "partial", "uncertain", "unsupported", "failed", "unprocessed")}))
            summaries = []
            for coverage in tasks:
                candidates = [(category, evidence) for (task, category), evidence in self._candidates.items()
                              if task == coverage.task]
                if not candidates:
                    continue
                def rank(candidate):
                    category, evidence = candidate
                    return (-len({e.reference.conversation_ref for e in evidence}),
                        -len({e.ordinal for e in evidence}),
                        -len(evidence) if coverage.task == "concept_mentions" else 0, category)
                category, evidence = min(candidates, key=rank)
                complete = {e.ordinal for e in evidence if e.assessment == "complete"}
                partial = {e.ordinal for e in evidence if e.assessment == "partial"}
                summaries.append(PreviewSummary(kind=KINDS[TASKS.index(coverage.task)],
                    label=self._labels[(coverage.task, category)], evidence=tuple(sorted(evidence, key=_evidence_key)),
                    supporting_conversations=len({e.reference.conversation_ref for e in evidence}),
                    supporting_messages=len(complete | partial), occurrences=len(evidence),
                    complete_supporting_messages=len(complete), partial_supporting_messages=len(partial),
                    share_of_complete_messages=len(complete) / coverage.assessed if coverage.assessed else None,
                    ranking_scope="frozen_scope" if coverage.resolved else "assessed_portion",
                    additional_categories=len(candidates) - 1 if coverage.resolved else None))
            if not summaries:
                return NoPreviewOutput(**_identity(self.spec, self.profile),
                    disposition="empty" if all(t.resolved for t in tasks) else "incomplete")
            output = PreviewContribution(**_identity(self.spec, self.profile), source_count=len(self.sources),
                examination=tuple(self._exams[(ordinal, task)] for ordinal in range(len(self.sources)) for task in self._tasks),
                tasks=tuple(tasks), summaries=tuple(summaries))
            try:
                frame = encode_preview(output)
            except PreviewError:
                return NoPreviewOutput(**_identity(self.spec, self.profile), disposition="overflow")
            return validate_preview_output(self.spec, self.profile, self.sources, frame)
        finally:
            self._discard()
