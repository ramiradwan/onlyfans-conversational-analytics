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
    checksum_version: int = 1


@dataclass(frozen=True, slots=True)
class ConversationGraphUnit:
    header: ConversationGraphUnitHeader
    node_ids: bytes
    edge_ids: bytes
    integrity_metadata: bytes | None = None

    @property
    def retained_bytes(self) -> int:
        return len(self.node_ids) + len(self.edge_ids) + len(self.integrity_metadata or b'')


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
    *, checksum_version: int = 1,
) -> str:
    if type(checksum_version) is not int or checksum_version not in (1, 2):
        raise ValueError("conversation_integrity_version_invalid")
    digest = hashlib.sha256(f"conversation-graph-unit.v{checksum_version}\0".encode())
    digest.update(graph_digest.encode("ascii") + b"\0")
    for kind, values in ((b"node", node_ids), (b"edge", edge_ids)):
        digest.update(kind + b"\0")
        from app.analytics.conversation_id_frames import IdGroups
        if isinstance(values, IdGroups):
            for block in values.lines():
                digest.update(block)
        else:
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
    checksum_version: int = 1,
    check=lambda: None,
) -> ConversationGraphUnit | None:
    nodes = tuple(sorted(graph.nodes))
    edges = tuple(sorted(graph.edges))
    if (
        not nodes
        or len(nodes) > MAX_GRAPH_UNIT_RECORDS
        or len(edges) > MAX_GRAPH_UNIT_RECORDS
    ):
        return None
    metadata = None
    if checksum_version == 2:
        from app.analytics.conversation_integrity import from_graph, IntegrityCapacity
        try:
            digest, metadata = from_graph(account_ref, conversation_ref, graph, check)
        except IntegrityCapacity:
            return None
    else:
        digest = graph_digest or graph.digest(check=lambda: None)
    return create_membership_unit(account_ref=account_ref, conversation_ref=conversation_ref,
        input_digest=input_digest, config_digest=config_digest, cutoff=cutoff,
        findings=findings, metrics=metrics, nodes=nodes, edges=edges, digest=digest,
        checksum_version=checksum_version, integrity_metadata=metadata)


def create_membership_unit(*, account_ref, conversation_ref, input_digest, config_digest,
                           cutoff, findings, metrics, nodes, edges, digest,
                           checksum_version=1, integrity_metadata=None):
    """Encode the existing unit after checking its canonical graph bytes."""
    if not nodes or max(len(nodes), len(edges)) > MAX_GRAPH_UNIT_RECORDS:
        return None
    from app.analytics.conversation_id_frames import IdGroups
    node_data = nodes.pack(MAX_GRAPH_UNIT_BYTES) if isinstance(nodes, IdGroups) else _compress(_encode_ids(nodes))
    edge_data = edges.pack(MAX_GRAPH_UNIT_BYTES) if isinstance(edges, IdGroups) else _compress(_encode_ids(edges))
    if (node_data is None or edge_data is None
            or len(node_data) > MAX_GRAPH_UNIT_BYTES or len(edge_data) > MAX_GRAPH_UNIT_BYTES):
        return None
    from app.analytics.conversation_enrichment_units import AppendedMessageEnrichments, InsertedMessageEnrichments
    first_source = (findings.first_source_at if isinstance(findings, (AppendedMessageEnrichments, InsertedMessageEnrichments))
                    else min(item.sent_at for item in findings))
    header = ConversationGraphUnitHeader(
        account_ref=account_ref,
        conversation_ref=conversation_ref,
        input_digest=input_digest,
        config_digest=config_digest,
        retention_cutoff=cutoff,
        expires_at=first_source
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        participant_ref=metrics.participant_ref,
        started_at=metrics.started_at,
        ended_at=metrics.ended_at,
        graph_digest=digest,
        node_count=len(nodes),
        edge_count=len(edges),
        unit_id=unit_id(digest, nodes, edges, checksum_version=checksum_version),
        checksum_version=checksum_version,
    )
    result = ConversationGraphUnit(header, node_data, edge_data, integrity_metadata)
    if checksum_version == 2:
        from app.analytics.conversation_integrity import decode_manifest
        decode_manifest(result)
    elif integrity_metadata is not None:
        raise ValueError("conversation_integrity_version_invalid")
    return result


def graph_unit_ids(unit: ConversationGraphUnit) -> tuple[tuple[str, ...], tuple[str, ...]]:
    header = unit.header
    if header.checksum_version == 2:
        from app.analytics.conversation_integrity import decode_manifest
        decode_manifest(unit)
    nodes = _unpack(unit.node_ids, expected_count=header.node_count, kind="node")
    edges = _unpack(unit.edge_ids, expected_count=header.edge_count, kind="edge")
    if unit_id(header.graph_digest, nodes, edges, checksum_version=header.checksum_version) != header.unit_id:
        raise ValueError("conversation_graph_unit_digest_invalid")
    return nodes, edges
