"""Preserve typed values and reject altered native graph transport records."""

from copy import deepcopy
import json

import pytest

from app.analytics.exchange_codec import (
    ExchangeInvalid, canonical_bytes, decode_value, decode_exchange, encode_exchange,
)
from tests.test_analytics_exchange import make_bundle
from tools.analytics_exchange_fixtures import load_cases
from tools.analytics_exchange_records import exchange_records, recover_exchange

pytestmark = [pytest.mark.ci_tier("fast")]


@pytest.mark.parametrize("value", [2**53, -(2**63), 2**127, 10**100])
def test_decimal_integer_tag_is_lossless(value):
    original = {"integer": value, "float": 1.0, "null": None, "empty": {}}
    raw = canonical_bytes(original)
    assert b"$analytics_integer" in raw
    decoded = decode_value(raw)
    assert decoded == original
    assert type(decoded["integer"]) is int and type(decoded["float"]) is float
    assert "absent" not in decoded and decoded["null"] is None


@pytest.mark.parametrize("tag", ["01", "+9007199254740992", "-0", "1", 9007199254740992,
                               "9.007199254740992e15", "1" * 1025])
def test_noncanonical_integer_tags_are_rejected(tag):
    with pytest.raises(ExchangeInvalid):
        decode_value(json.dumps({"$analytics_integer": tag}).encode())


def test_integer_tags_cannot_collide_with_logical_properties():
    with pytest.raises(ExchangeInvalid):
        canonical_bytes({"$analytics_integer": "9007199254740992"})
    with pytest.raises(ExchangeInvalid):
        decode_value(b'{"$analytics_integer":"9007199254740992","other":true}')
    with pytest.raises(ExchangeInvalid):
        decode_value(b'{"value":9007199254740992}')


def test_wide_source_order_survives_exchange_and_native_graph_transport():
    case = deepcopy(load_cases("no-later-reply")[0])
    case["messages"][0]["order"] = 2**100
    _, bundle = make_bundle(case)
    raw = encode_exchange(bundle)
    assert b'"$analytics_integer":"1267650600228229401496703205376"' in raw
    assert decode_exchange(raw) == bundle
    assert recover_exchange(exchange_records(bundle)) == bundle


@pytest.mark.parametrize("case", load_cases("no-later-reply") + load_cases("pricing-discussions"),
                         ids=lambda case: case["id"])
def test_native_graph_transport_restores_complete_artifact(case):
    _, bundle = make_bundle(case)
    records = exchange_records(bundle)
    assert recover_exchange(records) == bundle
    assert all("source_id" in item for item in records if item["logical_id"].startswith("edge:"))


@pytest.mark.parametrize("mutation", ["payload", "index", "endpoint", "duplicate", "missing"])
def test_transport_rejects_altered_identity_payload_or_membership(mutation):
    _, bundle = make_bundle()
    records = exchange_records(bundle)
    if mutation == "payload":
        records[0]["record_json"] = "{}"
    elif mutation == "index":
        records[0]["fields"]["record_index"] += 1
    elif mutation == "endpoint":
        next(item for item in records if "source_id" in item)["source_id"] = "node:g1:" + "0" * 64
    elif mutation == "duplicate":
        records.append(records[0])
    else:
        records.pop()
    with pytest.raises(ExchangeInvalid):
        recover_exchange(records)
