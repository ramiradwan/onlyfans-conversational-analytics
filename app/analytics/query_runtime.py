"""Runtime-owned question execution, evidence bindings, and expiry cleanup."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import secrets
from threading import RLock, BoundedSemaphore

from app.analytics.errors import AnalyticsError, ProjectionUnavailable
from app.analytics.evidence import EvidenceResolver
from app.analytics.query_handlers import no_later_creator_reply
from app.analytics.query_reader import PublishedQuestionReader
from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion
from app.security.runtime_policy import authorized_account


class PricingNotQualified(AnalyticsError):
    code = "analytics_pricing_not_qualified"
    public_message = "Pricing discussions are not available yet."


class QuestionResources:
    def __init__(self, source, pipeline, *, clock=lambda: datetime.now(timezone.utc)):
        self.source, self.pipeline, self.clock = source, pipeline, clock
        self.secret = secrets.token_bytes(32)
        self.evidence = EvidenceResolver(source, clock=clock, request_refresh=self._request_refresh)
        self._policies = OrderedDict()
        self._refresh_requests = set()
        self._lock = RLock()
        self._slots = BoundedSemaphore(2)
        self._maintenance = None
        self.closed = False

    def _remember(self, policy):
        account = authorized_account(policy, None)
        discarded = []
        with self._lock:
            if self.closed:
                raise ProjectionUnavailable()
            for other, previous in list(self._policies.items()):
                if other != account and previous.identity.principal_id == policy.identity.principal_id:
                    discarded.append(previous)
                    self._policies.pop(other)
                    self._refresh_requests.discard(other)
            self._policies[account] = policy
            self._policies.move_to_end(account)
            while len(self._policies) > 32:
                removed, previous = self._policies.popitem(last=False)
                self._refresh_requests.discard(removed)
                discarded.append(previous)
        for previous in discarded:
            self.evidence.clear_account(previous)
        return account

    def execute(self, policy, request, *, cancellation_check=None):
        from app.analytics.errors import ProjectionBackpressure

        if not self._slots.acquire(blocking=False):
            raise ProjectionBackpressure()
        try:
            return self._execute(policy, request, cancellation_check=cancellation_check)
        finally:
            self._slots.release()

    def _execute(self, policy, request, *, cancellation_check=None):
        from app.analytics.query_contracts import QuestionPlan
        from app.analytics.errors import InvalidAnalyticsRequest

        account = self._remember(policy)
        try:
            plan = QuestionPlan.model_validate(request)
        except (ValueError, TypeError):
            raise InvalidAnalyticsRequest("analytics_question_invalid", "The question or its filters are invalid.") from None
        if plan.question == "pricing_discussions.v1":
            raise PricingNotQualified()
        if not callable(getattr(self.source, "open_question_scope", None)):
            raise ProjectionUnavailable(reason_code="analytics_question_source_unavailable")
        reader = PublishedQuestionReader(self.source, self.pipeline.projections, account,
            policy, self.evidence, self.pipeline.pipeline_revision, self.pipeline.pipeline_config_digest)
        service = AnalyticsQuestionService(reader,
            [RegisteredQuestion("no_later_creator_reply.v1", "canonical.v1", no_later_creator_reply)],
            cursor_secret=self.secret, clock=self.clock)
        return service.execute(policy, plan, cancellation_check=lambda: self._cancelled(account, cancellation_check))

    def resolve(self, policy, reference, *, cancellation_check=None):
        from app.analytics.errors import ProjectionBackpressure

        if not self._slots.acquire(blocking=False):
            raise ProjectionBackpressure()
        try:
            account = self._remember(policy)
            return self.evidence.resolve(policy, reference,
                cancellation_check=lambda: self._cancelled(account, cancellation_check))
        finally:
            self._slots.release()

    def _cancelled(self, account, external):
        with self._lock:
            unavailable = self.closed or account not in self._policies
        return unavailable or (external is not None and external())

    def _request_refresh(self, account):
        with self._lock:
            if account in self._policies:
                self._refresh_requests.add(account)

    def take_refresh(self, account):
        with self._lock:
            pending = account in self._refresh_requests
            self._refresh_requests.discard(account)
            return pending

    def discard(self, account):
        with self._lock:
            policy = self._policies.pop(account, None)
            self._refresh_requests.discard(account)
        if policy is not None:
            self.evidence.clear_account(policy)

    def start(self):
        if self._maintenance is None and not self.closed:
            self._maintenance = asyncio.create_task(self._maintain(), name="analytics-evidence-expiry")

    async def _maintain(self):
        while not self.closed:
            await asyncio.sleep(30)
            self.evidence.expire()

    def close(self):
        with self._lock:
            self.closed = True
            policies = list(self._policies.values())
            self._policies.clear()
            self._refresh_requests.clear()
        for policy in policies:
            self.evidence.clear_account(policy)
        if self._maintenance is not None and not self._maintenance.done():
            loop = self._maintenance.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._maintenance.cancel)
