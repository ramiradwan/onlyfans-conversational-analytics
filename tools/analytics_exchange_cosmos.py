"""Conformance-only publication and question reads through reviewed Gremlin plans."""

from contextlib import contextmanager
from uuid import uuid4

from app.analytics.exchange_codec import (
    ExchangeInvalid, canonical_bytes, decode_exchange, encode_exchange,
    source_binding, validate_exchange, decode_value,
)
from app.analytics.exchange_contracts import ExchangeObservation
from app.analytics.query_facts import QuestionConversation, QuestionMessage
from tools.analytics_exchange_local import ExchangeFactsSession
from tools.analytics_exchange_records import exchange_records, instant_us, records_digest, recover_exchange


class CosmosExchangeTarget:
    def __init__(self, process, source, *, namespace=None):
        self.process, self.source = process, source
        self.namespace = namespace or uuid4().hex
        self.scope = None
        self.manifest = None
        self.bundle = None
        self.metrics = {"request_charge": 0.0, "retries": 0}

    def request(self, op, *, budget=None, **fields):
        timeout = None if budget is None else budget.remaining_seconds()
        result = self.process.request(op, self.scope, timeout=timeout, **fields)
        for key in self.metrics:
            self.metrics[key] += result.get("metrics", {}).get(key, 0)
        return result.get("result")

    def import_bytes(self, raw, *, fault=lambda _: None):
        bundle = decode_exchange(raw, expected=self.source.binding(), now=self.source.now)
        self.scope = {"account_ref": bundle.snapshot.account_ref, "namespace": self.namespace,
                      "generation": bundle.content_digest[7:]}
        records = exchange_records(bundle)
        manifest = {"schema_version": "analytics-exchange-publication.v1", "content_digest": bundle.content_digest,
            "records_digest": records_digest(records), "record_count": len(records),
            "source": source_binding(bundle).model_dump(mode="json"),
            "retention_due_at": bundle.snapshot.model_dump(mode="json")["retention_due_at"]}
        existing = self.request("read_manifest")
        if existing is not None:
            if existing != canonical_bytes(manifest).decode():
                raise ExchangeInvalid("analytics_exchange_identity_conflict")
            self.manifest = manifest
            if self.export() != raw:
                raise ExchangeInvalid("analytics_exchange_digest")
            self.bundle = bundle
            return
        for record in records:
            if "source_id" not in record:
                self.request("put_vertex", **record)
        fault("vertices")
        for record in records:
            if "source_id" in record:
                self.request("put_edge", **record)
        fault("records")
        restored = self.read_records()
        if restored != records or encode_exchange(recover_exchange(restored)) != raw:
            raise ExchangeInvalid("analytics_exchange_remote_mismatch")
        validate_exchange(bundle, expected=self.source.binding(), now=self.source.now)
        fault("validated")
        validate_exchange(bundle, expected=self.source.binding(), now=self.source.now)
        self.request("publish", manifest_json=canonical_bytes(manifest).decode())
        fault("published")
        self.manifest = manifest
        self.assert_current(bundle)
        self.bundle = bundle

    def read_records(self):
        records, after = [], ""
        while True:
            batch = self.request("read_records", after=after, limit=256)
            if not isinstance(batch, list) or len(batch) > 256:
                raise ExchangeInvalid("analytics_exchange_records")
            if not batch:
                return records
            for record in batch:
                if not isinstance(record, dict) or record.get("logical_id", "") <= after:
                    raise ExchangeInvalid("analytics_exchange_record_order")
                after = record["logical_id"]
                records.append(record)
            if len(records) > 100000:
                raise ExchangeInvalid("analytics_exchange_record_limit")

    def assert_current(self, bundle, budget=None):
        if budget is not None:
            budget.check()
        if source_binding(bundle) != self.source.binding():
            raise ExchangeInvalid("analytics_exchange_source_changed")
        if bundle.snapshot.retention_due_at is not None and bundle.snapshot.retention_due_at <= self.source.now:
            raise ExchangeInvalid("analytics_exchange_expired")
        if self.manifest is None or self.request("read_manifest", budget=budget) != canonical_bytes(self.manifest).decode():
            raise ExchangeInvalid("analytics_exchange_not_published")

    def export(self):
        if self.manifest is None:
            raise ExchangeInvalid("analytics_exchange_not_published")
        records = self.read_records()
        if (len(records) != self.manifest["record_count"]
                or records_digest(records) != self.manifest["records_digest"]):
            raise ExchangeInvalid("analytics_exchange_digest")
        bundle = recover_exchange(records)
        if bundle.content_digest != self.manifest["content_digest"]:
            raise ExchangeInvalid("analytics_exchange_digest")
        self.assert_current(bundle)
        return encode_exchange(bundle)

    @contextmanager
    def open(self, account, budget):
        if self.scope is None or account != self.scope["account_ref"]:
            raise ExchangeInvalid("analytics_exchange_account")
        bundle = self.bundle
        if bundle is None:
            raise ExchangeInvalid("analytics_exchange_not_published")
        self.assert_current(bundle, budget)
        session = CosmosFactsSession(self, bundle)
        yield session
        session.assert_current(session.snapshot, budget)

    def cleanup(self):
        if self.scope is not None:
            self.request("cleanup")
            if self.request("read_manifest") is not None or self.read_records():
                raise ExchangeInvalid("analytics_exchange_cleanup")
            self.manifest = None
            self.bundle = None


