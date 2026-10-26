"""Explicitly constructed background processing and frozen findings publication."""
from __future__ import annotations

from datetime import timedelta
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import time
from threading import Lock
from typing import Literal
from uuid import uuid4

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.identity import source_identity
from app.analytics.nlp_findings_store import FrozenFindings, FindingsStore, PAGE_SIZE, MAX_SOURCES, MAX_LOCATORS, source_binding, encoded
from app.analytics.nlp_execution import require_findings_execution
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.nlp import v2
from app.nlp.profiles import read_profile

_EXECUTION = Lock()


@contextmanager
def execution_slot():
    """Acquire the shared nonblocking inference owner."""
    require_findings_execution()
    acquired = _EXECUTION.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _EXECUTION.release()


def infer_source(profile, original, execute, *, cancelled, remaining_ms, failure_policy: Literal["licensed_failed_tasks", "propagate"] = "licensed_failed_tasks"):
    """Validate sequential component results for one original source."""
    if failure_policy not in ('licensed_failed_tasks', 'propagate'):
        raise ValueError('nlp_failure_policy_invalid')
    results = []
    for component in profile.components:
        if cancelled():
            return False
        remaining = min(120_000, remaining_ms())
        if remaining < 1:
            return False
        request = v2.WorkerRequest(request_id=uuid4().hex, profile_sha256=profile.identity_digest(),
            component_id=component.component_id, component_sha256=component.identity_digest(),
            tasks=tuple(task.task for task in component.tasks), deadline_ms=remaining,
            items=(v2.WorkerInput(request_item_id='message', text=original.text,
                text_sha256=hashlib.sha256(original.text.encode()).hexdigest()),))
        v2.validate_request(profile, request.model_dump_json().encode())
        failed = False
        try:
            response = v2.validate_response(profile, request, execute(request, cancelled))
            results.extend(response.items[0].tasks)
        except Exception:
            if failure_policy == 'propagate':
                raise
            failed = True
        if cancelled():
            return False
        if failed:
            for task in component.tasks:
                payload = {'task': task.task, 'outcome': 'failed', 'reason': 'worker_failure',
                    'coverage': {'source_codepoints': len(original.text), 'examined': [],
                        'excluded': ([{'start': 0, 'end': len(original.text)}] if original.text else []),
                        'limits_reached': []}, 'findings': []}
                results.append(v2.BoundTaskResult.model_validate_json(encoded(dict(
                    component_id=component.component_id, component_sha256=component.identity_digest(),
                    text_sha256=request.items[0].text_sha256, payload=payload))))
    return results


@dataclass(slots=True)
class _CancellationState:
    error: Exception | None = None
    observed: bool = False


@dataclass(frozen=True, slots=True)
class _PublicationLimits:
    # Two units per source, two for scope entry, two for the final token check.
    max_records: int = 2 * MAX_LOCATORS + 4
    wall_clock_ms: int = 30_000


