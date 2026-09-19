"""Independently verify persisted graph rows without retaining every model."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from app.models.analytics import GraphNode, GraphEdge


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
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE generation_id=? AND creator_account_id=? ORDER BY {key}",
            (generation_id, account_id),
        )
        try:
            for index, row in enumerate(rows):
                check()
                record = decode(row)
                counts[getattr(record, kind).value] += 1
                if index:
                    digest.update(b",")
                digest.update(json.dumps(record.model_dump(mode="json"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if materialize:
                    output.append(record)
        finally:
            rows.close()
    check()
    digest.update(b"]}")
    return VerifiedGraph("sha256:" + digest.hexdigest(), node_counts, edge_counts, nodes, edges)