class CosmosFactsSession(ExchangeFactsSession):
    def __init__(self, target, bundle):
        super().__init__(bundle, lambda: target.assert_current(bundle))
        self.target = target
        self.observations = {item.message_ref: item for item in bundle.facts.observations}

    def assert_current(self, snapshot, budget):
        if snapshot != self.snapshot:
            raise ExchangeInvalid("analytics_exchange_source_changed")
        self.target.assert_current(self.bundle, budget)

    def conversations(self, question, budget):
        grouped = {c.conversation_ref: [] for c in self.bundle.facts.conversations}
        after, seen = "", set()
        expected = set()
        for item in self.bundle.facts.observations:
            budget.check()
            if not question.retention_cutoff_exclusive < item.evidence.sent_at <= question.cutoff:
                continue
            if (question.plan.filters.conversation_ref is not None
                    and item.evidence.conversation_ref != question.plan.filters.conversation_ref):
                continue
            if (question.plan.question == "pricing_discussions.v1"
                    and not question.plan.start <= item.evidence.sent_at < question.plan.end):
                continue
            expected.add("observation:" + item.message_ref)
        while True:
            limit = min(256, budget.limits.max_records - budget.records_examined + 1)
            batch = self.target.request("question_facts", budget=budget, after=after, limit=limit,
                question=question.plan.question, start_us=instant_us(question.plan.start),
                end_us=instant_us(question.plan.end), cutoff_us=instant_us(question.cutoff),
                retention_us=instant_us(question.retention_cutoff_exclusive),
                conversation_ref=question.plan.filters.conversation_ref)
            if not isinstance(batch, list) or len(batch) > limit:
                raise ExchangeInvalid("analytics_exchange_records")
            if not batch:
                break
            for record in batch:
                budget.consume()
                logical = record.get("logical_id", "")
                if logical <= after or logical in seen:
                    raise ExchangeInvalid("analytics_exchange_record_order")
                seen.add(logical)
                after = logical
                item = ExchangeObservation.model_validate(decode_value(record["record_json"]))
                if (logical != "observation:" + item.message_ref
                        or self.observations.get(item.message_ref) != item
                        or item.evidence.conversation_ref not in grouped):
                    raise ExchangeInvalid("analytics_exchange_evidence")
                classification = item.classification
                grouped[item.evidence.conversation_ref].append(QuestionMessage(item.message_ref,
                    item.evidence.sent_at, item.role, item.kind, item.order, item.ordering,
                    classification.status, classification.input_version_digest == item.evidence.source_version_digest,
                    classification.language))
        if seen != expected:
            raise ExchangeInvalid("analytics_exchange_evidence_incomplete")
        for item in self.bundle.facts.conversations:
            yield QuestionConversation(item.conversation_ref, tuple(grouped[item.conversation_ref]), item.coverage)
