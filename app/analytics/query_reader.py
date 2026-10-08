"""Join bounded canonical facts to an exact published analytics generation."""

from contextlib import contextmanager, nullcontext, ExitStack
from dataclasses import replace

from app.analytics.errors import ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionResultInvalid


class PublishedQuestionReader:
    def __init__(self, source, store, account, policy, evidence, pipeline_revision, config_digest, *, preparing=None, connections=None):
        self.source, self.store, self.account = source, store, account
        self.policy, self.evidence_resolver = policy, evidence
        self.pipeline_revision, self.config_digest = pipeline_revision, config_digest
        self.preparing = preparing
        self.connections = connections

    def publication(self, scope, budget, read=None):
        read = read or getattr(self.store, "question_snapshot", None)
        if read is None:
            raise ProjectionUnavailable(reason_code="analytics_question_store_unavailable")
        snapshot, revision, config = read(self.account, scope.identity, budget)
        if revision != self.pipeline_revision or config != self.config_digest:
            raise ProjectionUnavailable(reason_code="analytics_question_pipeline_changed")
        return snapshot

    @contextmanager
    def open(self, partition, budget):
        if partition != account_ref(self.account):
            raise QuestionResultInvalid()
        budget.check()
        if self.preparing is not None and self.preparing(self.account) is True:
            self.evidence_resolver.clear_account(self.policy)
            raise ProjectionUnavailable(availability="building")
        acquire = getattr(self.store, "question_publications", None)
        try:
            with ExitStack() as owned:
                analytics_lease = None
                if self.connections is not None:
                    canonical_lease, analytics_lease = owned.enter_context(
                        self.connections.borrow(self.source, self.store, self.account, budget))
                    owned.enter_context(self.source.use_question_connection(canonical_lease))
                scope = owned.enter_context(self.source.open_question_scope(self.account, budget))
                if analytics_lease is not None:
                    owned.enter_context(self.store.activation.read_scope(connection=scope.connection))
                with (acquire(self.account, budget, lease=analytics_lease) if analytics_lease is not None
                      else acquire(self.account, budget) if acquire is not None else nullcontext(None)) as read:
                    session = _QuestionSession(self, scope, self.publication(scope, budget, read), read)
                    yield session
                    session.assert_current(session.snapshot, budget)
            budget.check()  # Connection cleanup remains inside the original request budget.
        except BaseException:
            self.evidence_resolver.clear_account(self.policy)
            raise


class _QuestionSession:
    def __init__(self, owner, scope, snapshot, publication_read=None):
        self.owner, self.scope, self.snapshot = owner, scope, snapshot
        self.publication_read = publication_read

    def assert_current(self, snapshot, budget):
        close_cursors = getattr(self.scope.connection, 'close_cursors', None)
        if callable(close_cursors):
            close_cursors()
        self.scope.check(budget)
        if self.owner.publication(self.scope, budget, self.publication_read) != snapshot:
            raise ProjectionUnavailable(availability="building")
        self.scope.check(budget)

    def conversations(self, question, budget):
        for conversation in self.scope.conversations(question, budget):
            if question.plan.question == "pricing_discussions.v1":
                classifications = self.owner.store.question_pricing(self.owner.account,
                    self.snapshot, [m.message_ref for m in conversation.messages], budget)
                conversation = replace(conversation, messages=tuple(replace(message,
                    pricing=classifications.get(message.message_ref, "missing"),
                    classification_current=message.message_ref in classifications)
                    for message in conversation.messages))
            yield conversation

    def evidence(self, message, budget):
        location = self.scope.locations[message.message_ref]
        record = self.owner.source.read_evidence_message(self.owner.account, location, budget)
        if record is None:
            raise EvidenceUnavailable()
        reference = record.reference()
        if (reference.source_revision != self.snapshot.source_revision
                or reference.message_ref != message.message_ref
                or reference.sent_at != message.sent_at):
            raise EvidenceUnavailable()
        if self.snapshot.retention_due_at is None:
            raise EvidenceUnavailable()
        budget.consume()
        self.owner.evidence_resolver.bind(self.owner.policy, reference, location,
            valid_until=self.snapshot.retention_due_at,
            cancellation_check=lambda: budget.check() or False)
        return reference, location
