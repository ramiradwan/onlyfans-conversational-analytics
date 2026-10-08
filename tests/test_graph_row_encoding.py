"""Compare streamed storage encoding with independent public graph models."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app.analytics.graph_row_encoding import node_bytes, edge_bytes
from app.analytics.sqlite_graph_store import _node, _edge
from app.analytics.graph_schema import NODE_PROPERTY_RULES, EDGE_PROPERTY_RULES
from app.models.analytics import GraphNodeKind, GraphRelation

pytestmark = [pytest.mark.ci_tier('integration')]

ACCOUNT = "a1:" + "1" * 64
NODE = "g1:" + "2" * 64
EDGE = "e1:" + "3" * 64
TARGET = "g1:" + "4" * 64


def node_row(kind="message", properties=None, at=None):
    return {"creator_account_id": ACCOUNT, "node_id": NODE, "kind": kind,
            "occurred_at": at, "properties_json": json.dumps(properties or {})}


def edge_row(relation="precedes", properties=None, at=None, sequence=None):
    return {"creator_account_id": ACCOUNT, "edge_id": EDGE, "source_id": NODE,
            "target_id": TARGET, "relation": relation, "occurred_at": at,
            "sequence": sequence, "properties_json": json.dumps(properties or {})}


NODE_CASES = [(kind.value, {}) for kind in GraphNodeKind] + [
    ("participant", {"role": role}) for role in ("creator", "counterpart")
] + [
    ("message", {"direction": direction, "character_count": 42, "source_ordinal": 0})
    for direction in ("inbound", "outbound")
] + [
    ("conversation", {"message_count": 3, "turn_count": 2,
        "average_sentiment_score": score, "response_coverage": 0.5})
    for score in (None, -1, -0.0, 0.0, 1.0)
] + [
    ("affect_state", {"label": "neutral", "score": score, "confidence": None})
    for score in (None, -0.0, 0.0, 0.25)
] + [
    ("topic", {"taxonomy_id": name, "label": name.capitalize()})
    for name in ("feedback", "greeting", "media", "pricing", "scheduling", "support")
] + [
    ("entity", {"entity_type": kind, "entity_ref": "x1:" + "5" * 64})
    for kind in ("amount", "hashtag", "mention", "url")
] + [("engagement_state", {"state": "information", "confidence": 1})]
EDGE_CASES = [(kind.value, {}) for kind in GraphRelation] + [
    ("precedes", {"scope": scope, "interval_seconds": value})
    for scope in ("message", "conversation") for value in (None, 0, -0.0, 0.001, 1e100)
] + [("mentions_topic", {"confidence": 0.5}), ("mentions_entity", {"confidence": None}),
     ("participates_in", {"role": "counterpart"})]

TIMES = [None, "2026-09-19T12:30:00.000000Z", "2026-09-19T12:30:00.123456Z",
         "2026-09-19T15:30:00.120000+03:00", "0001-01-01T00:00:00+00:00"]


def public_bytes(record):
    return json.dumps(record.model_dump(mode="json"), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("at", TIMES)
@pytest.mark.parametrize("kind,properties", NODE_CASES)
def test_node_bytes_match_public_models(kind, properties, at):
    row = node_row(kind, properties, at)
    original = deepcopy(row)
    assert node_bytes(row, ACCOUNT) == (kind, public_bytes(_node(row)))
    assert row == original


@pytest.mark.parametrize("at", TIMES)
@pytest.mark.parametrize("relation,properties", EDGE_CASES)
def test_edge_bytes_match_public_models(relation, properties, at):
    row = edge_row(relation, properties, at, 2**40)
    assert edge_bytes(row, ACCOUNT) == (relation, public_bytes(_edge(row)))


def test_shared_rule_tables_cover_every_public_kind():
    assert set(NODE_PROPERTY_RULES) == {kind.value for kind in GraphNodeKind}
    assert set(EDGE_PROPERTY_RULES) == {kind.value for kind in GraphRelation}


@pytest.mark.parametrize("encode,make", [(node_bytes, node_row), (edge_bytes, edge_row)])
@pytest.mark.parametrize("field,value", [
    ("creator_account_id", "not-an-account"), ("creator_account_id", "a1:" + "9" * 64),
    ("occurred_at", "2026-09-19T12:00:00"), ("occurred_at", "not-a-time"),
    ("properties_json", "[]"), ("properties_json", "null"),
    ("properties_json", '{"not_allowed": "synthetic"}'),
    ("properties_json", '{"nested": {"value": 1}}'),
])
def test_invalid_or_cross_account_rows_are_rejected(encode, make, field, value):
    row = make(); row[field] = value
    with pytest.raises(ValueError):
        encode(row, ACCOUNT)


@pytest.mark.parametrize("field,value", [
    ("edge_id", NODE), ("source_id", EDGE), ("target_id", "raw-message"),
    ("relation", "unknown"), ("sequence", -1), ("sequence", 1.5),
    ("sequence", True), ("sequence", "2"),
])
def test_invalid_edge_columns_are_rejected(field, value):
    row = edge_row(); row[field] = value
    with pytest.raises(ValueError):
        edge_bytes(row, ACCOUNT)


@pytest.mark.parametrize("kind,properties", [
    ("topic", {"taxonomy_id": "pricing", "label": "Greeting"}),
    ("message", {"character_count": True}), ("message", {"source_ordinal": -1}),
    ("message", {"direction": "unknown"}), ("affect_state", {"score": 2}),
    ("affect_state", {"score": float("nan")}),
    ("affect_state", {"confidence": float("inf")}),
    ("participant", {"role": ["creator"]}),
])
def test_property_failures_agree_with_public_models(kind, properties):
    row = node_row(kind, properties)
    for operation in (lambda: _node(row), lambda: node_bytes(row, ACCOUNT)):
        with pytest.raises(ValueError):
            operation()


def test_node_identity_and_kind_are_validated():
    for field, value in (("node_id", EDGE), ("kind", "unknown")):
        row = node_row(); row[field] = value
        with pytest.raises(ValueError):
            node_bytes(row, ACCOUNT)


@pytest.mark.parametrize("shared", [True, False])
def test_verification_matches_materialized_graph_without_model_allocation(tmp_path, monkeypatch, shared):
    from app.analytics.graph_verification import verify_graph_rows
    from app.analytics import sqlite_graph_store
    from tests.continuous_analytics_fixture import ACCOUNT as SOURCE, make_fixture, cleanup
    fixture = make_fixture(tmp_path)
    fixture.stores.projections.reuse_graph_content = shared
    try:
        expected = fixture.pipeline.project_account(SOURCE).artifact
        generation = fixture.stores.database.active_generation(SOURCE).generation_id
        def forbidden(*args, **kwargs):
            raise AssertionError("verification constructed a disposable graph model")
        monkeypatch.setattr(sqlite_graph_store, "_node", forbidden)
        monkeypatch.setattr(sqlite_graph_store, "_edge", forbidden)
        with fixture.stores.database.read() as db:
            result = verify_graph_rows(db, generation, expected.projection.account_ref)
        from app.analytics.graph_privacy import graph_content_digest
        assert result.digest == graph_content_digest(expected.nodes, expected.edges)
        assert result.segment_root == expected.projection.graph_digest
        assert sum(result.node_counts.values()) == len(expected.nodes)
        assert sum(result.edge_counts.values()) == len(expected.edges)
        assert result.nodes == result.edges == []
    finally:
        cleanup(fixture)


def _raw_row(kind, properties, at=None):
    row = node_row(at=at) if kind == "node" else edge_row(at=at, sequence=2**40)
    row["properties_json"] = properties
    return row


def _independent_public_bytes(kind, row):
    return public_bytes((_node if kind == "node" else _edge)(row))


RAW_JSON_CASES = [
    ("node", '{"direction":"outbound","direction":"inbound","source_ordinal":0}'),
    ("node", r'{"direc\u0074ion":"outbound","direction":"inbound"}'),
    ("node", '{"source_ordinal":-0,"character_count":9007199254740993,"direction":"inbound"}'),
    ("node", '{"source_ordinal":true,"source_ordinal":7}'),
    ("edge", '{"scope":"message","interval_seconds":-0.0}'),
    ("edge", '{"interval_seconds":1e-09,"scope":"conversation"}'),
    ("edge", '{"interval_seconds":1e100,"scope":"message"}'),
    ("edge", '{"interval_seconds":NaN,"interval_seconds":null}'),
]


@pytest.mark.parametrize("at", [None, "2026-01-01T00:15:00+00:30",
                                "9999-12-31T22:59:59.999999+01:00"])
@pytest.mark.parametrize("kind,properties", RAW_JSON_CASES)
def test_raw_json_spelling_keeps_independent_public_model_bytes(kind, properties, at):
    row = _raw_row(kind, properties, at)
    expected = _independent_public_bytes(kind, row)
    encode = node_bytes if kind == "node" else edge_bytes
    assert encode(row, ACCOUNT)[1] == expected


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_rejected_row_does_not_affect_next_canonical_encoding(kind):
    encode = node_bytes if kind == "node" else edge_bytes
    property_name = "character_count" if kind == "node" else "interval_seconds"
    valid = _raw_row(kind, json.dumps({property_name: 0}))
    expected = _independent_public_bytes(kind, valid)
    assert encode(valid, ACCOUNT)[1] == expected
    with pytest.raises(ValueError, match="^graph_property_invalid$"):
        encode(_raw_row(kind, json.dumps({property_name: []})), ACCOUNT)
    assert encode(valid, ACCOUNT)[1] == expected


def test_concurrent_encoding_keeps_independent_bytes_for_every_call():
    rows = [(kind, _raw_row(kind, properties)) for kind, properties in RAW_JSON_CASES]
    expected = [_independent_public_bytes(kind, row) for kind, row in rows]

    def checked(index):
        kind, row = rows[index % len(rows)]
        encode = node_bytes if kind == "node" else edge_bytes
        return encode(row, ACCOUNT)[1] == expected[index % len(rows)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(checked, range(128)))
