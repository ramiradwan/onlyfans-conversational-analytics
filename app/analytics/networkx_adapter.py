"""Deterministic bounded NetworkX algorithms over disposable graph slices."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import networkx as nx

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.graph_store import GraphDeadlineExceeded, UnsupportedGraphAlgorithm
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphCentralityResult,
    GraphCommunity,
    GraphCommunityResult,
    GraphEdge,
    GraphNode,
)


@dataclass(frozen=True, slots=True)
class BoundedSubgraph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool


def algorithm_parameter_hash(
    *,
    algorithm: str,
    seed: int,
    parameters: dict[str, Any],
    bounds: GraphAlgorithmBounds,
) -> str:
    """Hash every result-affecting algorithm parameter deterministically."""

    encoded = json.dumps(
        {
            "algorithm": algorithm,
            "seed": seed,
            "parameters": parameters,
            "bounds": {
                **bounds.model_dump(
                    mode="json", exclude={"node_kinds", "edge_kinds"}
                ),
                "node_kinds": sorted(item.value for item in bounds.node_kinds),
                "edge_kinds": sorted(item.value for item in bounds.edge_kinds),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

def normalize_algorithm_parameters(
    algorithm: str, parameters: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate and canonicalize defaults before cache lookup or execution."""

    supplied = dict(parameters or {})
    allowed = {
        "degree": set(),
        "betweenness": {"sample_size"},
        "connected_components": set(),
        "louvain": {"resolution", "threshold", "max_level"},
    }.get(algorithm)
    if allowed is None:
        raise UnsupportedGraphAlgorithm("graph_algorithm_unsupported")
    if set(supplied) - allowed:
        raise ValueError("graph_algorithm_parameter_unknown")
    if algorithm == "betweenness":
        sample_size = int(supplied.get("sample_size", 64))
        if sample_size <= 0:
            raise ValueError("graph_algorithm_parameter_invalid")
        return {"sample_size": sample_size}
    if algorithm == "louvain":
        resolution = float(supplied.get("resolution", 1.0))
        threshold = float(supplied.get("threshold", 1e-7))
        max_level_value = supplied.get("max_level")
        max_level = None if max_level_value is None else int(max_level_value)
        if (
            not math.isfinite(resolution)
            or not math.isfinite(threshold)
            or resolution <= 0
            or threshold < 0
            or (max_level is not None and max_level <= 0)
        ):
            raise ValueError("graph_algorithm_parameter_invalid")
        return {
            "max_level": max_level,
            "resolution": resolution,
            "threshold": threshold,
        }
    return {}


class BoundedNetworkXAlgorithms:
    """NetworkX adapter that never owns persistence or loads an unbounded graph."""

    def centrality(
        self,
        subgraph: BoundedSubgraph,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
        deadline: float | None = None,
    ) -> GraphCentralityResult:
        normalized = normalize_algorithm_parameters(algorithm, parameters)
        check = self._checker(cancellation_check, deadline)
        check()
        graph = self._graph(subgraph, check)
        check()
        if algorithm == "degree":
            raw = nx.degree_centrality(graph)
        elif algorithm == "betweenness":
            requested = int(normalized["sample_size"])
            sample_size = min(requested, len(graph)) if graph else None
            raw = nx.betweenness_centrality(
                graph,
                k=sample_size,
                normalized=True,
                seed=seed,
            )
        else:
            raise UnsupportedGraphAlgorithm(
                "graph_algorithm_unsupported"
            )
        # A non-cooperative NetworkX call may finish after cancellation, but its
        # result is rejected here and therefore can never be returned or cached.
        check()
        parameter_hash = algorithm_parameter_hash(
            algorithm=algorithm,
            seed=seed,
            parameters=normalized,
            bounds=bounds,
        )
        return GraphCentralityResult(
            algorithm=algorithm,
            parameter_hash=parameter_hash,
            values={key: round(float(raw[key]), 12) for key in sorted(raw)},
            node_count=len(subgraph.nodes),
            edge_count=graph.number_of_edges(),
            source_edge_count=len(subgraph.edges),
            algorithm_edge_count=graph.number_of_edges(),
            truncated=subgraph.truncated,
        )

    def communities(
        self,
        subgraph: BoundedSubgraph,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
        deadline: float | None = None,
    ) -> GraphCommunityResult:
        normalized = normalize_algorithm_parameters(algorithm, parameters)
        check = self._checker(cancellation_check, deadline)
        check()
        graph = self._graph(subgraph, check)
        check()
        if algorithm == "connected_components":
            raw = list(nx.connected_components(graph))
        elif algorithm == "louvain":
            resolution = float(normalized["resolution"])
            threshold = float(normalized["threshold"])
            raw = list(
                nx.community.louvain_communities(
                    graph,
                    seed=seed,
                    resolution=resolution,
                    threshold=threshold,
                    max_level=normalized["max_level"],
                )
            )
        else:
            raise UnsupportedGraphAlgorithm(
                "graph_algorithm_unsupported"
            )
        check()
        components = [sorted(component) for component in raw]
        components.sort(key=lambda item: (item[0] if item else "", len(item), item))
        parameter_hash = algorithm_parameter_hash(
            algorithm=algorithm,
            seed=seed,
            parameters=normalized,
            bounds=bounds,
        )
        return GraphCommunityResult(
            algorithm=algorithm,
            parameter_hash=parameter_hash,
            communities=[
                GraphCommunity(
                    community_id=f"community-{index:04d}",
                    node_ids=component,
                )
                for index, component in enumerate(components, start=1)
            ],
            node_count=len(subgraph.nodes),
            edge_count=graph.number_of_edges(),
            source_edge_count=len(subgraph.edges),
            algorithm_edge_count=graph.number_of_edges(),
            truncated=subgraph.truncated,
        )

    @staticmethod
    def _graph(subgraph: BoundedSubgraph, check: Callable[[], None]) -> nx.MultiGraph:
        graph = nx.MultiGraph()
        for node in sorted(subgraph.nodes, key=lambda item: item.node_id):
            check()
            graph.add_node(node.node_id)
        node_ids = set(graph)
        for edge in sorted(subgraph.edges, key=lambda item: item.edge_id):
            check()
            if edge.source_id in node_ids and edge.target_id in node_ids:
                graph.add_edge(edge.source_id, edge.target_id, key=edge.edge_id)
        return graph

    @staticmethod
    def _checker(
        cancellation_check: CancellationCheck | None,
        deadline: float | None,
    ) -> Callable[[], None]:
        def check() -> None:
            check_cancelled(cancellation_check)
            if deadline is not None and time.monotonic() > deadline:
                raise GraphDeadlineExceeded("graph_deadline_exceeded")

        return check
