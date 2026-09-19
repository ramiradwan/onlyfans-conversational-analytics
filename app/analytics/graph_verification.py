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


def verify_graph_rows(connection, generation_id: str, account_id: str, *,
                      materialize: bool = False,
                      check: Callable[[], None] = lambda: None) -> VerifiedGraph:
    from app.analytics.sqlite_graph_store import _node, _edge

    from app.analytics.shared_graph import supported, uses_segments, ordered_rows
    from app.analytics.graph_store import GraphReferentialIntegrityError

    shared = supported(connection) and uses_segments(connection, generation_id, account_id)
    if shared:
        for table in ('graph_owned_nodes', 'graph_owned_edges'):
            if connection.execute(f'SELECT 1 FROM {table} WHERE generation_id=? AND creator_account_id=? LIMIT 1', (generation_id, account_id)).fetchone():
                raise GraphReferentialIntegrityError('graph_generation_layout_mixed')
    digest = hashlib.sha256(b'{"edges":[')
    nodes, edges = [], []
    node_counts, edge_counts = Counter(), Counter()
    for table, key, decode, output, counts, kind in (
        ("graph_edges", "edge_id", _edge, edges, edge_counts, "relation"),
        ("graph_nodes", "node_id", _node, nodes, node_counts, "kind"),
    ):
        check()
        if table == "graph_nodes":
            digest.update(b'],"nodes":[')
        rows = (ordered_rows(connection, generation_id, account_id, 'node' if table == 'graph_nodes' else 'edge')
            if shared else connection.execute(
                f"SELECT * FROM {table} WHERE generation_id=? AND creator_account_id=? ORDER BY {key}",
                (generation_id, account_id)))
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
                if index:
                    digest.update(b",")
                digest.update(encoded)
        finally:
            rows.close()
    check()
    digest.update(b"]}")
    return VerifiedGraph("sha256:" + digest.hexdigest(), node_counts, edge_counts, nodes, edges)
