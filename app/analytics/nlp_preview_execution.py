"""Transient one-shot preview computation on the shared inference owner."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.text_analysis_preview_execution import PreviewFindingsBinding
    from app.services.text_analysis_preview import TextAnalysisPreviewService
    from app.services.nlp_dependencies import DependencyService

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.nlp_findings_store import source_binding
from app.analytics.nlp_preview import PreviewAccumulator, encode_preview
from app.analytics.nlp_processing import execution_slot, infer_source
from app.analytics.nlp_read_projection import frozen_labels
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionCancelled
from app.models.text_analysis_preview import NoPreviewOutput, PreviewLabel
from app.nlp import v2


@dataclass(frozen=True, slots=True)
class PreviewTurnResult:
    job_id: str
    contribution_id: str
    state: str


class PreviewComputeOwner:
    """Keep candidate records in memory until a validated terminal decision."""

    def __init__(self, service: TextAnalysisPreviewService, dependencies: DependencyService, source, *, clock, monotonic=time.monotonic,
                 cancellation_check=lambda: False):
        from app.services.text_analysis_preview import TextAnalysisPreviewService
        if type(service) is not TextAnalysisPreviewService:
            raise ValueError('preview_compute_owner_invalid')
        if service.dependencies is not dependencies or service.source is not source:
            raise ValueError('preview_compute_owner_mismatch')
        self.service, self.dependencies, self.source = service, dependencies, source
        self.clock, self.monotonic, self.cancellation_check = clock, monotonic, cancellation_check

    def run(self, binding: PreviewFindingsBinding, *, started_at=None) -> PreviewTurnResult | None:
        from app.models.text_analysis_preview_execution import PreviewFindingsBinding
        if type(binding) is not PreviewFindingsBinding:
            raise ValueError('preview_binding_invalid')
        started = self.monotonic() if started_at is None else started_at
        compute_end, total_end = started + 90, started + 120
        with execution_slot() as acquired:
            if not acquired:
                return None
            if self.cancellation_check():
                raise ValueError('preview_cancelled')
            lease_seconds = min(120, int(total_end-self.monotonic()))
            if lease_seconds < 1:
                raise ValueError('preview_deadline')
            token = self.service.claim(binding, lease_seconds=lease_seconds)
            accumulator = PreviewAccumulator(binding.spec, binding.profile, binding.sources)
            fatal = None

            def check():
                nonlocal fatal
                if fatal is not None:
                    raise fatal
                try:
                    if self.cancellation_check():
                        raise ValueError('preview_cancelled')
                    self.service.check(binding, token)
                except Exception as error:
                    fatal = error
                    raise
                return self.monotonic() >= compute_end

            def remaining_compute():
                return max(0, int(min(compute_end-self.monotonic(),
                    token.lease_expires_at-self.service.repository.clock()) * 1000))

            def source_cancelled():
                if self.cancellation_check():
                    raise ValueError('preview_cancelled')
                return self.monotonic() >= compute_end

            def read(member):
                check()
                milliseconds = remaining_compute()
                if milliseconds < 1:
                    return None
                budget = QuestionBudget(QuestionLimits(max_records=100_000,
                    wall_clock_ms=min(30_000, milliseconds)), cancellation_check=source_cancelled)
                value = self.source.read_evidence_message(binding.spec.creator_account_id, EvidenceLocation(**member.location.model_dump()), budget)
                if (value is None or source_binding(value) != member.source
                        or hashlib.sha256(value.text.encode('utf8')).hexdigest() != member.text_sha256):
                    raise ValueError('knowledge_source_changed')
                if value.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) <= self.clock():
                    raise ValueError('knowledge_source_expired')
                check()
                return value.model_copy(update={'source_revision': member.source_revision})

            next_ordinal = 0
            try:
                check()
                if binding.sources and self.monotonic() < compute_end and not self.dependencies.ready(binding.profile):
                    self.dependencies.prepare_installed(binding.profile.identity_digest(), cancellation_check=check)
                if binding.sources and self.monotonic() < compute_end:
                    check()
                    resolver = self.dependencies.presentation_resolver(binding.profile)
                    labels = tuple(PreviewLabel.model_validate({**row,
                        'label_identity_sha256': row['label_identity_sha256'].removeprefix('sha256:')})
                        for row in frozen_labels(binding.profile, resolver))
                    accumulator = PreviewAccumulator(binding.spec, binding.profile, binding.sources, labels=labels)
                    check()
                    with self.dependencies.retain(binding.profile) as execute:
                        for member in binding.sources:
                            if check() or remaining_compute() < 1:
                                break
                            original = read(member)
                            if original is None:
                                break
                            results = infer_source(binding.profile, original, execute,
                                cancelled=check, remaining_ms=remaining_compute, failure_policy="propagate")
                            if results is False or check():
                                break
                            latest = read(member)
                            if latest is None:
                                break
                            model = v2.SubsetEnrichmentRecord if binding.profile.schema_version == 'nlp-profile.v3' else v2.EnrichmentRecord
                            record = model(source=member.source, profile=binding.profile,
                                analyzed_at=self.clock().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                tasks=tuple(v2.StoredTask(source=member.source,
                                    profile_sha256=binding.profile.identity_digest(), result=result) for result in results))
                            accumulator.add(member.ordinal, latest, v2.encode_record(record))
                            next_ordinal += 1
                            if accumulator.overflowed:
                                break
            except Exception as error:
                if fatal is not None:
                    raise fatal
                cutoff = isinstance(error, QuestionCancelled) or (
                    isinstance(error, ValueError) and str(error) in ('nlp_cancelled', 'nlp_deadline'))
                if not cutoff or self.monotonic() < compute_end:
                    raise
            check()
            if not accumulator.overflowed:
                for member in binding.sources[next_ordinal:]:
                    accumulator.unprocessed(member.ordinal, reason='deadline')
            outcome = accumulator.finish()
            check()
            value = outcome if isinstance(outcome, NoPreviewOutput) else encode_preview(outcome)
            milliseconds = int(min(30, total_end-self.monotonic(),
                token.lease_expires_at-self.service.repository.clock()) * 1000)
            if milliseconds < 1:
                raise ValueError('preview_deadline')
            receipt = self.service.commit(binding, token, value, validation_wall_clock_ms=milliseconds)
            return PreviewTurnResult(receipt.job_id, receipt.contribution_id, receipt.state)
