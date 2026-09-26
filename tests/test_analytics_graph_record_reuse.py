"""Checked graph rows retain their canonical bytes without a JSON round trip."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from unittest.mock import Mock

import pytest

from app.analytics import graph_row_encoding as encoding
from app.analytics.conversation_graph_sql import PAGE_RECORDS, graph_records


ACCOUNT = "a1:" + "1" * 64
OTHER_ACCOUNT = "a1:" + "2" * 64
NODE = "g1:" + "3" * 64
OTHER_NODE = "g1:" + "4" * 64
EDGE = "e1:" + "5" * 64


def _row(kind="node", /, **changes):
    row = {
        "creator_account_id": ACCOUNT,
        "occurred_at": "2026-09-20T03:00:00+03:00",
        "properties_json": '{"source_ordinal":7,"direction":"inbound","character_count":9}',
        "node_id": NODE,
        "kind": "message",
    } if kind == "node" else {
        "creator_account_id": ACCOUNT,
        "occurred_at": None,
        "properties_json": '{"scope":"message","interval_seconds":0.0}',
        "edge_id": EDGE,
        "relation": "precedes",
        "source_id": NODE,
        "target_id": OTHER_NODE,
        "sequence": 0,
    }
    return {**row, **changes}


def _expected(kind="node", /, **changes):
    record = {
        "account_ref": ACCOUNT,
        "occurred_at": "2026-09-20T00:00:00Z",
        "properties": {"character_count": 9, "direction": "inbound", "source_ordinal": 7},
        "node_id": NODE,
        "kind": "message",
    } if kind == "node" else {
        "account_ref": ACCOUNT,
        "occurred_at": None,
        "properties": {"interval_seconds": 0.0, "scope": "message"},
        "edge_id": EDGE,
        "relation": "precedes",
        "source_id": NODE,
        "target_id": OTHER_NODE,
        "sequence": 0,
    }
    return {**record, **changes}


def _canonical(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _encoder(kind):
    return encoding.node_record_bytes if kind == "node" else encoding.edge_record_bytes


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_record_and_legacy_bytes_match_independent_expected_fields(kind):
    row = _row(kind)
    before = dict(row)
    record, data = _encoder(kind)(row, ACCOUNT)
    expected = _expected(kind)
    assert record == expected
    assert data == _canonical(expected)
    legacy = encoding.node_bytes if kind == "node" else encoding.edge_bytes
    assert legacy(row, ACCOUNT) == (expected["kind" if kind == "node" else "relation"], data)
    assert row == before


@pytest.mark.parametrize("kind", ["node", "edge"])
@pytest.mark.parametrize("instant", [None, "2026-09-20T00:00:00Z", "2026-09-20T01:30:00+01:30"])
def test_missing_and_offset_timestamps_have_the_same_canonical_meaning(kind, instant):
    expected = _expected(kind, occurred_at=None if instant is None else "2026-09-20T00:00:00Z")
    record, data = _encoder(kind)(_row(kind, occurred_at=instant), ACCOUNT)
    assert record == expected
    assert data == _canonical(expected)


@pytest.mark.parametrize("kind", ["node", "edge"])
@pytest.mark.parametrize("changes,error", [
    ({"creator_account_id": OTHER_ACCOUNT}, "graph_account_mismatch"),
    ({"creator_account_id": "not-an-account"}, "analytics_ref_invalid"),
    ({"occurred_at": "2026-09-20T00:00:00"}, "graph_timestamp_timezone_required"),
    ({"occurred_at": 123}, "graph_timestamp_invalid"),
    ({"properties_json": "[]"}, "graph_properties_invalid"),
    ({"properties_json": "null"}, "graph_properties_invalid"),
    ({"properties_json": '{"unexpected":1}'}, "graph_property_unknown"),
])
def test_record_encoders_preserve_common_validation(kind, changes, error):
    with pytest.raises(ValueError, match=error):
        _encoder(kind)(_row(kind, **changes), ACCOUNT)


@pytest.mark.parametrize("kind,changes,error", [
    ("node", {"node_id": EDGE}, "graph_id_invalid"),
    ("node", {"kind": "unknown"}, "graph_kind_invalid"),
    ("node", {"properties_json": '{"source_ordinal":true}'}, "graph_property_invalid"),
    ("node", {"properties_json": '{"source_ordinal":[]}'}, "graph_property_invalid"),
    ("edge", {"edge_id": NODE}, "graph_id_invalid"),
    ("edge", {"source_id": EDGE}, "graph_id_invalid"),
    ("edge", {"target_id": EDGE}, "graph_id_invalid"),
    ("edge", {"relation": "unknown"}, "graph_relation_invalid"),
    ("edge", {"sequence": True}, "graph_sequence_invalid"),
    ("edge", {"sequence": -1}, "graph_sequence_invalid"),
    ("edge", {"sequence": 0.5}, "graph_sequence_invalid"),
    ("edge", {"properties_json": '{"interval_seconds":NaN}'}, "graph_property_invalid"),
    ("edge", {"properties_json": '{"interval_seconds":Infinity}'}, "graph_property_invalid"),
])
def test_record_encoders_preserve_kind_specific_validation(kind, changes, error):
    with pytest.raises(ValueError, match=error):
        _encoder(kind)(_row(kind, **changes), ACCOUNT)


@pytest.fixture
def connection():
    # Minimal relational fixture for the real SELECT, not a production-store fixture.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE generation_graph_segments (
        creator_account_id TEXT, generation_id TEXT, kind TEXT, bucket TEXT, segment_id TEXT)""")
    for kind, columns in (("node", "kind TEXT"), ("edge", "relation TEXT, source_id TEXT, target_id TEXT, sequence INTEGER")):
        db.execute(f"""CREATE TABLE graph_{kind}_content (
            creator_account_id TEXT, content_id TEXT, {kind}_id TEXT,
            occurred_at TEXT, properties_json TEXT, {columns},
            PRIMARY KEY (creator_account_id, content_id, {kind}_id))""")
        db.execute(f"""CREATE TABLE graph_segment_{kind}s (
            creator_account_id TEXT, segment_id TEXT, content_id TEXT, {kind}_id TEXT,
            PRIMARY KEY (creator_account_id, segment_id, {kind}_id))""")
    yield db
    db.close()


