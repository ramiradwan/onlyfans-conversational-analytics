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

    safe_nodes, safe_edges = safe_graph_records(nodes, edges, check=check)
    if check is not None:
        check()
    ordered_nodes = sorted(safe_nodes, key=lambda item: item.node_id)
    if check is not None:
        check()
    node_documents = []
    for item in ordered_nodes:
        if check is not None:
            check()
        node_documents.append(item.model_dump(mode="json"))
    ordered_edges = sorted(safe_edges, key=lambda item: item.edge_id)
    if check is not None:
        check()
    edge_documents = []
    for item in ordered_edges:
        if check is not None:
            check()
        edge_documents.append(item.model_dump(mode="json"))
    value = {"nodes": node_documents, "edges": edge_documents}
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        if check is not None:
            check()
        digest.update(chunk.encode("utf-8"))
    if check is not None:
        check()
    return "sha256:" + digest.hexdigest()
