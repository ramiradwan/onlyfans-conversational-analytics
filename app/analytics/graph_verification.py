"""Independently verify persisted graph rows without retaining every model."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from app.models.analytics import GraphNode, GraphEdge
from app.analytics.graph_row_encoding import node_bytes, edge_bytes


@dataclass(slots=True)
class VerifiedGraph:
    digest: str
    node_counts: Counter
    edge_counts: Counter
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    segments: tuple = ()


def verify_graph_rows(connection, generation_id: str, account_id: str, *,
                      materialize: bool = False,
                      check: Callable[[], None] = lambda: None) -> VerifiedGraph:
    from app.analytics.sqlite_graph_store import _node, _edge

    from app.analytics.shared_graph import (
        VerifiedSegment, ordered_rows, supported, uses_segments,
    )
    from app.analytics.graph_store import GraphReferentialIntegrityError

    shared = supported(connection) and uses_segments(connection, generation_id, account_id)
    if shared:
        for table in ('graph_owned_nodes', 'graph_owned_edges'):
            if connection.execute(f'SELECT 1 FROM {table} WHERE generation_id=? AND creator_account_id=? LIMIT 1', (generation_id, account_id)).fetchone():
                raise GraphReferentialIntegrityError('graph_generation_layout_mixed')
    digest = hashlib.sha256(b'{"edges":[')
    nodes, edges, segments = [], [], []
    node_counts, edge_counts = Counter(), Counter()
    segment_proof_valid = True
    for table, key, decode, output, counts, kind, storage_kind in (
        ("graph_edges", "edge_id", _edge, edges, edge_counts, "relation", "edge"),
        ("graph_nodes", "node_id", _node, nodes, node_counts, "kind", "node"),
    ):
        check()
        if table == "graph_nodes":
            digest.update(b'],"nodes":[')
        rows = (ordered_rows(connection, generation_id, account_id, storage_kind)
            if shared else connection.execute(
                f"SELECT * FROM {table} WHERE generation_id=? AND creator_account_id=? ORDER BY {key}",
                (generation_id, account_id)))
        current_segment = None
        segment_digest = None
        segment_count = 0
        segment_counts = Counter()

        def finish_segment() -> None:
            nonlocal current_segment, segment_digest, segment_count, segment_counts
            nonlocal segment_proof_valid
            if current_segment is None:
                return
            bucket, segment_id, expected_digest = current_segment
            if segment_digest.hexdigest() != expected_digest:
                segment_proof_valid = False
            elif segment_proof_valid:
                segments.append(VerifiedSegment(
                    storage_kind, bucket, segment_id, expected_digest, segment_count,
                    tuple(sorted(segment_counts.items())),
                ))
            current_segment = None
            segment_digest = None
            segment_count = 0
            segment_counts = Counter()

        try:
            for index, row in enumerate(rows):
                check()
                if materialize:
                    record = decode(row)
                    record_kind = getattr(record, kind).value
                    encoded = json.dumps(record.model_dump(mode="json"), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                    output.append(record)
                else:
                    encode = edge_bytes if table == "graph_edges" else node_bytes
                    record_kind, encoded = encode(row, account_id)
                counts[record_kind] += 1
                if shared:
                    version = hashlib.sha256(encoded).hexdigest()
                    if version != row["content_id"]:
                        segment_proof_valid = False
                    identity = (
                        row["segment_bucket"], row["segment_id"], row["segment_digest"]
                    )
                    if current_segment != identity:
                        finish_segment()
                        current_segment = identity
                        segment_digest = hashlib.sha256(
                            ("graph-segment.v1:" + storage_kind + ":" + identity[0]).encode()
                        )
                    segment_digest.update(
                        row[key].encode() + b":" + version.encode() + b"\n"
                    )
                    segment_counts[record_kind] += 1
                    segment_count += 1
                if index:
                    digest.update(b",")
                digest.update(encoded)
            finish_segment()
        finally:
            rows.close()
    check()
    digest.update(b"]}")
    return VerifiedGraph(
        "sha256:" + digest.hexdigest(), node_counts, edge_counts,
        nodes, edges, tuple(segments) if segment_proof_valid else (),
    )
