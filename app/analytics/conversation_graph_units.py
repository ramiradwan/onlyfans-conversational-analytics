"""Bounded immutable units for incremental conversation-graph reuse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import zlib

from app.analytics.graph_identity import require_graph_id
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS


MAX_GRAPH_UNIT_BYTES = 64 * 1024 * 1024
MAX_GRAPH_UNIT_RECORDS = 4_000_000


@dataclass(frozen=True, slots=True)
class ConversationGraphUnitHeader:
    account_ref: str
    conversation_ref: str
    input_digest: str
    config_digest: str
    retention_cutoff: datetime
    expires_at: datetime
    participant_ref: str
    started_at: datetime | None
    ended_at: datetime | None
    graph_digest: str
    node_count: int
    edge_count: int
    unit_id: str


@dataclass(frozen=True, slots=True)
class ConversationGraphUnit:
    header: ConversationGraphUnitHeader
    node_ids: bytes
    edge_ids: bytes

    @property
    def retained_bytes(self) -> int:
        return len(self.node_ids) + len(self.edge_ids)


@dataclass(frozen=True, slots=True)
class ConversationGraphReference:
    generation_id: str
    header: ConversationGraphUnitHeader


@dataclass(frozen=True, slots=True)
class ConversationGraphProof:
    generation_id: str
    binding: str
    stamp_prefix: tuple
    headers: tuple[ConversationGraphUnitHeader, ...]


def _encode_ids(ids) -> bytes:
    return json.dumps(
        list(ids), ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _compress(raw: bytes) -> bytes:
    return zlib.compress(raw, 1)


def _unpack(data: bytes, *, expected_count: int, kind: str) -> tuple[str, ...]:
    if (
        not isinstance(data, bytes)
        or not data
        or len(data) > MAX_GRAPH_UNIT_BYTES
        or expected_count < 0
        or expected_count > MAX_GRAPH_UNIT_RECORDS
    ):
        raise ValueError("conversation_graph_unit_size_invalid")
    maximum = min(
        MAX_GRAPH_UNIT_BYTES * 4,
        max(2, expected_count * 72 + 2),
    )
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(data, maximum + 1)
    except zlib.error as error:
        raise ValueError("conversation_graph_unit_encoding_invalid") from error
    if (
        len(raw) > maximum
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("conversation_graph_unit_expansion_invalid")
    try:
        values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("conversation_graph_unit_encoding_invalid") from error
    if (
        not isinstance(values, list)
        or len(values) != expected_count
        or values != sorted(values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("conversation_graph_unit_membership_invalid")
    expected_kind = "edge" if kind == "edge" else None
    for value in values:
        require_graph_id(value, expected_kind=expected_kind)
    return tuple(values)


def unit_id(
    graph_digest: str,
    node_ids: tuple[str, ...],
    edge_ids: tuple[str, ...],
) -> str:
    digest = hashlib.sha256(b"conversation-graph-unit.v1\0")
    digest.update(graph_digest.encode("ascii") + b"\0")
    for kind, values in ((b"node", node_ids), (b"edge", edge_ids)):
        digest.update(kind + b"\0")
        for value in values:
            digest.update(value.encode("ascii") + b"\n")
    return digest.hexdigest()


def create_graph_unit(
    *,
    account_ref: str,
    conversation_ref: str,
    input_digest: str,
    config_digest: str,
    cutoff: datetime,
    findings,
    metrics,
    graph,
    graph_digest: str | None = None,
) -> ConversationGraphUnit | None:
    nodes = tuple(sorted(graph.nodes))
    edges = tuple(sorted(graph.edges))
    if (
        not nodes
        or len(nodes) > MAX_GRAPH_UNIT_RECORDS
        or len(edges) > MAX_GRAPH_UNIT_RECORDS
    ):
        return None
    digest = graph_digest or graph.digest(check=lambda: None)
    node_data = _compress(_encode_ids(nodes))
    edge_data = _compress(_encode_ids(edges))
    if len(node_data) > MAX_GRAPH_UNIT_BYTES or len(edge_data) > MAX_GRAPH_UNIT_BYTES:
        return None
    header = ConversationGraphUnitHeader(
        account_ref=account_ref,
        conversation_ref=conversation_ref,
        input_digest=input_digest,
        config_digest=config_digest,
        retention_cutoff=cutoff,
        expires_at=min(item.sent_at for item in findings)
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        participant_ref=metrics.participant_ref,
        started_at=metrics.started_at,
        ended_at=metrics.ended_at,
        graph_digest=digest,
        node_count=len(nodes),
        edge_count=len(edges),
        unit_id=unit_id(digest, nodes, edges),
    )
    return ConversationGraphUnit(header, node_data, edge_data)


def graph_unit_ids(unit: ConversationGraphUnit) -> tuple[tuple[str, ...], tuple[str, ...]]:
    header = unit.header
    nodes = _unpack(unit.node_ids, expected_count=header.node_count, kind="node")
    edges = _unpack(unit.edge_ids, expected_count=header.edge_count, kind="edge")
    if unit_id(header.graph_digest, nodes, edges) != header.unit_id:
        raise ValueError("conversation_graph_unit_digest_invalid")
    return nodes, edges
