"""Validate stored graph columns without constructing disposable graph models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Protocol

from app.analytics.graph_identity import require_graph_id
from app.analytics.graph_schema import validate_node_properties, validate_edge_properties
from app.analytics.opaque_refs import require_opaque_ref


@dataclass(frozen=True, slots=True)
class EncodedGraphRecord:
    """Canonical bytes and columns checked by the storage row validators."""

    account_ref: str
    kind: str
    key: str
    category: str
    data: str
    source_id: str | None = None
    target_id: str | None = None
    conversation_edge: bool = False


class StoredRow(Protocol):
    def __getitem__(self, key: str) -> Any: ...


def _occurred_at(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("graph_timestamp_invalid")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("graph_timestamp_timezone_required")
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _base(row: StoredRow, account: str) -> dict[str, Any]:
    stored_account = require_opaque_ref(row["creator_account_id"], "account")
    if stored_account != account:
        raise ValueError("graph_account_mismatch")
    properties = json.loads(row["properties_json"])
    if not isinstance(properties, dict):
        raise ValueError("graph_properties_invalid")
    return {"account_ref": stored_account,
            "occurred_at": _occurred_at(row["occurred_at"]), "properties": properties}


def _encode(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _node_record(row: StoredRow, account: str) -> dict[str, Any]:
    record = _base(row, account)
    kind = row["kind"]
    record["properties"] = validate_node_properties(kind, record["properties"])
    record["node_id"] = require_graph_id(row["node_id"], expected_kind=kind)
    record["kind"] = kind
    return record


def _edge_record(row: StoredRow, account: str) -> dict[str, Any]:
    record = _base(row, account)
    relation = row["relation"]
    record["properties"] = validate_edge_properties(relation, record["properties"])
    record["edge_id"] = require_graph_id(row["edge_id"], expected_kind="edge")
    record["source_id"] = require_graph_id(row["source_id"])
    record["target_id"] = require_graph_id(row["target_id"])
    sequence = row["sequence"]
    if sequence is not None and (type(sequence) is not int or sequence < 0):
        raise ValueError("graph_sequence_invalid")
    record["relation"], record["sequence"] = relation, sequence
    return record


def node_bytes(row: StoredRow, account: str) -> tuple[str, bytes]:
    record = _node_record(row, account)
    return record["kind"], _encode(record)


def edge_bytes(row: StoredRow, account: str) -> tuple[str, bytes]:
    record = _edge_record(row, account)
    return record["relation"], _encode(record)


def node_record_bytes(row: StoredRow, account: str) -> tuple[dict[str, Any], bytes]:
    """Return the checked record and the exact bytes used for its content hash."""

    record = _node_record(row, account)
    return record, _encode(record)


def edge_record_bytes(row: StoredRow, account: str) -> tuple[dict[str, Any], bytes]:
    """Return the checked record and the exact bytes used for its content hash."""

    record = _edge_record(row, account)
    return record, _encode(record)