class FrozenFindingsPipeline(AnalyticsPipeline):
    """A publication acknowledges only the notification frozen with its input."""
    def __init__(self, *args, on_published, dependency_guard=nullcontext, retained_anchor=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_published = on_published
        self._dependency_guard = dependency_guard
        self.retained_anchor = retained_anchor

    def publish_candidate(self, candidate):
        with self._dependency_guard():
            run = super().publish_candidate(candidate)
            self._on_published()
            return run


class FindingsReadContext:
    """Source validation and reconstruction without execution dependencies."""
    def __init__(self, source, projections, *, authorize, clock, cancellation_check=None):
        self.source, self.projections = source, projections
        self.authorize, self.clock = authorize, clock
        self.cancellation_check = cancellation_check
        self.findings = FindingsStore(projections.database)

    def _dependency_admission(self, account_id):
        self.authorize(account_id)

    def _budget(self):
        return QuestionBudget(QuestionLimits(max_records=100_000,wall_clock_ms=30_000),
                              cancellation_check=self.cancellation_check)

    def _read(self, account_id, location, budget):
        message = self.source.read_evidence_message(account_id,location,budget)
        if message is None or message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) <= self.clock():
            return None
        return message

    def _publication_admission(self, account_id):
        self._admit_inventory(account_id)

    def _admit_inventory(self, account_id):
        """Validate live sources in bounded pages under one connection and deadline."""
        budget = QuestionBudget(_PublicationLimits(), cancellation_check=self.cancellation_check)
        self.authorize(account_id)
        self._dependency_admission(account_id)
        due, invalid, count = None, None, 0
        with self.source.open_question_scope(account_id, budget, verify_source_token=True,
                                              prepare_identity=True) as scope:
            canonical = scope.identity
            with self.projections.database.read() as db:
                cursor = db.execute('SELECT * FROM nlp_sources WHERE account_ref=? ORDER BY source_ref',
                    (self.findings.state(account_id)['account_ref'],))
                while page := cursor.fetchmany(PAGE_SIZE):
                    count += len(page)
                    if count > MAX_LOCATORS:
                        from app.analytics.query_execution import QuestionLimitExceeded
                        raise QuestionLimitExceeded()
                    self.authorize(account_id)
                    self._dependency_admission(account_id)
                    for row in page:
                        message = self._read(account_id, EvidenceLocation(
                            conversation_id=row['conversation_id'], message_id=row['message_id']), budget)
                        binding = source_binding(message) if message is not None else None
                        if (binding is None or binding.source_version != row['source_version']
                                or binding.speaker_ref != row['speaker_ref']):
                            invalid = row['source_ref']
                            break
                        expiry = message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                        due = expiry if due is None else min(due, expiry)
                    if invalid is not None:
                        break
        if invalid is not None:
            self.findings.invalidate_source(account_id, invalid)
            raise ValueError('nlp_source_invalid')
        self._cheap_publication_admission(account_id, due)
        budget.check()
        return canonical, due

    def _cheap_publication_admission(self, account_id, due):
        self.authorize(account_id)
        self._dependency_admission(account_id)
        if due is not None and due <= self.clock():
            raise ValueError('nlp_source_expired')

    def reconcile(self, account_id, *, maximum=MAX_SOURCES):
        """Advance a durable scan by a bounded number of locator pages."""
        if not 1 <= maximum <= MAX_SOURCES:
            raise ValueError('nlp_scan_limit')
        budget, count, complete = self._budget(),0,False
        self.authorize(account_id)
        with self.source.open_question_scope(account_id, budget, verify_source_token=True,
                                              prepare_identity=True) as scope:
            canonical = scope.identity
            state = self.findings.state(account_id)
            identity = encoded([canonical.revision,canonical.content_digest]).decode()
            complete = state['scan_identity']==identity and bool(state['scan_complete'])
            cursor = json.loads(state['scan_cursor']) if state['scan_identity']==identity else None
            while not complete and count < maximum:
                page=self.source.nlp_location_page(account_id,cursor,limit=min(PAGE_SIZE,maximum-count))
                sources=[]
                for location in page:
                    message=self._read(account_id,location,budget)
                    if message is not None:
                        sources.append((message,'live' if message.sent_at >= self.clock()-timedelta(days=1) else 'backfill'))
                epoch,expected_cursor=self.findings.reconcile_page(account_id,canonical,sources,cursor,complete=not page)
                scope.check(budget)
                next_cursor=([page[-1].conversation_id,page[-1].message_id] if page else cursor)
                self.findings.finish_page(account_id,canonical,epoch,next_cursor,complete=not page,
                                          expected_cursor=expected_cursor,check=budget.check)
                if not page:
                    complete = True
                    break
                cursor=next_cursor
                count+=len(page)
        budget.check()
        return complete

    def publish_literals(self, account_id, binding):
        """Seal only cached derivations under the existing live source owner."""
        from app.analytics.nlp_literals import publish
        if type(binding) is not FrozenFindings or binding.account_ref != account_ref(account_id):
            raise ValueError('nlp_literal_account_mismatch')
        budget = self._budget()
        self.authorize(account_id)
        with self.source.open_question_scope(account_id, budget, verify_source_token=True,
                                              prepare_identity=True) as scope:
            result = publish(self.projections.database, binding, canonical=scope.identity,
                             source_current=lambda: scope.check(budget), check=budget.check)
            scope.check(budget)
            return result

    def backfill_literals(self, account_id, binding, *, maximum=PAGE_SIZE):
        """Capture one bounded page of missing derivations without inference."""
        from app.analytics.nlp_findings_store import require_current_snapshot
        from app.analytics.nlp_literals import capture_record, supported, LiteralUnavailable
        if type(binding) is not FrozenFindings or binding.account_ref != account_ref(account_id):
            raise ValueError('nlp_literal_account_mismatch')
        if not 1 <= maximum <= PAGE_SIZE:
            raise ValueError('nlp_literal_backfill_limit')
        self.authorize(account_id)
        budget = self._budget()
        completed = 0
        with self.source.open_question_scope(account_id, budget, verify_source_token=True,
                                              prepare_identity=True) as scope:
            with self.projections.database.read() as db:
                if not supported(db):
                    raise LiteralUnavailable()
                value = require_current_snapshot(db, binding, scope.identity, verify_records=False)
                profile = read_profile(encoded(value['snapshot']['profile']))
                page = []
                for key in value['records']:
                    budget.check()
                    if not db.execute('SELECT 1 FROM nlp_literal_derivations WHERE account_ref=? AND record_digest=?',
                                      (binding.account_ref,key)).fetchone():
                        raw = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?',
                                         (binding.account_ref,key)).fetchone()
                        if raw is None or hashlib.sha256(bytes(raw[0])).hexdigest() != key:
                            raise ValueError('nlp_record_invalid')
                        record = v2.read_record(bytes(raw[0]))
                        location = db.execute('SELECT conversation_id,message_id FROM nlp_sources WHERE account_ref=? AND source_ref=?',
                                              (binding.account_ref,record.source.source_ref)).fetchone()
                        if location is None:
                            raise ValueError('nlp_source_invalid')
                        page.append((key,record,bytes(raw[0]),EvidenceLocation(conversation_id=location[0],message_id=location[1])))
                        if len(page) == maximum:
                            break
            for key,record,raw,location in page:
                budget.check()
                original = self._read(account_id,location,budget)
                if original is None or source_binding(original) != record.source:
                    raise ValueError('nlp_source_invalid')
                scope.check(budget)
                with self.projections.database.transaction() as db:
                    require_current_snapshot(db,binding,scope.identity,verify_records=False)
                    capture_record(db,profile,original,record,key,check=budget.check,record_bytes=raw)
                completed += 1
            scope.check(budget)
        return completed



