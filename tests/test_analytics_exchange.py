"""Check exchange identity and source authority independently of transport."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.analytics.exchange_codec import (
    ExchangeInvalid, canonical_bytes, decode_exchange, encode_exchange, source_binding,
)
from app.analytics.pipeline import AnalyticsPipeline
from tools.analytics_exchange_fixtures import SyntheticSource, load_cases
from tools.analytics_exchange_local import bundle_from_artifact

pytestmark = [pytest.mark.ci_tier("fast")]


def make_bundle(case=None):
    source = SyntheticSource(case or load_cases("no-later-reply")[0])
    artifact = AnalyticsPipeline(source, clock=lambda: source.now,
                                 compact_graph=False).rebuild_account(source.account).artifact
    return source, bundle_from_artifact(source, artifact)


@pytest.mark.parametrize("case", load_cases("no-later-reply") + load_cases("pricing-discussions"),
                         ids=lambda case: case["id"])
def test_exchange_preserves_versioned_graph_and_question_facts(case):
    source, bundle = make_bundle(case)
    raw = encode_exchange(bundle)
    decoded = decode_exchange(raw, expected=source.binding(), now=source.now)
    assert decoded == bundle
    assert encode_exchange(decoded) == raw
    assert "graph.segment-root.v1" in decoded.artifact.projection.pipeline_revision
    assert source_binding(decoded) == source.binding()
    assert all(m.evidence.span is None for m in decoded.facts.observations)


@pytest.mark.parametrize("field", ["source_revision", "canonical_content_digest", "source_facts_digest", "account_ref"])
def test_exchange_cannot_supply_its_own_authority(field):
    source, bundle = make_bundle()
    expected = source.binding()
    replacement = expected.source_revision + 1 if field == "source_revision" else "sha256:" + "1" * 64
    with pytest.raises(ExchangeInvalid, match="source_changed"):
        decode_exchange(encode_exchange(bundle), expected=expected.model_copy(update={field: replacement}), now=source.now)


@pytest.mark.parametrize("field,value", [("kind", "system"), ("role", "creator"), ("order", 7), ("version", 3)])
def test_source_fact_changes_invalidate_even_without_an_identity_change(field, value):
    source, bundle = make_bundle()
    source.case["messages"][0][field] = value
    with pytest.raises(ExchangeInvalid, match="source_changed"):
        decode_exchange(encode_exchange(bundle), expected=source.binding(), now=source.now)


def test_exchange_rejects_expiry_at_the_exact_retention_boundary():
    source, bundle = make_bundle()
    with pytest.raises(ExchangeInvalid, match="expired"):
        decode_exchange(encode_exchange(bundle), expected=source.binding(), now=bundle.snapshot.retention_due_at)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema_version="analytics-exchange.v2"),
    lambda value: value["facts"]["observations"].append(value["facts"]["observations"][0]),
    lambda value: value["artifact"]["edges"][0].update(target_id="g1:" + "0" * 64),
    lambda value: value["artifact"]["nodes"][0].update(account_ref="a1:" + "0" * 64),
    lambda value: value["facts"]["observations"][0].update(order=9007199254740992),
    lambda value: value["facts"]["observations"][0].update(kind="human"),
    lambda value: value["question_definitions"].clear(),
])
def test_exchange_rejects_malformed_or_rebound_records(mutate):
    _, bundle = make_bundle()
    value = json.loads(encode_exchange(bundle))
    mutate(value)
    with pytest.raises(ExchangeInvalid):
        decode_exchange(canonical_bytes(value))


def test_exchange_rejects_duplicate_json_keys_and_nonfinite_values():
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[' * 50 + b'0' + b']' * 50):
        with pytest.raises(ExchangeInvalid):
            decode_exchange(raw)


def test_encoding_revalidates_mutated_nested_models():
    _, bundle = make_bundle()
    bundle.artifact.nodes[0].properties["unexpected"] = "value"
    with pytest.raises(ExchangeInvalid):
        encode_exchange(bundle)


def test_missing_classification_and_source_order_stay_unknown():
    case = deepcopy(load_cases("no-later-reply")[0])
    message = case["messages"][0]
    message.update(kind="unknown", order=None, ordering="inferred", pricing="missing", finding_version=None, language=None)
    _, bundle = make_bundle(case)
    observation = decode_exchange(encode_exchange(bundle)).facts.observations[0]
    assert observation.kind == "unknown"
    assert observation.order is None and observation.ordering == "inferred"
    assert observation.classification.input_version_digest is None
    assert observation.classification.language is None
