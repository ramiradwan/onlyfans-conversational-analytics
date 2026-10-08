"""Canonical exchange encoding and validation without storage authority."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
import re

from app.analytics.exchange_contracts import ExchangeBundle, ExchangeFacts, ExchangeSourceBinding
from app.analytics.identity import pipeline_identity_digest
from app.analytics.projection_encoding import projection_digest
from app.analytics.query_contracts import utc_instant
from app.analytics.query_handlers import no_later_creator_reply, pricing_discussions
from app.analytics.query_service import RegisteredQuestion
from app.analytics.shared_graph import projection_graph_digest

MAX_EXCHANGE_BYTES = 64 * 1024 * 1024
MAX_EXCHANGE_RECORDS = 100000
MAX_SAFE_INTEGER = 9007199254740991
_INTEGER_TAG = "$analytics_integer"
_DOMAIN = b"analytics-exchange.v1\0"


class ExchangeInvalid(ValueError):
    def __init__(self, code="analytics_exchange_invalid"):
        self.code = code
        super().__init__(code)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(wire_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def wire_value(value, depth=0):
    if depth > 32:
        raise ExchangeInvalid("analytics_exchange_depth")
    if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
        number = str(value)
        if len(number.lstrip("-")) > 1024:
            raise ExchangeInvalid("analytics_exchange_integer_encoding")
        return {_INTEGER_TAG: number}
    if isinstance(value, dict):
        if _INTEGER_TAG in value:
            raise ExchangeInvalid("analytics_exchange_scalar_tag")
        return {key: wire_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_value(item, depth + 1) for item in value]
    return value


def logical_value(value, depth=0):
    if depth > 32:
        raise ExchangeInvalid("analytics_exchange_depth")
    if isinstance(value, dict):
        if _INTEGER_TAG in value:
            number = value[_INTEGER_TAG]
            if (len(value) != 1 or not isinstance(number, str)
                    or not re.fullmatch(r"-?[1-9][0-9]{0,1023}", number)):
                raise ExchangeInvalid("analytics_exchange_scalar_tag")
            result = int(number)
            if abs(result) <= MAX_SAFE_INTEGER:
                raise ExchangeInvalid("analytics_exchange_scalar_tag")
            return result
        return {key: logical_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [logical_value(item, depth + 1) for item in value]
    if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
        raise ExchangeInvalid("analytics_exchange_integer_encoding")
    return value


def decode_value(raw):
    value = json.loads(raw, object_pairs_hook=_unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ExchangeInvalid()))
    return logical_value(value)


def content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_DOMAIN + canonical_bytes(value)).hexdigest()


def question_definitions() -> dict[str, str]:
    return {item.question: item.definition_digest for item in (
        RegisteredQuestion("no_later_creator_reply.v1", "exchange.v1", no_later_creator_reply),
        RegisteredQuestion("pricing_discussions.v1", "exchange.v1", pricing_discussions),
    )}


def facts_digest(facts: ExchangeFacts) -> str:
    return content_digest(facts.model_dump(mode="json"))


def source_binding(bundle: ExchangeBundle) -> ExchangeSourceBinding:
    return ExchangeSourceBinding(account_ref=bundle.snapshot.account_ref,
        source_revision=bundle.snapshot.source_revision,
        canonical_content_digest=bundle.snapshot.canonical_content_digest,
        source_facts_digest=bundle.source_facts_digest)


def _scalars(value: object, depth=0) -> None:
    if depth > 32:
        raise ExchangeInvalid("analytics_exchange_depth")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExchangeInvalid()
    if isinstance(value, dict):
        if _INTEGER_TAG in value:
            raise ExchangeInvalid("analytics_exchange_scalar_tag")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExchangeInvalid()
            _scalars(item, depth + 1)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _scalars(item, depth + 1)


def validate_exchange(bundle: ExchangeBundle, *, expected: ExchangeSourceBinding | None = None,
                      now: datetime | None = None) -> ExchangeBundle:
    try:
        value = bundle.model_dump(mode="json")
        _scalars(value)
        bundle = ExchangeBundle.model_validate(value)
        artifact, snapshot, facts = bundle.artifact, bundle.snapshot, bundle.facts
        projection = artifact.projection
        nodes, edges = artifact.nodes, artifact.edges
        record_count = (1 + len(nodes) + len(edges) + len(facts.observations)
                        + len(facts.conversations) + len(projection.message_enrichments)
                        + len(projection.conversation_metrics))
        if record_count > MAX_EXCHANGE_RECORDS:
            raise ExchangeInvalid("analytics_exchange_record_limit")
        node_ids = [item.node_id for item in nodes]
        node_set = set(node_ids)
        edge_ids = [item.edge_id for item in edges]
        conversations = [item.conversation_ref for item in facts.conversations]
        messages = [item.message_ref for item in facts.observations]
        for keys in (node_ids, edge_ids, conversations, messages):
            if keys != sorted(set(keys)):
                raise ExchangeInvalid("analytics_exchange_identity_order")
        if (
            projection.account_ref != snapshot.account_ref
            or projection.graph.account_ref != snapshot.account_ref
            or projection.source_revision != snapshot.source_revision
            or projection.graph.source_revision != snapshot.source_revision
            or projection.projection_generation != snapshot.projection_generation
            or projection.canonical_content_digest != snapshot.canonical_content_digest
            or projection.projection_digest != snapshot.projection_digest
            or projection.pipeline_identity_digest != pipeline_identity_digest(projection)
            or projection.projection_digest != projection_digest(projection)
            or projection.graph_digest != projection_graph_digest(projection.pipeline_revision, nodes, edges)
            or projection.graph.node_count != len(nodes)
            or projection.graph.edge_count != len(edges)
            or projection.graph.node_counts_by_kind != dict(Counter(n.kind.value for n in nodes))
            or projection.graph.edge_counts_by_relation != dict(Counter(e.relation.value for e in edges))
            or any(n.account_ref != snapshot.account_ref for n in nodes)
            or any(e.account_ref != snapshot.account_ref or e.source_id not in node_set
                   or e.target_id not in node_set for e in edges)
            or snapshot.source_message_count != len(messages)
            or bundle.source_facts_digest != facts_digest(facts)
            or bundle.question_definitions != question_definitions()
        ):
            raise ExchangeInvalid("analytics_exchange_binding")
        for item in facts.observations:
            evidence = item.evidence
            if (evidence.account_ref != snapshot.account_ref
                    or evidence.source_revision != snapshot.source_revision
                    or evidence.conversation_ref not in conversations or evidence.span is not None):
                raise ExchangeInvalid("analytics_exchange_evidence")
        due = min((m.evidence.sent_at + timedelta(days=90) for m in facts.observations), default=None)
        if due != snapshot.retention_due_at:
            raise ExchangeInvalid("analytics_exchange_retention")
        if expected is not None and source_binding(bundle) != expected:
            raise ExchangeInvalid("analytics_exchange_source_changed")
        if now is not None:
            now = utc_instant(now)
            if snapshot.derived_at > now or (due is not None and due <= now):
                raise ExchangeInvalid("analytics_exchange_expired")
        unsigned = bundle.model_dump(mode="json", exclude={"content_digest"})
        if bundle.content_digest != content_digest(unsigned):
            raise ExchangeInvalid("analytics_exchange_digest")
        return bundle
    except ExchangeInvalid:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ExchangeInvalid() from None


def seal_exchange(**fields) -> ExchangeBundle:
    provisional = ExchangeBundle(content_digest="sha256:" + "0" * 64, **fields)
    digest = content_digest(provisional.model_dump(mode="json", exclude={"content_digest"}))
    return validate_exchange(provisional.model_copy(update={"content_digest": digest}))


def encode_exchange(bundle: ExchangeBundle) -> bytes:
    encoded = canonical_bytes(validate_exchange(bundle).model_dump(mode="json"))
    if len(encoded) > MAX_EXCHANGE_BYTES:
        raise ExchangeInvalid("analytics_exchange_byte_limit")
    return encoded


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExchangeInvalid("analytics_exchange_duplicate_key")
        result[key] = value
    return result


def decode_exchange(raw: bytes, *, expected=None, now=None) -> ExchangeBundle:
    if not isinstance(raw, bytes) or len(raw) > MAX_EXCHANGE_BYTES:
        raise ExchangeInvalid("analytics_exchange_byte_limit")
    try:
        value = decode_value(raw)
        _scalars(value)
        return validate_exchange(ExchangeBundle.model_validate(value), expected=expected, now=now)
    except ExchangeInvalid:
        raise
    except (ValueError, TypeError, RecursionError):
        raise ExchangeInvalid() from None
