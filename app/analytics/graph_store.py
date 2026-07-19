"""Engine-neutral, account-scoped, hard-bounded property-graph contract."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Literal, Protocol, runtime_checkable
from uuid import uuid4

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.graph_privacy import safe_graph_edge, safe_graph_node, safe_graph_records
from app.analytics.opaque_refs import validated_account_ref
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphCentralityResult,
    GraphCommunityResult,
    GraphDegreeResult,
    GraphEdge,
    GraphNeighborhood,
    GraphNode,
    GraphPath,
    GraphPathResult,
    GraphRelation,
    GraphTraversalBounds,
)


GraphDirection = Literal["incoming", "outgoing", "both"]
UpsertOutcome = Literal["created", "updated", "unchanged"]


class GraphStoreError(RuntimeError):
    """Base error for graph contract violations."""


class GraphRevisionConflict(GraphStoreError):
    """Raised when a stale or non-deterministic generation is proposed."""


class GraphReferentialIntegrityError(GraphStoreError):
    """Raised when an edge references an absent or cross-account node."""


class UnsupportedGraphAlgorithm(GraphStoreError):
    """Raised when an adapter does not implement an analysis hook."""


class GraphDeadlineExceeded(GraphStoreError):
    """Raised when a graph operation reaches its declared wall-clock limit."""


@runtime_checkable
class GraphReader(Protocol):
    def partition_revision(self, partition_key: str) -> int | None: ...

    def get_node(self, partition_key: str, node_id: str) -> GraphNode | None: ...

    def get_edge(self, partition_key: str, edge_id: str) -> GraphEdge | None: ...

    def nodes(self, partition_key: str) -> list[GraphNode]: ...

    def edges(self, partition_key: str) -> list[GraphEdge]: ...

    def neighborhood(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphNeighborhood: ...

    def find_paths(
        self,
        partition_key: str,
        source_id: str,
        target_id: str,
        *,
        bounds: GraphTraversalBounds,
        directed: bool = True,
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphPathResult: ...

    def degree(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphDegreeResult: ...

    def compute_centrality(
        self,
        partition_key: str,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphCentralityResult: ...

    def detect_communities(
        self,
        partition_key: str,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphCommunityResult: ...


@runtime_checkable
class GraphGenerationWriter(Protocol):
    """Owner-bound mutation surface for exactly one building generation."""

    @property
    def generation_id(self) -> str: ...

    def upsert_node(self, node: GraphNode) -> UpsertOutcome: ...

    def upsert_edge(self, edge: GraphEdge) -> UpsertOutcome: ...

    def replace(self, *, nodes: list[GraphNode], edges: list[GraphEdge]) -> None: ...

    def refresh(self) -> None: ...

    def validate(self) -> str: ...

    def discard(self) -> None: ...


@dataclass(slots=True)
class _Budget:
    deadline: float
    cancellation_check: CancellationCheck | None

    @classmethod
    def traversal(
        cls,
        bounds: GraphTraversalBounds,
        cancellation: CancellationCheck | None,
        deadline: float | None = None,
    ) -> "_Budget":
        return cls(
            deadline or time.monotonic() + bounds.wall_clock_ms / 1000,
            cancellation,
        )

    @classmethod
    def algorithm(
        cls,
        bounds: GraphAlgorithmBounds,
        cancellation: CancellationCheck | None,
        deadline: float | None = None,
    ) -> "_Budget":
        return cls(
            deadline or time.monotonic() + bounds.wall_clock_ms / 1000,
            cancellation,
        )

    def check(self) -> None:
        check_cancelled(self.cancellation_check)
        if time.monotonic() > self.deadline:
            raise GraphDeadlineExceeded("graph_deadline_exceeded")


class _SnapshotGraphStore:
    """Private algorithm snapshot used after generation selection."""

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._revisions: dict[str, int] = {}
        self._generation_identity: dict[str, int] = {}
        self._lock = RLock()

    def upsert_node(self, node: GraphNode) -> UpsertOutcome:
        node = safe_graph_node(node)
        with self._lock:
            existing = self._nodes.get(node.node_id)
            if existing is not None and (
                existing.partition_key != node.partition_key or existing.kind != node.kind
            ):
                raise GraphStoreError("graph_node_identity_conflict")
            if existing == node:
                return "unchanged"
            replacement = dict(self._nodes)
            replacement[node.node_id] = deepcopy(node)
            self._nodes = replacement
            self._advance_generation(node.partition_key)
            return "created" if existing is None else "updated"

    def upsert_edge(self, edge: GraphEdge) -> UpsertOutcome:
        edge = safe_graph_edge(edge)
        with self._lock:
            self._validate_edge(edge, self._nodes)
            existing = self._edges.get(edge.edge_id)
            if existing is not None and (
                existing.partition_key,
                existing.source_id,
                existing.target_id,
                existing.relation,
            ) != (
                edge.partition_key,
                edge.source_id,
                edge.target_id,
                edge.relation,
            ):
                raise GraphStoreError("graph_edge_identity_conflict")
            if existing == edge:
                return "unchanged"
            replacement = dict(self._edges)
            replacement[edge.edge_id] = deepcopy(edge)
            self._edges = replacement
            self._advance_generation(edge.partition_key)
            return "created" if existing is None else "updated"

    def replace_partition(
        self,
        partition_key: str,
        *,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        source_revision: int,
    ) -> bool:
        if source_revision < 0:
            raise ValueError("graph_source_revision_invalid")
        safe_nodes, safe_edges = safe_graph_records(nodes, edges)
        replacement_nodes = self._validated_nodes(partition_key, safe_nodes)
        replacement_edges = self._validated_edges(
            partition_key, safe_edges, replacement_nodes
        )
        with self._lock:
            current_revision = self._revisions.get(partition_key)
            current_nodes = {
                key: value
                for key, value in self._nodes.items()
                if value.partition_key == partition_key
            }
            current_edges = {
                key: value
                for key, value in self._edges.items()
                if value.partition_key == partition_key
            }
            if current_revision is not None and source_revision < current_revision:
                raise GraphRevisionConflict("graph revision moved backwards")
            if current_revision == source_revision:
                if current_nodes == replacement_nodes and current_edges == replacement_edges:
                    return False
                raise GraphRevisionConflict(
                    "the same canonical revision produced different graph content"
                )
            new_nodes = {
                key: value
                for key, value in self._nodes.items()
                if value.partition_key != partition_key
            }
            new_edges = {
                key: value
                for key, value in self._edges.items()
                if value.partition_key != partition_key
            }
            new_nodes.update(deepcopy(replacement_nodes))
            new_edges.update(deepcopy(replacement_edges))
            self._nodes = new_nodes
            self._edges = new_edges
            self._revisions[partition_key] = source_revision
            self._advance_generation(partition_key)
            return True

    def clear_partition(self, partition_key: str) -> None:
        with self._lock:
            self._nodes = {
                key: value
                for key, value in self._nodes.items()
                if value.partition_key != partition_key
            }
            self._edges = {
                key: value
                for key, value in self._edges.items()
                if value.partition_key != partition_key
            }
            self._revisions.pop(partition_key, None)
            self._advance_generation(partition_key)

    def partition_revision(self, partition_key: str) -> int | None:
        with self._lock:
            return self._revisions.get(partition_key)

    def get_node(self, node_id: str) -> GraphNode | None:
        with self._lock:
            node = self._nodes.get(node_id)
            return deepcopy(node) if node is not None else None

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        with self._lock:
            edge = self._edges.get(edge_id)
            return deepcopy(edge) if edge is not None else None

    def nodes(self, partition_key: str) -> list[GraphNode]:
        with self._lock:
            return deepcopy(
                sorted(
                    (
                        item
                        for item in self._nodes.values()
                        if item.partition_key == partition_key
                    ),
                    key=lambda item: item.node_id,
                )
            )

    def edges(self, partition_key: str) -> list[GraphEdge]:
        with self._lock:
            return deepcopy(
                sorted(
                    (
                        item
                        for item in self._edges.values()
                        if item.partition_key == partition_key
                    ),
                    key=lambda item: item.edge_id,
                )
            )

    def neighborhood(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
        _deadline: float | None = None,
    ) -> GraphNeighborhood:
        self._validate_scope(partition_key, bounds.creator_account_id)
        self._validate_direction(direction)
        budget = _Budget.traversal(bounds, cancellation_check, _deadline)
        with self._lock:
            root = self._partition_node(partition_key, node_id)
            root_in_scope = self._node_in_scope(root, bounds)
            if not root_in_scope:
                if bounds.root_policy == "require_in_scope":
                    raise KeyError("graph_node_out_of_scope")
                return GraphNeighborhood(
                    root_node_id=root.node_id,
                    nodes=[deepcopy(root)],
                    edges=[],
                    visited_count=1,
                )
            adjacency, scan_truncated = self._traversal_adjacency(
                partition_key, bounds, direction, relations, budget
            )
            visited = {node_id}
            frontier = deque([(node_id, 0)]) if root_in_scope else deque()
            selected_edges: dict[str, GraphEdge] = {}
            result_limit = max(1, bounds.max_results)
            visited_count = 0
            truncated = scan_truncated
            while frontier:
                budget.check()
                current, depth = frontier.popleft()
                visited_count += 1
                if visited_count >= bounds.max_visited:
                    if frontier or any(
                        adjacent not in visited
                        for _, adjacent in adjacency.get(current, [])
                    ):
                        truncated = True
                    break
                if depth >= bounds.max_hops:
                    if any(adjacent not in visited for _, adjacent in adjacency.get(current, [])):
                        truncated = True
                    continue
                for edge, adjacent in adjacency.get(current, []):
                    budget.check()
                    if adjacent not in visited:
                        if len(visited) >= bounds.max_visited:
                            truncated = True
                            continue
                        if len(frontier) >= bounds.max_queue:
                            truncated = True
                            continue
                        visited.add(adjacent)
                        frontier.append((adjacent, depth + 1))
                    if edge.source_id in visited and edge.target_id in visited:
                        if edge.edge_id in selected_edges:
                            continue
                        if len(selected_edges) < result_limit:
                            selected_edges[edge.edge_id] = edge
                        else:
                            truncated = True
            # Edges are selected only when both declared endpoints are present.
            selected_edges = {
                key: value
                for key, value in selected_edges.items()
                if value.source_id in visited and value.target_id in visited
            }
            result_node_ids = {root.node_id}
            for edge in selected_edges.values():
                result_node_ids.update((edge.source_id, edge.target_id))
            result = GraphNeighborhood(
                root_node_id=root.node_id,
                nodes=[
                    deepcopy(self._nodes[key]) for key in sorted(result_node_ids)
                ],
                edges=[deepcopy(selected_edges[key]) for key in sorted(selected_edges)],
                truncated=truncated,
                visited_count=min(len(visited), bounds.max_visited),
            )
            budget.check()
            return result

    def find_paths(
        self,
        partition_key: str,
        source_id: str,
        target_id: str,
        *,
        bounds: GraphTraversalBounds,
        directed: bool = True,
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
        _deadline: float | None = None,
    ) -> GraphPathResult:
        self._validate_scope(partition_key, bounds.creator_account_id)
        budget = _Budget.traversal(bounds, cancellation_check, _deadline)
        with self._lock:
            source = self._partition_node(partition_key, source_id)
            target = self._partition_node(partition_key, target_id)
            source_in_scope = self._node_in_scope(source, bounds)
            target_in_scope = self._node_in_scope(target, bounds)
            if not source_in_scope or not target_in_scope:
                if bounds.root_policy == "require_in_scope":
                    raise KeyError("graph_node_out_of_scope")
                if source_id == target_id:
                    return GraphPathResult(
                        paths=[GraphPath(node_ids=[source_id], edge_ids=[])],
                        visited_count=1,
                    )
                return GraphPathResult(paths=[], visited_count=1)
            if source_id == target_id:
                return GraphPathResult(
                    paths=[GraphPath(node_ids=[source_id], edge_ids=[])],
                    visited_count=1,
                )
            direction: GraphDirection = "outgoing" if directed else "both"
            adjacency, scan_truncated = self._traversal_adjacency(
                partition_key, bounds, direction, relations, budget
            )
            distances = {source_id: 0}
            predecessors: dict[str, list[tuple[str, str]]] = defaultdict(list)
            queue = deque([source_id])
            visited_count = 0
            target_depth: int | None = None
            truncated = scan_truncated
            while queue:
                budget.check()
                current = queue.popleft()
                depth = distances[current]
                visited_count += 1
                if target_depth is not None and depth >= target_depth:
                    continue
                if visited_count > bounds.max_visited:
                    truncated = True
                    break
                if depth >= bounds.max_hops:
                    if any(
                        adjacent not in distances
                        for _, adjacent in adjacency.get(current, [])
                    ):
                        truncated = True
                    continue
                for edge, adjacent in adjacency.get(current, []):
                    budget.check()
                    next_depth = depth + 1
                    known = distances.get(adjacent)
                    if known is None:
                        if len(distances) >= bounds.max_visited or len(queue) >= bounds.max_queue:
                            truncated = True
                            continue
                        distances[adjacent] = next_depth
                        queue.append(adjacent)
                        known = next_depth
                    if known == next_depth:
                        predecessors[adjacent].append((current, edge.edge_id))
                    if adjacent == target_id and target_depth is None:
                        target_depth = next_depth
            if target_depth is None:
                result = GraphPathResult(
                    paths=[],
                    truncated=truncated
                    or (len(distances) >= bounds.max_visited and target_id not in distances),
                    visited_count=min(len(distances), bounds.max_visited),
                )
                budget.check()
                return result
            paths, reconstruction_truncated = self._reconstruct_shortest_paths(
                source_id,
                target_id,
                predecessors,
                bounds,
                budget,
            )
            result = GraphPathResult(
                paths=paths,
                truncated=truncated or reconstruction_truncated,
                visited_count=min(len(distances), bounds.max_visited),
            )
            budget.check()
            return result

    @staticmethod
    def _reconstruct_shortest_paths(
        source_id: str,
        target_id: str,
        predecessors: dict[str, list[tuple[str, str]]],
        bounds: GraphTraversalBounds,
        budget: _Budget,
    ) -> tuple[list[GraphPath], bool]:
        stack: list[tuple[str, list[str], list[str]]] = [
            (target_id, [target_id], [])
        ]
        results: list[GraphPath] = []
        truncated = False
        while stack and len(results) < bounds.max_results:
            budget.check()
            node_id, reversed_nodes, reversed_edges = stack.pop()
            if node_id == source_id:
                results.append(
                    GraphPath(
                        node_ids=list(reversed(reversed_nodes)),
                        edge_ids=list(reversed(reversed_edges)),
                    )
                )
                continue
            for previous, edge_id in reversed(
                sorted(predecessors.get(node_id, []), key=lambda item: (item[0], item[1]))
            ):
                stack.append(
                    (previous, [*reversed_nodes, previous], [*reversed_edges, edge_id])
                )
        if stack:
            truncated = True
        return results, truncated

    def degree(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
        _deadline: float | None = None,
    ) -> GraphDegreeResult:
        self._validate_scope(partition_key, bounds.creator_account_id)
        self._validate_direction(direction)
        budget = _Budget.traversal(bounds, cancellation_check, _deadline)
        with self._lock:
            node = self._partition_node(partition_key, node_id)
            if not self._node_in_scope(node, bounds):
                if bounds.root_policy == "require_in_scope":
                    raise KeyError("graph_node_out_of_scope")
                return GraphDegreeResult(degree=0)
            adjacency, scan_truncated = self._traversal_adjacency(
                partition_key,
                bounds,
                direction,
                relations,
                budget,
            )
            count = len(adjacency.get(node_id, []))
            cap = min(bounds.max_results, bounds.max_edges_examined)
            result = GraphDegreeResult(
                degree=min(count, cap),
                truncated=scan_truncated or count > cap,
            )
            budget.check()
            return result

    def compute_centrality(
        self,
        partition_key: str,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
        _deadline: float | None = None,
    ) -> GraphCentralityResult:
        from app.analytics.networkx_adapter import BoundedNetworkXAlgorithms

        self._validate_scope(partition_key, bounds.creator_account_id)
        budget = _Budget.algorithm(bounds, cancellation_check, _deadline)
        with self._lock:
            subgraph = self._bounded_subgraph(partition_key, bounds, budget)
        budget.check()
        result = BoundedNetworkXAlgorithms().centrality(
            subgraph,
            algorithm=algorithm,
            bounds=bounds,
            seed=seed,
            parameters=parameters,
            cancellation_check=cancellation_check,
            deadline=budget.deadline,
        )
        budget.check()
        return result

    def detect_communities(
        self,
        partition_key: str,
        *,
        algorithm: str,
        bounds: GraphAlgorithmBounds,
        seed: int,
        parameters: dict[str, Any] | None = None,
        cancellation_check: CancellationCheck | None = None,
        _deadline: float | None = None,
    ) -> GraphCommunityResult:
        from app.analytics.networkx_adapter import BoundedNetworkXAlgorithms

        self._validate_scope(partition_key, bounds.creator_account_id)
        budget = _Budget.algorithm(bounds, cancellation_check, _deadline)
        with self._lock:
            subgraph = self._bounded_subgraph(partition_key, bounds, budget)
        budget.check()
        result = BoundedNetworkXAlgorithms().communities(
            subgraph,
            algorithm=algorithm,
            bounds=bounds,
            seed=seed,
            parameters=parameters,
            cancellation_check=cancellation_check,
            deadline=budget.deadline,
        )
        budget.check()
        return result

    def _bounded_subgraph(
        self, partition_key: str, bounds: GraphAlgorithmBounds, budget: _Budget
    ):
        from app.analytics.networkx_adapter import BoundedSubgraph

        if bounds.root_node_id is not None:
            return self._bounded_root_subgraph(partition_key, bounds, budget)
        eligible: dict[str, GraphNode] = {}
        truncated = False
        for node in sorted(self._nodes.values(), key=lambda item: item.node_id):
            budget.check()
            if node.partition_key != partition_key or not self._node_in_algorithm_scope(
                node, bounds
            ):
                continue
            if len(eligible) >= bounds.max_nodes:
                truncated = True
                break
            eligible[node.node_id] = node
        root = bounds.root_node_id
        if root is not None and root not in eligible:
            known = self._nodes.get(root)
            if (
                bounds.root_policy == "include_only"
                and known is not None
                and known.partition_key == partition_key
            ):
                return BoundedSubgraph(nodes=[deepcopy(known)], edges=[], truncated=True)
            raise KeyError("graph_root_out_of_scope")
        edges: list[GraphEdge] = []
        for edge in sorted(self._edges.values(), key=lambda item: item.edge_id):
            budget.check()
            if edge.partition_key != partition_key:
                continue
            if edge.relation not in bounds.edge_kinds:
                continue
            if edge.source_id not in eligible or edge.target_id not in eligible:
                continue
            if not self._time_in_scope(
                edge.occurred_at,
                bounds.start_time,
                bounds.end_time,
                bounds.include_timeless,
            ):
                continue
            if len(edges) >= bounds.max_edges:
                truncated = True
                break
            edges.append(edge)
        if root is not None:
            selected = self._hop_limited_nodes(root, edges, bounds, budget)
            if len(selected) < len(eligible):
                truncated = True
            eligible = {key: value for key, value in eligible.items() if key in selected}
            edges = [
                edge
                for edge in edges
                if edge.source_id in eligible and edge.target_id in eligible
            ]
        return BoundedSubgraph(
            nodes=[deepcopy(eligible[key]) for key in sorted(eligible)],
            edges=deepcopy(edges),
            truncated=truncated,
        )

    def _bounded_root_subgraph(
        self, partition_key: str, bounds: GraphAlgorithmBounds, budget: _Budget
    ):
        """Select a root-anchored subgraph without materializing path copies."""

        from app.analytics.networkx_adapter import BoundedSubgraph

        assert bounds.root_node_id is not None
        root = self._partition_node(partition_key, bounds.root_node_id)
        if not self._node_in_algorithm_scope(root, bounds):
            if bounds.root_policy == "require_in_scope":
                raise KeyError("graph_root_out_of_scope")
            return BoundedSubgraph(nodes=[deepcopy(root)], edges=[], truncated=False)

        selected_nodes: dict[str, GraphNode] = {root.node_id: root}
        selected_edges: dict[str, GraphEdge] = {}
        frontier = {root.node_id}
        truncated = False
        for _depth in range(bounds.max_hops):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for edge in sorted(self._edges.values(), key=lambda item: item.edge_id):
                budget.check()
                if edge.edge_id in selected_edges or edge.partition_key != partition_key:
                    continue
                if edge.relation not in bounds.edge_kinds:
                    continue
                if edge.source_id not in frontier and edge.target_id not in frontier:
                    continue
                source = self._nodes.get(edge.source_id)
                target = self._nodes.get(edge.target_id)
                if source is None or target is None:
                    continue
                if not self._node_in_algorithm_scope(
                    source, bounds
                ) or not self._node_in_algorithm_scope(target, bounds):
                    continue
                if not self._time_in_scope(
                    edge.occurred_at,
                    bounds.start_time,
                    bounds.end_time,
                    bounds.include_timeless,
                ):
                    continue
                if len(selected_edges) >= bounds.max_edges:
                    truncated = True
                    break
                missing = list(
                    {
                        node.node_id: node
                        for node in (source, target)
                        if node.node_id not in selected_nodes
                    }.values()
                )
                if missing and (
                    len(selected_nodes) + len(missing) > bounds.max_nodes
                    or len(next_frontier) + len(missing) > bounds.max_queue
                ):
                    truncated = True
                    continue
                for node in missing:
                    selected_nodes[node.node_id] = node
                    next_frontier.add(node.node_id)
                selected_edges[edge.edge_id] = edge
            frontier = next_frontier
            if len(selected_edges) >= bounds.max_edges:
                if not truncated and frontier and self._algorithm_extension_exists(
                    partition_key,
                    frontier,
                    set(selected_nodes),
                    set(selected_edges),
                    bounds,
                    budget,
                ):
                    truncated = True
                break
        else:
            if frontier and self._algorithm_extension_exists(
                partition_key, frontier, set(selected_nodes), set(selected_edges), bounds, budget
            ):
                truncated = True
        return BoundedSubgraph(
            nodes=[
                deepcopy(selected_nodes[key]) for key in sorted(selected_nodes)
            ],
            edges=[
                deepcopy(selected_edges[key]) for key in sorted(selected_edges)
            ],
            truncated=truncated,
        )

    @staticmethod
    def _hop_limited_nodes(
        root: str,
        edges: list[GraphEdge],
        bounds: GraphAlgorithmBounds,
        budget: _Budget,
    ) -> set[str]:
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source_id].append(edge.target_id)
            adjacency[edge.target_id].append(edge.source_id)
        selected = {root}
        queue = deque([(root, 0)])
        while queue:
            budget.check()
            node_id, depth = queue.popleft()
            if depth >= bounds.max_hops:
                continue
            for adjacent in sorted(adjacency.get(node_id, [])):
                if adjacent in selected:
                    continue
                if len(selected) >= bounds.max_nodes or len(queue) >= bounds.max_queue:
                    return selected
                selected.add(adjacent)
                queue.append((adjacent, depth + 1))
        return selected

    def _traversal_adjacency(
        self,
        partition_key: str,
        bounds: GraphTraversalBounds,
        direction: GraphDirection,
        relations: set[GraphRelation] | None,
        budget: _Budget,
    ) -> tuple[dict[str, list[tuple[GraphEdge, str]]], bool]:
        effective_relations = bounds.edge_kinds
        if relations is not None:
            effective_relations = effective_relations.intersection(relations)
        adjacency: dict[str, list[tuple[GraphEdge, str]]] = defaultdict(list)
        examined = 0
        truncated = False
        for edge in sorted(self._edges.values(), key=lambda item: item.edge_id):
            budget.check()
            if edge.partition_key != partition_key:
                continue
            if edge.relation not in effective_relations:
                continue
            source = self._nodes.get(edge.source_id)
            target = self._nodes.get(edge.target_id)
            if source is None or target is None:
                continue
            # Both adjacent endpoint timestamps are enforced at every expansion.
            if not self._node_in_scope(source, bounds) or not self._node_in_scope(
                target, bounds
            ):
                continue
            if not self._time_in_scope(
                edge.occurred_at,
                bounds.start_time,
                bounds.end_time,
                bounds.include_timeless,
            ):
                continue
            examined += 1
            if examined > bounds.max_edges_examined:
                truncated = True
                break
            if direction in {"outgoing", "both"}:
                adjacency[edge.source_id].append((edge, edge.target_id))
            if direction in {"incoming", "both"}:
                adjacency[edge.target_id].append((edge, edge.source_id))
        for values in adjacency.values():
            values.sort(key=lambda item: (item[0].edge_id, item[1]))
        return adjacency, truncated

    def _algorithm_extension_exists(
        self,
        partition_key: str,
        frontier: set[str],
        selected_nodes: set[str],
        selected_edges: set[str],
        bounds: GraphAlgorithmBounds,
        budget: _Budget,
    ) -> bool:
        for edge in sorted(self._edges.values(), key=lambda item: item.edge_id):
            budget.check()
            if (
                edge.partition_key != partition_key
                or edge.edge_id in selected_edges
                or edge.relation not in bounds.edge_kinds
                or (edge.source_id not in frontier and edge.target_id not in frontier)
            ):
                continue
            source = self._nodes.get(edge.source_id)
            target = self._nodes.get(edge.target_id)
            if source is None or target is None:
                continue
            if not self._node_in_algorithm_scope(
                source, bounds
            ) or not self._node_in_algorithm_scope(target, bounds):
                continue
            if not self._time_in_scope(
                edge.occurred_at,
                bounds.start_time,
                bounds.end_time,
                bounds.include_timeless,
            ):
                continue
            return True
        return False

    @staticmethod
    def _time_in_scope(value, start, end, include_timeless: bool) -> bool:
        if value is None:
            return include_timeless
        return start <= value <= end

    @classmethod
    def _node_in_scope(cls, node: GraphNode, bounds: GraphTraversalBounds) -> bool:
        return node.kind in bounds.node_kinds and cls._time_in_scope(
            node.occurred_at,
            bounds.start_time,
            bounds.end_time,
            bounds.include_timeless,
        )

    @classmethod
    def _node_in_algorithm_scope(
        cls, node: GraphNode, bounds: GraphAlgorithmBounds
    ) -> bool:
        return node.kind in bounds.node_kinds and cls._time_in_scope(
            node.occurred_at,
            bounds.start_time,
            bounds.end_time,
            bounds.include_timeless,
        )

    def _partition_node(self, partition_key: str, node_id: str) -> GraphNode:
        node = self._nodes.get(node_id)
        if node is None or node.partition_key != partition_key:
            raise KeyError("graph_node_unavailable")
        return node

    def _advance_generation(self, partition_key: str) -> None:
        self._generation_identity[partition_key] = (
            self._generation_identity.get(partition_key, 0) + 1
        )

    @staticmethod
    def _validate_scope(partition_key: str, bounded_account: str) -> None:
        if partition_key != bounded_account:
            raise ValueError("graph_account_mismatch")

    @staticmethod
    def _validate_direction(direction: GraphDirection) -> None:
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("graph_direction_invalid")

    @staticmethod
    def _validated_nodes(
        partition_key: str, nodes: list[GraphNode]
    ) -> dict[str, GraphNode]:
        result: dict[str, GraphNode] = {}
        for node in nodes:
            if node.partition_key != partition_key:
                raise GraphStoreError("graph_account_mismatch")
            existing = result.get(node.node_id)
            if existing is not None and existing != node:
                raise GraphStoreError("graph_node_identity_conflict")
            result[node.node_id] = node
        return result

    @classmethod
    def _validated_edges(
        cls,
        partition_key: str,
        edges: list[GraphEdge],
        nodes: dict[str, GraphNode],
    ) -> dict[str, GraphEdge]:
        result: dict[str, GraphEdge] = {}
        for edge in edges:
            if edge.partition_key != partition_key:
                raise GraphStoreError("graph_account_mismatch")
            cls._validate_edge(edge, nodes)
            existing = result.get(edge.edge_id)
            if existing is not None and existing != edge:
                raise GraphStoreError("graph_edge_identity_conflict")
            result[edge.edge_id] = edge
        return result

    @staticmethod
    def _validate_edge(edge: GraphEdge, nodes: dict[str, GraphNode]) -> None:
        source = nodes.get(edge.source_id)
        target = nodes.get(edge.target_id)
        if source is None or target is None:
            raise GraphReferentialIntegrityError("graph_endpoint_absent")
        if (
            source.partition_key != edge.partition_key
            or target.partition_key != edge.partition_key
        ):
            raise GraphReferentialIntegrityError("graph_account_mismatch")


@dataclass(slots=True)
class _MemoryGeneration:
    generation_id: str
    partition_key: str
    source_revision: int
    owner_token: str
    lease_expires_at: datetime
    status: Literal["building", "validated", "active", "retired"] = "building"
    snapshot: _SnapshotGraphStore | None = None
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: dict[str, GraphEdge] = field(default_factory=dict)


class InMemoryGraphRepository:
    """Generation owner that exposes separate reader and writer surfaces."""

    def __init__(
        self,
        *,
        rollback_retention: int = 1,
        gc_batch_size: int = 8,
        lock: RLock | None = None,
    ) -> None:
        if rollback_retention < 0 or gc_batch_size <= 0:
            raise ValueError("graph_retention_invalid")
        self._nodes: dict[tuple[str, str, str], GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}
        self._generations: dict[str, _MemoryGeneration] = {}
        self._active: dict[str, str] = {}
        self._rollback_retention = rollback_retention
        self._gc_batch_size = gc_batch_size
        self._lock = lock or RLock()
        self._active_visibility: Callable[[str, str], bool] | None = None
        self.reader = InMemoryGraphReader(self)

    def set_active_visibility(
        self, callback: Callable[[str, str], bool] | None
    ) -> None:
        with self._lock:
            self._active_visibility = callback

    def begin_generation(
        self,
        partition_key: str,
        *,
        source_revision: int,
        owner_token: str | None = None,
        lease_seconds: float = 120.0,
    ) -> "InMemoryGraphGenerationWriter":
        if not partition_key or source_revision < 0 or lease_seconds <= 0:
            raise ValueError("graph_generation_input_invalid")
        partition_key = validated_account_ref(partition_key)
        generation_id = str(uuid4())
        token = owner_token or str(uuid4())
        with self._lock:
            self._generations[generation_id] = _MemoryGeneration(
                generation_id=generation_id,
                partition_key=partition_key,
                source_revision=source_revision,
                owner_token=token,
                lease_expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=lease_seconds),
            )
        return InMemoryGraphGenerationWriter(
            self, generation_id, token, lease_seconds=lease_seconds
        )

    def activate(
        self,
        generation_id: str,
        *,
        expected_active: str | None,
        owner_token: str | None = None,
    ) -> None:
        with self._lock:
            generation = self._required(generation_id)
            if generation.status != "validated":
                raise GraphStoreError("graph_generation_not_validated")
            if (
                owner_token is None
                or generation.owner_token != owner_token
                or generation.lease_expires_at <= datetime.now(timezone.utc)
            ):
                raise GraphStoreError("graph_generation_ownership_lost")
            if self._active.get(generation.partition_key) != expected_active:
                raise GraphRevisionConflict("graph_active_generation_changed")
            if expected_active is not None:
                previous = self._required(expected_active)
                previous.status = "retired"
            generation.status = "active"
            self._active[generation.partition_key] = generation_id
            self._collect_locked(generation.partition_key)

    def discard_generation(self, generation_id: str, *, owner_token: str) -> None:
        with self._lock:
            generation = self._required(generation_id)
            if (
                generation.status not in {"building", "validated"}
                or generation.owner_token != owner_token
                or generation.lease_expires_at <= datetime.now(timezone.utc)
            ):
                raise GraphStoreError("graph_generation_ownership_lost")
            generation.status = "retired"
            self._collect_locked(generation.partition_key)

    def reclaim_expired_generation(self, generation_id: str) -> None:
        with self._lock:
            generation = self._required(generation_id)
            if (
                generation.status not in {"building", "validated"}
                or generation.lease_expires_at > datetime.now(timezone.utc)
            ):
                raise GraphStoreError("graph_generation_not_reclaimable")
            generation.status = "retired"
            self._collect_locked(generation.partition_key)

    def validate_generation_owner(
        self,
        generation_id: str,
        *,
        owner_token: str,
        expected_status: Literal["building", "validated"],
    ) -> _MemoryGeneration:
        with self._lock:
            generation = self._required(generation_id)
            if (
                generation.status != expected_status
                or generation.owner_token != owner_token
                or generation.lease_expires_at <= datetime.now(timezone.utc)
            ):
                raise GraphStoreError("graph_generation_ownership_lost")
            return generation

    def _owned_build(self, generation_id: str, owner_token: str) -> _MemoryGeneration:
        generation = self._required(generation_id)
        if (
            generation.status != "building"
            or generation.owner_token != owner_token
            or generation.lease_expires_at <= datetime.now(timezone.utc)
        ):
            raise GraphStoreError("graph_generation_ownership_lost")
        return generation

    def _required(self, generation_id: str) -> _MemoryGeneration:
        generation = self._generations.get(generation_id)
        if generation is None:
            raise GraphStoreError("graph_generation_missing")
        return generation

    def _collect_locked(self, partition_key: str) -> None:
        retired = sorted(
            (
                generation
                for generation in self._generations.values()
                if generation.partition_key == partition_key
                and generation.status == "retired"
            ),
            key=lambda item: item.generation_id,
            reverse=True,
        )
        for generation in retired[
            self._rollback_retention : self._rollback_retention + self._gc_batch_size
        ]:
            self._generations.pop(generation.generation_id, None)
            self._nodes = {
                key: value
                for key, value in self._nodes.items()
                if key[1] != generation.generation_id
            }
            self._edges = {
                key: value
                for key, value in self._edges.items()
                if key[1] != generation.generation_id
            }


class InMemoryGraphGenerationWriter:
    """Lease-fenced writer that becomes unusable immediately after validation."""

    def __init__(
        self,
        repository: InMemoryGraphRepository,
        generation_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> None:
        self._repository = repository
        self._generation_id = generation_id
        self._owner_token = owner_token
        self._lease_seconds = lease_seconds
        self._valid = True

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def upsert_node(self, node: GraphNode) -> UpsertOutcome:
        node = safe_graph_node(node)
        with self._repository._lock:
            generation = self._owned()
            if node.partition_key != generation.partition_key:
                raise GraphStoreError("graph_account_mismatch")
            key = (generation.partition_key, generation.generation_id, node.node_id)
            existing = self._repository._nodes.get(key)
            if existing is not None and existing.kind != node.kind:
                raise GraphStoreError("graph_node_identity_conflict")
            if existing == node:
                return "unchanged"
            self._repository._nodes[key] = deepcopy(node)
            generation.nodes[node.node_id] = deepcopy(node)
            self._owned()
            return "created" if existing is None else "updated"

    def upsert_edge(self, edge: GraphEdge) -> UpsertOutcome:
        edge = safe_graph_edge(edge)
        with self._repository._lock:
            generation = self._owned()
            if edge.partition_key != generation.partition_key:
                raise GraphStoreError("graph_account_mismatch")
            _SnapshotGraphStore._validate_edge(edge, generation.nodes)
            key = (generation.partition_key, generation.generation_id, edge.edge_id)
            existing = self._repository._edges.get(key)
            if existing is not None and (
                existing.source_id,
                existing.target_id,
                existing.relation,
            ) != (edge.source_id, edge.target_id, edge.relation):
                raise GraphStoreError("graph_edge_identity_conflict")
            if existing == edge:
                return "unchanged"
            self._repository._edges[key] = deepcopy(edge)
            generation.edges[edge.edge_id] = deepcopy(edge)
            self._owned()
            return "created" if existing is None else "updated"

    def replace(self, *, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        safe_nodes, safe_edges = safe_graph_records(nodes, edges)
        with self._repository._lock:
            generation = self._owned()
            node_map = _SnapshotGraphStore._validated_nodes(
                generation.partition_key, safe_nodes
            )
            edge_map = _SnapshotGraphStore._validated_edges(
                generation.partition_key, safe_edges, node_map
            )
            generation.nodes = deepcopy(node_map)
            generation.edges = deepcopy(edge_map)
            self._repository._nodes = {
                key: value
                for key, value in self._repository._nodes.items()
                if key[1] != generation.generation_id
            }
            self._repository._edges = {
                key: value
                for key, value in self._repository._edges.items()
                if key[1] != generation.generation_id
            }
            for node_id, node in node_map.items():
                self._repository._nodes[
                    (generation.partition_key, generation.generation_id, node_id)
                ] = deepcopy(node)
            for edge_id, edge in edge_map.items():
                self._repository._edges[
                    (generation.partition_key, generation.generation_id, edge_id)
                ] = deepcopy(edge)
            self._owned()

    def refresh(self) -> None:
        with self._repository._lock:
            generation = self._owned()
            generation.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=self._lease_seconds
            )
            self._owned()

    def validate(self) -> str:
        from app.analytics.graph_privacy import graph_content_digest

        with self._repository._lock:
            generation = self._owned()
            nodes = list(generation.nodes.values())
            edges = list(generation.edges.values())
            snapshot = _SnapshotGraphStore()
            snapshot.replace_partition(
                generation.partition_key,
                nodes=nodes,
                edges=edges,
                source_revision=generation.source_revision,
            )
            digest = graph_content_digest(nodes, edges)
            self._owned()
            generation.snapshot = snapshot
            generation.status = "validated"
            self._valid = False
            return digest

    def discard(self) -> None:
        if not self._valid:
            raise GraphStoreError("graph_writer_invalid")
        self._repository.discard_generation(
            self._generation_id, owner_token=self._owner_token
        )
        self._valid = False

    def _owned(self) -> _MemoryGeneration:
        if not self._valid:
            raise GraphStoreError("graph_writer_invalid")
        return self._repository._owned_build(self._generation_id, self._owner_token)

    def _generation_nodes(self, generation: _MemoryGeneration) -> dict[str, GraphNode]:
        return generation.nodes

    def _generation_edges(self, generation: _MemoryGeneration) -> dict[str, GraphEdge]:
        return generation.edges


class InMemoryGraphReader:
    """Account- and active-generation-scoped read-only graph surface."""

    def __init__(self, repository: InMemoryGraphRepository) -> None:
        self._repository = repository

    def _active_generation(self, partition_key: str) -> str | None:
        generation_id = self._repository._active.get(partition_key)
        if generation_id is None:
            return None
        visible = self._repository._active_visibility
        if visible is not None and not visible(partition_key, generation_id):
            return None
        return generation_id

    def _snapshot(
        self, partition_key: str
    ) -> tuple[str, _SnapshotGraphStore] | None:
        with self._repository._lock:
            generation_id = self._active_generation(partition_key)
            if generation_id is None:
                return None
            generation = self._repository._required(generation_id)
            if generation.status != "active" or generation.snapshot is None:
                return None
            return generation_id, generation.snapshot

    def _require_current(
        self,
        partition_key: str,
        generation_id: str,
        cancellation_check: CancellationCheck | None = None,
    ) -> None:
        with self._repository._lock:
            if self._active_generation(partition_key) != generation_id:
                raise GraphStoreError("graph_generation_changed")
        check_cancelled(cancellation_check)

    def partition_revision(self, partition_key: str) -> int | None:
        partition_key = validated_account_ref(partition_key)
        with self._repository._lock:
            generation_id = self._active_generation(partition_key)
            if generation_id is None:
                return None
            return self._repository._required(generation_id).source_revision

    def active_generation_id(self, partition_key: str) -> str | None:
        partition_key = validated_account_ref(partition_key)
        with self._repository._lock:
            return self._active_generation(partition_key)

    def get_node(self, partition_key: str, node_id: str) -> GraphNode | None:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            return None
        generation_id, snapshot = selected
        node = snapshot.get_node(node_id)
        self._require_current(partition_key, generation_id)
        return node if node is not None and node.partition_key == partition_key else None

    def get_edge(self, partition_key: str, edge_id: str) -> GraphEdge | None:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            return None
        generation_id, snapshot = selected
        edge = snapshot.get_edge(edge_id)
        self._require_current(partition_key, generation_id)
        return edge if edge is not None and edge.partition_key == partition_key else None

    def nodes(self, partition_key: str) -> list[GraphNode]:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            return []
        generation_id, snapshot = selected
        result = snapshot.nodes(partition_key)
        self._require_current(partition_key, generation_id)
        return result

    def edges(self, partition_key: str) -> list[GraphEdge]:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            return []
        generation_id, snapshot = selected
        result = snapshot.edges(partition_key)
        self._require_current(partition_key, generation_id)
        return result

    def neighborhood(self, partition_key: str, node_id: str, **kwargs) -> GraphNeighborhood:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            raise KeyError("graph_node_unavailable")
        generation_id, snapshot = selected
        result = snapshot.neighborhood(partition_key, node_id, **kwargs)
        self._require_current(
            partition_key, generation_id, kwargs.get("cancellation_check")
        )
        return result

    def find_paths(
        self, partition_key: str, source_id: str, target_id: str, **kwargs
    ) -> GraphPathResult:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            raise KeyError("graph_node_unavailable")
        generation_id, snapshot = selected
        result = snapshot.find_paths(partition_key, source_id, target_id, **kwargs)
        self._require_current(
            partition_key, generation_id, kwargs.get("cancellation_check")
        )
        return result

    def degree(self, partition_key: str, node_id: str, **kwargs) -> GraphDegreeResult:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            raise KeyError("graph_node_unavailable")
        generation_id, snapshot = selected
        result = snapshot.degree(partition_key, node_id, **kwargs)
        self._require_current(
            partition_key, generation_id, kwargs.get("cancellation_check")
        )
        return result

    def compute_centrality(self, partition_key: str, **kwargs) -> GraphCentralityResult:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            raise KeyError("graph_partition_unavailable")
        generation_id, snapshot = selected
        result = snapshot.compute_centrality(partition_key, **kwargs)
        self._require_current(
            partition_key, generation_id, kwargs.get("cancellation_check")
        )
        return result

    def detect_communities(self, partition_key: str, **kwargs) -> GraphCommunityResult:
        partition_key = validated_account_ref(partition_key)
        selected = self._snapshot(partition_key)
        if selected is None:
            raise KeyError("graph_partition_unavailable")
        generation_id, snapshot = selected
        result = snapshot.detect_communities(partition_key, **kwargs)
        self._require_current(
            partition_key, generation_id, kwargs.get("cancellation_check")
        )
        return result
