"""Join bounded canonical facts to an exact published analytics generation."""

from contextlib import contextmanager
from dataclasses import replace

from app.analytics.errors import ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionResultInvalid


class PublishedQuestionReader:
    def __init__(self, source, store, account, policy, evidence, pipeline_revision, config_digest):
        self.source, self.store, self.account = source, store, account
        self.policy, self.evidence_resolver = policy, evidence
        self.pipeline_revision, self.config_digest = pipeline_revision, config_digest

    def publication(self, scope, budget):
        read = getattr(self.store, "question_snapshot", None)
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
        with self.source.open_question_scope(self.account, budget) as scope:
            session = _QuestionSession(self, scope, self.publication(scope, budget))
            try:
                yield session
                session.assert_current(session.snapshot, budget)
            except Exception:
                self.evidence_resolver.clear_account(self.policy)
                raise


class _QuestionSession:
    def __init__(self, owner, scope, snapshot):
        self.owner, self.scope, self.snapshot = owner, scope, snapshot

    def assert_current(self, snapshot, budget):
        self.scope.check(budget)
        if self.owner.publication(self.scope, budget) != snapshot:
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
