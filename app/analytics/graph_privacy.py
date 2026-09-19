"""Data-minimizing allowlist for persisted relationship-graph properties."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

from app.models.analytics import (
    GraphEdge,
    GraphNode,
)


def safe_graph_node(node: GraphNode) -> GraphNode:
    """Revalidate a node so model-copy callers cannot bypass the closed schema."""

    return GraphNode.model_validate(node.model_dump())


def safe_graph_edge(edge: GraphEdge) -> GraphEdge:
    """Revalidate an edge so model-copy callers cannot bypass the closed schema."""

    return GraphEdge.model_validate(edge.model_dump())


def safe_graph_records(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    check: Callable[[], None] | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    safe_nodes: list[GraphNode] = []
    safe_edges: list[GraphEdge] = []
    for item in nodes:
        if check is not None:
            check()
        safe_nodes.append(safe_graph_node(item))
    for item in edges:
        if check is not None:
            check()
        safe_edges.append(safe_graph_edge(item))
    return safe_nodes, safe_edges


def graph_content_digest(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    check: Callable[[], None] | None = None,
) -> str:
    """Digest the exact validated property graph independently of row metadata."""

    # Encode one validated record at a time using the canonical key order.
    digest = hashlib.sha256(b'{"edges":[')
    for name, records, key, validate in (
        ("edges", edges, lambda item: item.edge_id, safe_graph_edge),
        ("nodes", nodes, lambda item: item.node_id, safe_graph_node),
    ):
        if name == "nodes":
            digest.update(b'],"nodes":[')
        safe = []
        for item in records:
            if check is not None:
                check()
            safe.append(validate(item))
        if check is not None:
            check()
        safe.sort(key=key)
        for index, item in enumerate(safe):
            if check is not None:
                check()
            if index:
                digest.update(b',')
            digest.update(json.dumps(item.model_dump(mode="json"), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode("utf-8"))
        safe.clear()
    digest.update(b']}')
    if check is not None:
        check()
    return "sha256:" + digest.hexdigest()
