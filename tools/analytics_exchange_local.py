"""Synthetic exchange targets using the active SQLite projection implementation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3

from app.analytics.exchange_codec import (
    ExchangeInvalid, decode_exchange, encode_exchange, facts_digest,
    question_definitions, seal_exchange, source_binding, validate_exchange,
)
from app.analytics.exchange_contracts import PublishedExchange
from app.analytics.factory import create_analytics_stores
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_contracts import QuestionSnapshot
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.errors import ProjectionUnavailable
from app.analytics.query_facts import QuestionConversation, QuestionMessage
from app.analytics.query_handlers import no_later_creator_reply, pricing_discussions
from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy


def bundle_from_artifact(source, artifact):
    facts = source.facts()
    projection = artifact.projection
    snapshot = QuestionSnapshot(account_ref=projection.account_ref, source_revision=projection.source_revision,
        projection_generation=projection.projection_generation, generation_id="exchange-" + projection.projection_digest[7:],
        canonical_content_digest=projection.canonical_content_digest, projection_digest=projection.projection_digest,
        derived_at=source.now, source_message_count=len(facts.observations),
        retention_due_at=min((m.evidence.sent_at + timedelta(days=90) for m in facts.observations), default=None))
    artifact = artifact.model_copy(update={"nodes": sorted(artifact.nodes, key=lambda n: n.node_id),
                                           "edges": sorted(artifact.edges, key=lambda e: e.edge_id)})
    return seal_exchange(artifact=artifact, snapshot=snapshot, facts=facts,
                         source_facts_digest=facts_digest(facts), question_definitions=question_definitions())


class SQLiteExchangeTarget:
    def __init__(self, directory: Path, source):
        directory.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.repositories = source.attach_canonical(directory)
        self.activation = self.repositories.projection_activation
        self.stores = create_analytics_stores("sqlite", projections_path=directory / "analytics.sqlite3",
            activation=self.activation, canonical_identity_reader=source.read_identity,
            retention_clock=lambda: source.now)
        self.sidecar_path = directory / "exchange.sqlite3"
        self.sidecar = sqlite3.connect(self.sidecar_path)
        self.sidecar.execute("""CREATE TABLE IF NOT EXISTS exchanges (
            digest TEXT PRIMARY KEY, generation TEXT NOT NULL, state TEXT NOT NULL,
            document BLOB NOT NULL)""")
        self.sidecar.commit()
        self.prepared = {}
        self.pending = set()

    def build(self):
        pipeline = AnalyticsPipeline(self.source, projections=self.stores.projections,
                                     clock=lambda: self.source.now, compact_graph=False)
        artifact = pipeline.rebuild_account(self.source.account).artifact
        bundle = bundle_from_artifact(self.source, artifact)
        generation = self.active_generation()
        with self.sidecar:
            self.sidecar.execute("INSERT INTO exchanges VALUES (?,?,'ready',?)",
                                 (bundle.content_digest, generation, encode_exchange(bundle)))
        receipt = self.receipt(bundle, generation)
        self.prepared[bundle.content_digest] = (receipt, bundle, self.sidecar_token())
        return receipt

    def active_generation(self, budget=None):
        budget = budget or QuestionBudget(QuestionLimits(wall_clock_ms=30000))
        budget.check()
        try:
            snapshot, _, _ = self.stores.projections.question_snapshot(self.source.account,
                self.source.read_identity(self.source.account), budget)
            return snapshot.generation_id
        except ProjectionUnavailable:
            return None

    def receipt(self, bundle, generation):
        return PublishedExchange(account_ref=bundle.snapshot.account_ref, generation_id=generation,
            content_digest=bundle.content_digest, source=source_binding(bundle),
            retention_due_at=bundle.snapshot.retention_due_at)

    def import_bytes(self, raw, *, fault=lambda _: None):
        bundle = decode_exchange(raw, expected=self.source.binding(), now=self.source.now)
        existing = self.sidecar.execute("SELECT generation,state,document FROM exchanges WHERE digest=?",
                                        (bundle.content_digest,)).fetchone()
        if existing is not None and bytes(existing[2]) != encode_exchange(bundle):
            raise ExchangeInvalid("analytics_exchange_identity_conflict")
        if existing is not None and existing[1] == "ready":
            receipt = self.receipt(bundle, existing[0])
            self.export(receipt)
            return receipt
        identity = self.source.read_identity(self.source.account)
        generation = existing[0] if existing else None
        if generation is None or self.active_generation() != generation:
            if generation is not None:
                self.stores.projections.reconcile_startup()
                self.stores.projections.discard_generation(generation)
            generation = self.stores.projections.stage_artifact(
                bundle.artifact, creator_account_id=self.source.account, canonical_identity=identity)
            self.pending.add(generation)
            with self.sidecar:
                self.sidecar.execute("INSERT OR REPLACE INTO exchanges VALUES (?,?,'building',?)",
                                     (bundle.content_digest, generation, encode_exchange(bundle)))
        fault("staged")
        validate_exchange(bundle, expected=self.source.binding(), now=self.source.now)
        if self.active_generation() != generation:
            self.stores.projections.publish_generation(generation, creator_account_id=self.source.account,
                                                       canonical_identity=identity)
        self.pending.discard(generation)
        fault("projection_published")
        validate_exchange(bundle, expected=self.source.binding(), now=self.source.now)
        with self.sidecar:
            self.sidecar.execute("UPDATE exchanges SET state='ready' WHERE digest=? AND generation=?",
                                 (bundle.content_digest, generation))
        receipt = self.receipt(bundle, generation)
        self.prepared[bundle.content_digest] = (receipt, bundle, self.sidecar_token())
        return receipt

    def export(self, receipt):
        if receipt.source != self.source.binding():
            raise ExchangeInvalid("analytics_exchange_source_changed")
        row = self.sidecar.execute("SELECT generation,state,document FROM exchanges WHERE digest=?",
                                  (receipt.content_digest,)).fetchone()
        if row is None or row[0] != receipt.generation_id or row[1] != "ready":
            raise ExchangeInvalid("analytics_exchange_not_published")
        bundle = decode_exchange(bytes(row[2]), expected=self.source.binding(), now=self.source.now)
        artifact = self.stores.projections.get_artifact(self.source.account,
            canonical_identity=self.source.read_identity(self.source.account))
        if artifact is None or self.active_generation() != receipt.generation_id:
            raise ExchangeInvalid("analytics_exchange_source_changed")
        artifact = artifact.model_copy(update={"nodes": sorted(artifact.nodes, key=lambda n: n.node_id),
                                               "edges": sorted(artifact.edges, key=lambda e: e.edge_id)})
        recovered = bundle.model_copy(update={"artifact": artifact})
        raw = encode_exchange(validate_exchange(recovered, expected=self.source.binding(), now=self.source.now))
        self.prepared[bundle.content_digest] = (receipt, recovered, self.sidecar_token())
        return raw

    def sidecar_token(self):
        stat = self.sidecar_path.stat()
        return (self.sidecar.total_changes, self.sidecar.execute("PRAGMA data_version").fetchone()[0],
                self.sidecar.execute("PRAGMA schema_version").fetchone()[0], stat.st_dev, stat.st_ino)

    def assert_current(self, receipt, bundle, token, budget):
        budget.check()
        if self.sidecar_token() != token:
            raise ExchangeInvalid("analytics_exchange_source_changed")
        if receipt.source != self.source.binding() or self.active_generation(budget) != receipt.generation_id:
            raise ExchangeInvalid("analytics_exchange_source_changed")
        if bundle.snapshot.retention_due_at is not None and bundle.snapshot.retention_due_at <= self.source.now:
            raise ExchangeInvalid("analytics_exchange_expired")
        row = self.sidecar.execute("SELECT state FROM exchanges WHERE digest=? AND generation=?",
                                  (receipt.content_digest, receipt.generation_id)).fetchone()
        if row is None or row[0] != "ready":
            raise ExchangeInvalid("analytics_exchange_not_published")
        budget.check()

    @contextmanager
    def open(self, account, budget):
        budget.check()
        if account != account_ref(self.source.account):
            raise ExchangeInvalid("analytics_exchange_account")
        row = self.sidecar.execute("SELECT digest,generation FROM exchanges WHERE state='ready' AND generation=?",
                                  (self.active_generation(budget),)).fetchone()
        if row is None:
            raise ExchangeInvalid("analytics_exchange_not_published")
        prepared = self.prepared.get(row[0])
        if prepared is None:
            raise ExchangeInvalid("analytics_exchange_not_prepared")
        receipt, bundle, token = prepared
        self.assert_current(receipt, bundle, token, budget)
        session = ExchangeFactsSession(bundle, lambda: self.assert_current(receipt, bundle, token, budget))
        yield session
        session.assert_current(session.snapshot, budget)

    def close(self):
        for generation in self.pending:
            self.stores.projections.discard_generation(generation)
        self.sidecar.close()
        self.stores.projections.close_retention_scheduler()
        self.stores.projections.close()


class ExchangeFactsSession:
    def __init__(self, bundle, current):
        self.bundle, self.current = bundle, current
        self.snapshot = bundle.snapshot
        self.references = {m.message_ref: m.evidence for m in bundle.facts.observations}

    def assert_current(self, snapshot, budget):
        budget.check()
        self.current()
        if snapshot != self.snapshot:
            raise ExchangeInvalid("analytics_exchange_source_changed")

    def conversations(self, question, budget):
        grouped = {c.conversation_ref: [] for c in self.bundle.facts.conversations}
        for item in self.bundle.facts.observations:
            budget.consume()
            classification = item.classification
            grouped[item.evidence.conversation_ref].append(QuestionMessage(item.message_ref,
                item.evidence.sent_at, item.role, item.kind, item.order, item.ordering,
                classification.status, classification.input_version_digest == item.evidence.source_version_digest,
                classification.language))
        for item in self.bundle.facts.conversations:
            yield QuestionConversation(item.conversation_ref, tuple(grouped[item.conversation_ref]), item.coverage)

    def evidence(self, message, budget):
        budget.consume()
        return self.references[message.message_ref], None


def execute_question(reader, source, plan=None, *, limits=None):
    policy = RuntimePolicy(AuthContext("synthetic-principal", source.account, "creator"), AuthorizationEpoch(0))
    options = {} if limits is None else {"limits": limits}
    service = AnalyticsQuestionService(reader, (
        RegisteredQuestion("no_later_creator_reply.v1", "exchange.v1", no_later_creator_reply),
        RegisteredQuestion("pricing_discussions.v1", "exchange.v1", pricing_discussions)),
        clock=lambda: source.now, **options)
    return service.execute(policy, plan or source.plan())