def _insert(db, kind="node", *, generation="active", segment="segment", row=None, expected=None):
    row = _row(kind) if row is None else row
    expected = _expected(kind) if expected is None else expected
    stored = {**row, "content_id": hashlib.sha256(_canonical(expected)).hexdigest()}
    fields = ",".join(stored)
    db.execute(f"INSERT INTO graph_{kind}_content ({fields}) VALUES ({','.join('?' for _ in stored)})", tuple(stored.values()))
    key, account = row[kind + "_id"], row["creator_account_id"]
    db.execute(f"INSERT INTO graph_segment_{kind}s VALUES (?,?,?,?)", (account, segment, stored["content_id"], key))
    db.execute("INSERT INTO generation_graph_segments VALUES (?,?,?,?,?)", (account, generation, kind, key[3:5], segment))


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_decodes_only_properties_once_per_distinct_row(connection, monkeypatch, kind):
    _insert(connection, kind)
    loads = Mock(wraps=json.loads)
    monkeypatch.setattr(encoding.json, "loads", loads)
    key = NODE if kind == "node" else EDGE
    result = graph_records(connection, "active", ACCOUNT, kind, [key, key], lambda: None)
    assert result == [_expected(kind), _expected(kind)]
    loads.assert_called_once_with(_row(kind)["properties_json"])


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_keeps_account_and_generation_selection(connection, kind):
    _insert(connection, kind)
    _insert(connection, kind, generation="old", segment="old-segment",
            row=_row(kind, occurred_at="2026-09-19T00:00:00Z"),
            expected=_expected(kind, occurred_at="2026-09-19T00:00:00Z"))
    _insert(connection, kind, row=_row(kind, creator_account_id=OTHER_ACCOUNT),
            expected=_expected(kind, account_ref=OTHER_ACCOUNT))
    key = NODE if kind == "node" else EDGE
    assert graph_records(connection, "active", ACCOUNT, kind, [key], lambda: None) == [_expected(kind)]
    with pytest.raises(ValueError, match="conversation_page_graph_reference_absent"):
        graph_records(connection, "absent", ACCOUNT, kind, [key], lambda: None)


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_rejects_changed_content(connection, kind):
    _insert(connection, kind)
    connection.execute(f"UPDATE graph_{kind}_content SET occurred_at='2026-09-19T00:00:00Z'")
    key = NODE if kind == "node" else EDGE
    with pytest.raises(ValueError, match="conversation_page_graph_content_invalid"):
        graph_records(connection, "active", ACCOUNT, kind, [key], lambda: None)


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_rejects_ambiguous_generation_membership(connection, kind):
    _insert(connection, kind)
    connection.execute("INSERT INTO generation_graph_segments SELECT * FROM generation_graph_segments")
    key = NODE if kind == "node" else EDGE
    with pytest.raises(ValueError, match="conversation_page_graph_content_invalid"):
        graph_records(connection, "active", ACCOUNT, kind, [key], lambda: None)


