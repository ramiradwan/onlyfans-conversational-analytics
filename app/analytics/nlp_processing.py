"""Explicitly constructed background processing and frozen findings publication."""
from __future__ import annotations

from datetime import timedelta
from contextlib import nullcontext
import hashlib
import json
import time
from threading import Lock
from uuid import uuid4

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.identity import source_identity
from app.analytics.nlp_findings_store import FindingsStore, PAGE_SIZE, MAX_SOURCES, source_binding, encoded
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.nlp import v2
from app.nlp.profiles import AnalysisProfile

_EXECUTION = Lock()


class FrozenFindingsPipeline(AnalyticsPipeline):
    """A publication acknowledges only the notification frozen with its input."""
    def __init__(self, *args, on_published, dependency_guard=nullcontext, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_published = on_published
        self._dependency_guard = dependency_guard

    def publish_candidate(self, candidate):
        with self._dependency_guard():
            run = super().publish_candidate(candidate)
            self._on_published()
            return run


class FindingsRunner:
    """Providers are explicit dependencies; no default runtime installs this runner.

    Authorization raises on refusal. Execution readiness returns a boolean for
    the exact profile; it cannot confer authorization or release qualification.
    The adapter honors request deadlines and polls the cancellation callback.
    """
    def __init__(self, source, projections, *, authorize, execution_ready, adapter,
                 clock, enrichment=None, lease_clock=time.time, dependencies=None):
        self.source, self.projections = source, projections
        self.authorize, self.execution_ready, self.adapter = authorize, execution_ready, adapter
        self.clock, self.enrichment = clock, enrichment
        self.dependencies = dependencies
        self.findings = FindingsStore(projections.database,clock=lease_clock)

    def _budget(self):
        return QuestionBudget(QuestionLimits(max_records=100_000,wall_clock_ms=30_000))

    def _read(self, account_id, location, budget):
        message = self.source.read_evidence_message(account_id,location,budget)
        if message is None or message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) <= self.clock():
            return None
        return message

    def _publication_admission(self, account_id):
        self.authorize(account_id)
        self._dependency_admission(account_id)
        budget=self._budget()
        with self.projections.database.read() as db:
            for row in db.execute('SELECT * FROM nlp_sources WHERE account_ref=? ORDER BY source_ref',
                    (self.findings.state(account_id)['account_ref'],)):
                message=self._read(account_id,EvidenceLocation(conversation_id=row['conversation_id'],message_id=row['message_id']),budget)
                if message is None or source_binding(message).source_version!=row['source_version']:
                    raise ValueError('nlp_source_invalid')

    def _cheap_publication_admission(self, account_id, due):
        self.authorize(account_id)
        self._dependency_admission(account_id)
        if due is not None and due <= self.clock():
            raise ValueError('nlp_source_expired')

    def _dependency_admission(self, account_id):
        if self.dependencies is not None:
            profile = AnalysisProfile.model_validate_json(self.findings.state(account_id)['profile_json'])
            if not self.dependencies.ready(profile):
                raise ValueError('nlp_dependency_unavailable')

    def reconcile(self, account_id, *, maximum=MAX_SOURCES):
        """Advance a durable scan by a bounded number of locator pages."""
        if not 1 <= maximum <= MAX_SOURCES:
            raise ValueError('nlp_scan_limit')
        self.authorize(account_id)
        canonical = source_identity(self.source,account_id)
        state = self.findings.state(account_id)
        identity = encoded([canonical.revision,canonical.content_digest]).decode()
        if state['scan_identity']==identity and state['scan_complete']:
            return True
        cursor = json.loads(state['scan_cursor']) if state['scan_identity']==identity else None
        budget, count = self._budget(),0
        while count < maximum:
            page=self.source.nlp_location_page(account_id,cursor,limit=min(PAGE_SIZE,maximum-count))
            sources=[]
            for location in page:
                message=self._read(account_id,location,budget)
                if message is not None:
                    sources.append((message,'live' if message.sent_at >= self.clock()-timedelta(days=1) else 'backfill'))
            epoch,expected_cursor=self.findings.reconcile_page(account_id,canonical,sources,cursor,complete=not page)
            if source_identity(self.source,account_id)!=canonical:
                raise ValueError('nlp_source_changed')
            next_cursor=([page[-1].conversation_id,page[-1].message_id] if page else cursor)
            self.findings.finish_page(account_id,canonical,epoch,next_cursor,complete=not page,expected_cursor=expected_cursor)
            if not page:
                return True
            cursor=next_cursor
            count+=len(page)
        return False

    def process_one(self, account_id):
        if self.dependencies is None:
            return self._process_one(account_id)
        self.authorize(account_id)
        profile = AnalysisProfile.model_validate_json(self.findings.state(account_id)['profile_json'])
        if not self.dependencies.ready(profile):
            return False
        with self.dependencies.retain(profile):
            return self._process_one(account_id)

    def _process_one(self, account_id):
        """Perform one bounded job; projection reads never call this method."""
        if not _EXECUTION.acquire(blocking=False):
            return False
        claim=None
        try:
            self.authorize(account_id)
            self.reconcile(account_id)
            state=self.findings.state(account_id)
            profile=AnalysisProfile.model_validate_json(state['profile_json'])
            if state['mode']!='enabled' or not self.execution_ready(profile):
                return False
            claim=self.findings.claim(account_id)
            if claim is None:
                return False
            location=EvidenceLocation(conversation_id=claim.conversation_id,message_id=claim.message_id)
            original=self._read(account_id,location,self._budget())
            if original is None or source_binding(original).source_version!=claim.source_version:
                self.findings.invalidate_source(account_id,claim.source_ref)
                return False
            if any(len(original.text)>component.max_source_codepoints for component in profile.components):
                self.findings.abandon(claim,'unsupported_input')
                return True
            def cancelled():
                self.authorize(account_id)
                return (not self.findings.valid_claim(claim) or not self.execution_ready(profile)
                        or (self.dependencies is not None and not self.dependencies.ready(profile)))
            results=[]
            for component in profile.components:
                if cancelled():
                    return False
                remaining_ms=min(120_000,int((claim.expires_at-self.findings.clock())*1000))
                if remaining_ms<1:
                    return False
                request=v2.WorkerRequest(request_id=uuid4().hex,profile_sha256=profile.identity_digest(),
                    component_id=component.component_id,component_sha256=component.identity_digest(),
                    tasks=tuple(task.task for task in component.tasks),deadline_ms=remaining_ms,
                    items=(v2.WorkerInput(request_item_id='message',text=original.text,
                        text_sha256=hashlib.sha256(original.text.encode()).hexdigest()),))
                v2.validate_request(profile,request.model_dump_json().encode())
                failed=False
                try:
                    response=v2.validate_response(profile,request,self.adapter(request,cancelled))
                    results.extend(response.items[0].tasks)
                except Exception:
                    failed=True
                if failed:
                    if cancelled():
                        return False
                    # A failed component retains a truthful result for each of its tasks.
                    for task in component.tasks:
                        payload={'task':task.task,'outcome':'failed','reason':'worker_failure',
                            'coverage':{'source_codepoints':len(original.text),'examined':[],
                                'excluded':([{'start':0,'end':len(original.text)}] if original.text else []),'limits_reached':[]},'findings':[]}
                        results.append(v2.BoundTaskResult.model_validate_json(encoded(dict(component_id=component.component_id,
                            component_sha256=component.identity_digest(),text_sha256=request.items[0].text_sha256,payload=payload))))
            if cancelled():
                return False
            latest=self._read(account_id,location,self._budget())
            if latest is None or source_binding(latest)!=source_binding(original):
                self.findings.invalidate_source(account_id,claim.source_ref)
                return False
            record=v2.EnrichmentRecord(source=source_binding(latest),profile=profile,
                analyzed_at=self.clock().strftime('%Y-%m-%dT%H:%M:%SZ'),
                tasks=tuple(v2.StoredTask(source=source_binding(latest),profile_sha256=profile.identity_digest(),result=r) for r in results))
            try:
                frame=v2.encode_record(record)
            except ValueError:
                self.findings.abandon(claim,'record_limit')
                return True
            return self.findings.complete(claim,profile,latest,frame)
        finally:
            try:
                if claim is not None:
                    self.findings.release(claim)
            finally:
                _EXECUTION.release()

    def frozen_pipeline(self, account_id):
        """Revalidate live eligibility before freezing immutable publication input."""
        self.authorize(account_id)
        self.reconcile(account_id)
        budget=self._budget()
        canonical=source_identity(self.source,account_id)
        due=None
        with self.projections.database.read() as db:
            cursor=db.execute('SELECT * FROM nlp_sources WHERE account_ref=? ORDER BY source_ref',
                (self.findings.state(account_id)['account_ref'],))
            for row in cursor:
                message=self._read(account_id,EvidenceLocation(conversation_id=row['conversation_id'],message_id=row['message_id']),budget)
                if message is None or source_binding(message).source_version!=row['source_version']:
                    # Release the read connection before changing derived state.
                    invalid=row['source_ref']
                    break
                expiry=message.sent_at+timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                due=expiry if due is None else min(due,expiry)
            else:
                invalid=None
        if invalid is not None:
            self.findings.invalidate_source(account_id,invalid)
            raise ValueError('nlp_source_invalid')
        if source_identity(self.source,account_id)!=canonical:
            raise ValueError('nlp_source_changed')
        revision=self.findings.state(account_id)['notification_revision']
        frozen=self.findings.freeze(account_id,canonical)
        profile=AnalysisProfile.model_validate_json(self.findings.state(account_id)['profile_json'])
        return FrozenFindingsPipeline(self.source,projections=self.projections,enrichment=self.enrichment,
            clock=self.clock,nlp_findings=frozen,nlp_authorize=lambda:self._cheap_publication_admission(account_id,due),
            nlp_admit=lambda:self._publication_admission(account_id),
            on_published=lambda:self.findings.acknowledge(account_id,revision),
            dependency_guard=(lambda:self.dependencies.publication(profile)) if self.dependencies is not None else nullcontext)

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