class FindingsRunner(FindingsReadContext):
    """Providers and background scheduling require explicit runtime configuration.

    Authorization raises on refusal. Execution readiness returns a boolean for
    the exact profile; it cannot confer authorization or release qualification.
    The adapter honors request deadlines and polls the cancellation callback.
    """
    def __init__(self, source, projections, *, authorize, execution_ready, adapter,
                 clock, enrichment=None, lease_clock=time.time, dependencies=None, label_resolver=None,
                 cancellation_check=None, canonical_execution=None, execution_service=None, execution_binding=None):
        if canonical_execution is not None:
            from app.analytics.nlp_canonical_execution import CanonicalFindingsExecution
            if type(canonical_execution) is not CanonicalFindingsExecution:
                raise ValueError('nlp_canonical_owner_invalid')
        self.canonical_execution = canonical_execution
        if execution_service is not None or execution_binding is not None:
            from app.services.text_analysis_execution import TextAnalysisExecutionService, LicensedFindingsBinding
            if (type(execution_service) is not TextAnalysisExecutionService or type(execution_binding) is not LicensedFindingsBinding
                    or execution_binding.execution is not canonical_execution):
                raise ValueError('nlp_execution_binding_invalid')
        self.execution_service, self.execution_binding = execution_service, execution_binding
        self.source, self.projections = source, projections
        self.authorize, self.execution_ready, self.adapter = authorize, execution_ready, adapter
        self.clock, self.enrichment = clock, enrichment
        self.dependencies = dependencies
        self.label_resolver = label_resolver
        self.cancellation_check = cancellation_check
        self.findings = FindingsStore(projections.database,clock=lease_clock)

    def _dependency_admission(self, account_id):
        if self.dependencies is not None:
            profile = read_profile(self.findings.state(account_id)['profile_json'])
            if self.execution_service is not None:
                self.execution_service.authorize_binding(self.execution_binding, account_id)
                state = self.dependencies.persistence.authority()
                if state['trust_sha256'] is None or profile.identity_digest() in state['withdrawals']:
                    raise ValueError('nlp_recovery_authority_unavailable')
                return
            if not self.dependencies.ready(profile):
                raise ValueError('nlp_dependency_unavailable')

    def process_one(self, account_id):
        require_findings_execution()
        if self.dependencies is None or self.canonical_execution is not None:
            return self._process_one(account_id)
        self.authorize(account_id)
        profile = read_profile(self.findings.state(account_id)['profile_json'])
        if not self.dependencies.ready(profile):
            return False
        with self.dependencies.retain(profile) as execute:
            return self._process_one(account_id, bound_adapter=execute if self.adapter is None else self.adapter,
                                     expected_profile=profile.identity_digest())

    def _process_one(self, account_id, *, bound_adapter=None, expected_profile=None):
        """Perform one bounded job; projection reads never call this method."""
        require_findings_execution()
        if not _EXECUTION.acquire(blocking=False):
            return False
        claim=None
        work=None
        try:
            self.authorize(account_id)
            self.reconcile(account_id)
            state=self.findings.state(account_id)
            profile=read_profile(state['profile_json'])
            if expected_profile is not None and profile.identity_digest() != expected_profile:
                return False
            if state['mode']!='enabled' or (self.canonical_execution is None and not self.execution_ready(profile)):
                return False
            claim=self.findings.claim(account_id)
            if claim is None:
                return False
            if claim.selection_epoch != state['selection_epoch'] or claim.admission_epoch != state['admission_epoch']:
                return False
            location=EvidenceLocation(conversation_id=claim.conversation_id,message_id=claim.message_id)
            original=self._read(account_id,location,self._budget())
            if original is None or source_binding(original).source_version!=claim.source_version:
                self.findings.invalidate_source(account_id,claim.source_ref)
                return False
            if self.canonical_execution is not None:
                lease_seconds = min(120, int(claim.expires_at-self.findings.clock()))
                if lease_seconds < 1:
                    return False
                work = self.canonical_execution.begin(self.source, profile, original, lease_seconds=lease_seconds)
                if work is None:
                    return False
                self._budget().check()
                self.authorize(account_id)
                if work.terminal_reason is not None:
                    self.findings.abandon(claim, work.terminal_reason)
                    return True
                if work.sealed or work.staged_record is not None:
                    if not work.sealed:
                        self.canonical_execution.seal(work)
                    self._replay_canonical(work, claim)
                    return True
            if any(len(original.text)>component.max_source_codepoints for component in profile.components):
                if work is not None:
                    self.canonical_execution.finish_without_output(work, 'unsupported_input')
                self.findings.abandon(claim,'unsupported_input')
                return True
            if work is not None and not self.execution_ready(profile):
                return False
            cancellation = _CancellationState()
            def cancelled():
                self.authorize(account_id)
                if cancellation.observed:
                    return True
                try:
                    if self.cancellation_check is not None and self.cancellation_check():
                        cancellation.observed=True
                        return True
                except Exception as error:
                    cancellation.error=error
                    raise
                stopped = (not self.findings.valid_claim(claim) or not self.execution_ready(profile)
                        or (self.dependencies is not None and not self.dependencies.ready(profile)))
                if not stopped and work is not None:
                    self.canonical_execution.check(work)
                return stopped
            if work is not None and self.dependencies is not None:
                if not self.dependencies.ready(profile):
                    return False
                with self.dependencies.retain(profile) as retained_adapter:
                    results = self._infer_components(profile, original, claim, cancelled,
                        self.adapter if self.adapter is not None else retained_adapter, cancellation, work=work)
            else:
                results = self._infer_components(profile, original, claim, cancelled, bound_adapter, cancellation, work=work)
            if cancellation.error is not None:
                self.authorize(account_id)
                raise cancellation.error
            if results is False or cancelled():
                return False
            latest=self._read(account_id,location,self._budget())
            if latest is None or source_binding(latest)!=source_binding(original):
                self.findings.invalidate_source(account_id,claim.source_ref)
                return False
            record_model = v2.SubsetEnrichmentRecord if profile.schema_version == 'nlp-profile.v3' else v2.EnrichmentRecord
            record=record_model(source=source_binding(latest),profile=profile,
                analyzed_at=self.clock().strftime('%Y-%m-%dT%H:%M:%SZ'),
                tasks=tuple(v2.StoredTask(source=source_binding(latest),profile_sha256=profile.identity_digest(),result=r) for r in results))
            try:
                frame=v2.encode_record(record)
            except ValueError:
                if work is not None:
                    self.canonical_execution.finish_without_output(work, 'record_limit')
                self.findings.abandon(claim,'record_limit')
                return True
            if work is not None:
                if cancelled():
                    return False
                self.canonical_execution.seal(work, frame)
                self._replay_canonical(work, claim)
                return True
            return self.findings.complete(claim,profile,latest,frame)
        finally:
            try:
                if claim is not None:
                    self.findings.release(claim)
            finally:
                _EXECUTION.release()

    def _replay_canonical(self, work, claim):
        guard = self.execution_service.retained_visibility(self.execution_binding) if self.execution_service is not None else nullcontext()
        with guard:
            self.findings.release(claim)
            return self.canonical_execution.replay(work, self.findings)


    def _infer_components(self, profile, original, claim, cancelled, bound_adapter, cancellation, *, work=None):
        def remaining():
            value = min(120_000, int((claim.expires_at-self.findings.clock())*1000))
            if work is not None:
                value = min(value, int((work.attempt.lease_expires_at-self.canonical_execution.repository.clock())*1000))
            return value
        def check():
            stopped = cancelled()
            if cancellation.error is not None:
                self.authorize(original.account_id)
                raise cancellation.error
            return stopped
        return infer_source(profile, original, self.adapter if bound_adapter is None else bound_adapter,
                            cancelled=check, remaining_ms=remaining)

    def frozen_pipeline(self, account_id):
        """Revalidate live eligibility before freezing immutable publication input."""
        self.authorize(account_id)
        self.reconcile(account_id)
        canonical, due = self._admit_inventory(account_id)
        revision=self.findings.state(account_id)['notification_revision']
        frozen=self.findings.freeze(account_id,canonical,label_resolver=self.label_resolver)
        profile=read_profile(self.findings.state(account_id)['profile_json'])
        retained_anchor = (self.canonical_execution.retained_anchor(self.findings, frozen)
                           if self.execution_service is not None else False)
        def published():
            self.findings.acknowledge(account_id,revision)
            from app.analytics.nlp_literals import LiteralUnavailable, LiteralIntegrityError
            try:
                self.publish_literals(account_id, frozen)
            except (LiteralUnavailable, LiteralIntegrityError):
                pass
        return FrozenFindingsPipeline(self.source,projections=self.projections,enrichment=self.enrichment,
            clock=self.clock,nlp_findings=frozen,nlp_authorize=lambda:self._cheap_publication_admission(account_id,due),
            nlp_admit=lambda:self._publication_admission(account_id),
            on_published=published,
            retained_anchor=retained_anchor,
            dependency_guard=(lambda:self.execution_service.retained_visibility(self.execution_binding))
                if self.execution_service is not None else
                (lambda:self.dependencies.publication(profile)) if self.dependencies is not None else nullcontext)

    async def notify_scheduler(self, scheduler, account_id):
        """Redeliver durable input changes until scheduler publication acknowledges."""
        state=self.findings.state(account_id)
        if state['acknowledged_revision']==state['notification_revision']:
            return False
        await scheduler.request_findings(account_id,source_identity(self.source,account_id).revision,
                                         state['notification_revision'])
        return True

    def publish_pending(self, account_id):
        """Acknowledge only completed publication; restart redelivers lost notices."""
        state=self.findings.state(account_id)
        if state['acknowledged_revision']==state['notification_revision']:
            return None
        pipeline=self.frozen_pipeline(account_id)
        candidate=pipeline.build_candidate(account_id)
        self.authorize(account_id)
        return pipeline.publish_candidate(candidate)