@pytest.mark.parametrize("kind,keys", [("invalid", [NODE]), ("node", []), ("node", [NODE] * (PAGE_RECORDS + 1))])
def test_sql_reader_keeps_request_bounds(kind, keys):
    db = Mock()
    with pytest.raises(ValueError, match="conversation_page_graph_lookup_invalid"):
        graph_records(db, "active", ACCOUNT, kind, keys, lambda: None)
    db.execute.assert_not_called()


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_closes_cursor_when_cancelled_between_rows(kind):
    cursor = Mock()
    cursor.__iter__ = Mock(return_value=iter([_row(kind)]))
    db = Mock()
    db.execute.return_value = cursor
    check = Mock(side_effect=[None, RuntimeError("cancelled")])
    key = NODE if kind == "node" else EDGE
    with pytest.raises(RuntimeError, match="cancelled"):
        graph_records(db, "active", ACCOUNT, kind, [key], check)
    cursor.close.assert_called_once_with()


@pytest.mark.parametrize("kind,name,properties", [
    ("node", "participant", {"role": "creator"}),
    ("node", "conversation", {"message_count": 2, "turn_count": 1,
                               "average_sentiment_score": None, "response_coverage": 0.0}),
    ("node", "topic", {"taxonomy_id": "pricing", "label": "Pricing"}),
    ("node", "entity", {"entity_type": "amount", "entity_ref": "x1:" + "6" * 64}),
    ("node", "affect_state", {"label": "neutral", "score": -0.0, "confidence": 1.0}),
    ("node", "engagement_state", {"state": "inquiry", "confidence": None}),
    ("edge", "participates_in", {"role": "counterpart"}),
    ("edge", "contains", {}),
    ("edge", "sent", {}),
    ("edge", "received_by", {}),
    ("edge", "expresses_affect", {}),
    ("edge", "has_engagement_state", {}),
    ("edge", "mentions_topic", {"confidence": 1.0}),
    ("edge", "mentions_entity", {"confidence": None}),
])
def test_all_other_closed_graph_kinds_keep_exact_bytes(kind, name, properties):
    field = "kind" if kind == "node" else "relation"
    expected = _expected(kind, **{field: name, "properties": properties})
    row = _row(kind, **{field: name, "properties_json": json.dumps(properties)})
    record, data = _encoder(kind)(row, ACCOUNT)
    assert record == expected
    assert data == _canonical(expected)


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_record_payload_is_fresh_on_every_read(kind):
    record, before = _encoder(kind)(_row(kind), ACCOUNT)
    record["properties"].clear()
    record["account_ref"] = OTHER_ACCOUNT
    next_record, after = _encoder(kind)(_row(kind), ACCOUNT)
    assert next_record == _expected(kind)
    assert after == before == _canonical(_expected(kind))


def test_sql_reader_preserves_requested_order_and_full_page_bound(connection):
    _insert(connection)
    _insert(connection, segment="second", row=_row(node_id=OTHER_NODE),
            expected=_expected(node_id=OTHER_NODE))
    keys = [OTHER_NODE, NODE] * (PAGE_RECORDS // 2)
    assert graph_records(connection, "active", ACCOUNT, "node", keys, lambda: None) == [
        _expected(node_id=key) for key in keys]


def test_sql_reader_cancellation_before_lookup_does_not_execute_sql():
    db = Mock()
    with pytest.raises(RuntimeError, match="cancelled"):
        graph_records(db, "active", ACCOUNT, "node", [NODE],
                      Mock(side_effect=RuntimeError("cancelled")))
    db.execute.assert_not_called()


@pytest.mark.parametrize("kind", ["node", "edge"])
def test_sql_reader_closes_cursor_after_validation_failure(kind):
    cursor = Mock()
    cursor.__iter__ = Mock(return_value=iter([_row(kind, properties_json="[]")]))
    db = Mock()
    db.execute.return_value = cursor
    key = NODE if kind == "node" else EDGE
    with pytest.raises(ValueError, match="graph_properties_invalid"):
        graph_records(db, "active", ACCOUNT, kind, [key], lambda: None)
    cursor.close.assert_called_once_with()
