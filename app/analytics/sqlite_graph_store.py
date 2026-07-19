"""SQLite graph adapter over immutable, generation-scoped projection rows."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.database import ProjectionsDatabase
from app.analytics.graph_privacy import (
    graph_content_digest,
    safe_graph_edge,
    safe_graph_node,
    safe_graph_records,
)
from app.analytics.graph_store import (
    GraphDeadlineExceeded,
    GraphDirection,
    GraphReferentialIntegrityError,
    GraphStoreError,
    _SnapshotGraphStore,
    UpsertOutcome,
)
from app.analytics.networkx_adapter import (
    algorithm_parameter_hash,
    normalize_algorithm_parameters,
)
from app.analytics.ownership import BuildOwner
from app.analytics.opaque_refs import validated_account_ref
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphCentralityResult,
    GraphCommunityResult,
    GraphDegreeResult,
    GraphEdge,
    GraphNeighborhood,
    GraphNode,
    GraphNodeKind,
    GraphPathResult,
    GraphRelation,
    GraphTraversalBounds,
)


ActiveGenerationResolver = Callable[..., str | None]


class SQLiteGraphReader:
    """Hard-bounded, account-scoped reads from witnessed active generations."""

    def __init__(
        self,
        database: ProjectionsDatabase | str | Path,
        *,
        active_generation_resolver: ActiveGenerationResolver,
    ) -> None:
        self.database = (
            database
            if isinstance(database, ProjectionsDatabase)
            else ProjectionsDatabase(database)
        )
        self._active_resolver = active_generation_resolver
        self._last_materialized_rows = 0

    @property
    def last_materialized_rows(self) -> int:
        """Rows converted for the most recent bounded SQL materialization."""

        return self._last_materialized_rows

    def partition_revision(self, partition_key: str) -> int | None:
        partition_key = validated_account_ref(partition_key)
        generation_id = self._active_generation_id(partition_key)
        if generation_id is None:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT canonical_revision FROM projection_generations
                WHERE generation_id = ? AND creator_account_id = ? AND status = 'active'
                """,
                (generation_id, partition_key),
            ).fetchone()
        result = None if row is None else int(row[0])
        if self._active_generation_id(
            partition_key, validate_rows=False
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        return result

    def get_node(self, partition_key: str, node_id: str) -> GraphNode | None:
        partition_key = validated_account_ref(partition_key)
        generation_id = self._active_generation_id(partition_key)
        if generation_id is None:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM graph_nodes
                WHERE generation_id=? AND creator_account_id=? AND node_id=?
                """,
                (generation_id, partition_key, node_id),
            ).fetchone()
        result = None if row is None else _node(row)
        if self._active_generation_id(
            partition_key, validate_rows=False
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        return result

    def get_edge(self, partition_key: str, edge_id: str) -> GraphEdge | None:
        partition_key = validated_account_ref(partition_key)
        generation_id = self._active_generation_id(partition_key)
        if generation_id is None:
            return None
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM graph_edges
                WHERE generation_id=? AND creator_account_id=? AND edge_id=?
                """,
                (generation_id, partition_key, edge_id),
            ).fetchone()
        result = None if row is None else _edge(row)
        if self._active_generation_id(
            partition_key, validate_rows=False
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        return result

    def nodes(self, partition_key: str) -> list[GraphNode]:
        partition_key = validated_account_ref(partition_key)
        generation_id = self._active_generation_id(partition_key)
        if generation_id is None:
            return []
        with self.database.read() as connection:
            result = [
                _node(row)
                for row in connection.execute(
                    """
                    SELECT * FROM graph_nodes
                    WHERE generation_id = ? AND creator_account_id = ?
                    ORDER BY node_id
                    """,
                    (generation_id, partition_key),
                )
            ]
        if self._active_generation_id(
            partition_key, validate_rows=False
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        return result

    def edges(self, partition_key: str) -> list[GraphEdge]:
        partition_key = validated_account_ref(partition_key)
        generation_id = self._active_generation_id(partition_key)
        if generation_id is None:
            return []
        with self.database.read() as connection:
            result = [
                _edge(row)
                for row in connection.execute(
                    """
                    SELECT * FROM graph_edges
                    WHERE generation_id = ? AND creator_account_id = ?
                    ORDER BY edge_id
                    """,
                    (generation_id, partition_key),
                )
            ]
        if self._active_generation_id(
            partition_key, validate_rows=False
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        return result

    def neighborhood(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphNeighborhood:
        partition_key = validated_account_ref(partition_key)
        deadline = time.monotonic() + bounds.wall_clock_ms / 1000
        try:
            store, truncated, generation_id = self._bounded_traversal_store(
                partition_key,
                bounds,
                {node_id},
                {node_id},
                direction,
                relations,
                cancellation_check,
                deadline,
                max_depth=bounds.max_hops,
            )
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        result = store.neighborhood(
            partition_key,
            node_id,
            bounds=bounds,
            direction=direction,
            relations=relations,
            cancellation_check=cancellation_check,
            _deadline=deadline,
        )
        self._check_budget(deadline, cancellation_check)
        final = result.model_copy(
            update={"truncated": result.truncated or truncated}
        )
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        return final

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
    ) -> GraphPathResult:
        partition_key = validated_account_ref(partition_key)
        deadline = time.monotonic() + bounds.wall_clock_ms / 1000
        try:
            store, truncated, generation_id = self._bounded_traversal_store(
                partition_key,
                bounds,
                {source_id, target_id},
                {source_id},
                "outgoing" if directed else "both",
                relations,
                cancellation_check,
                deadline,
                max_depth=bounds.max_hops,
            )
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        result = store.find_paths(
            partition_key,
            source_id,
            target_id,
            bounds=bounds,
            directed=directed,
            relations=relations,
            cancellation_check=cancellation_check,
            _deadline=deadline,
        )
        self._check_budget(deadline, cancellation_check)
        if not result.paths and source_id != target_id:
            result = store.find_paths(
                partition_key,
                source_id,
                target_id,
                bounds=bounds,
                directed=directed,
                relations=relations,
                cancellation_check=cancellation_check,
                _deadline=deadline,
            )
        final = result.model_copy(
            update={
                "truncated": result.truncated
                or (truncated and source_id != target_id)
                or (
                    not result.paths
                    and result.visited_count >= bounds.max_visited
                    and source_id != target_id
                )
            }
        )
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        return final

    def degree(
        self,
        partition_key: str,
        node_id: str,
        *,
        bounds: GraphTraversalBounds,
        direction: GraphDirection = "both",
        relations: set[GraphRelation] | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> GraphDegreeResult:
        partition_key = validated_account_ref(partition_key)
        if bounds.creator_account_id != partition_key:
            raise ValueError("graph_account_mismatch")
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("graph_direction_invalid")
        deadline = time.monotonic() + bounds.wall_clock_ms / 1000
        generation_id = self._required_active_generation(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        effective = bounds.edge_kinds
        if relations is not None:
            effective = effective.intersection(relations)
        relation_values = sorted(item.value for item in effective)
        kind_values = sorted(item.value for item in bounds.node_kinds)
        try:
            with self.database.read() as connection:
                self._install_progress_handler(connection, deadline, cancellation_check)
                root = connection.execute(
                    """
                    SELECT * FROM graph_nodes
                    WHERE generation_id=? AND creator_account_id=? AND node_id=?
                    """,
                    (generation_id, partition_key, node_id),
                ).fetchone()
                if root is None:
                    raise KeyError("graph_node_unavailable")
                root_node = _node(root)
                if not (
                    root_node.kind in bounds.node_kinds
                    and _time_in_window(
                        root["occurred_at"],
                        bounds.start_time,
                        bounds.end_time,
                        bounds.include_timeless,
                    )
                ):
                    if bounds.root_policy == "require_in_scope":
                        raise KeyError("graph_node_out_of_scope")
                    return GraphDegreeResult(degree=0)
                if not relation_values or not kind_values:
                    return GraphDegreeResult(degree=0)
                relation_marks = ",".join("?" for _ in relation_values)
                kind_marks = ",".join("?" for _ in kind_values)
                if direction == "outgoing":
                    direction_sql = "e.source_id=?"
                    direction_params = (node_id,)
                elif direction == "incoming":
                    direction_sql = "e.target_id=?"
                    direction_params = (node_id,)
                else:
                    direction_sql = "(e.source_id=? OR e.target_id=?)"
                    direction_params = (node_id, node_id)
                timeless_source = "OR s.occurred_at IS NULL" if bounds.include_timeless else ""
                timeless_target = "OR t.occurred_at IS NULL" if bounds.include_timeless else ""
                timeless_edge = "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
                rows = connection.execute(
                    f"""
                    SELECT e.edge_id FROM graph_edges AS e
                    JOIN graph_nodes AS s
                      ON s.generation_id=e.generation_id
                     AND s.creator_account_id=e.creator_account_id
                     AND s.node_id=e.source_id
                    JOIN graph_nodes AS t
                      ON t.generation_id=e.generation_id
                     AND t.creator_account_id=e.creator_account_id
                     AND t.node_id=e.target_id
                    WHERE e.generation_id=? AND e.creator_account_id=?
                      AND {direction_sql}
                      AND e.relation IN ({relation_marks})
                      AND s.kind IN ({kind_marks}) AND t.kind IN ({kind_marks})
                      AND ((s.occurred_at BETWEEN ? AND ?) {timeless_source})
                      AND ((t.occurred_at BETWEEN ? AND ?) {timeless_target})
                      AND ((e.occurred_at BETWEEN ? AND ?) {timeless_edge})
                    ORDER BY e.edge_id LIMIT ?
                    """,
                    (
                        generation_id,
                        partition_key,
                        *direction_params,
                        *relation_values,
                        *kind_values,
                        *kind_values,
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        max(bounds.max_results, bounds.max_edges_examined) + 1,
                    ),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        self._last_materialized_rows = 1 + len(rows)
        cap = min(bounds.max_results, bounds.max_edges_examined)
        result = GraphDegreeResult(
            degree=min(len(rows), cap),
            truncated=len(rows) > cap
            or (len(rows) >= bounds.max_edges_examined),
        )
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
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
    ) -> GraphCentralityResult:
        partition_key = validated_account_ref(partition_key)
        deadline = time.monotonic() + bounds.wall_clock_ms / 1000
        self._check_budget(deadline, cancellation_check)
        normalized_parameters = normalize_algorithm_parameters(algorithm, parameters)
        parameter_hash = algorithm_parameter_hash(
            algorithm=algorithm,
            seed=seed,
            parameters=normalized_parameters,
            bounds=bounds,
        )
        generation_id = self._required_active_generation(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        self._check_budget(deadline, cancellation_check)
        cached = self._algorithm_cache(
            generation_id,
            partition_key,
            "centrality",
            algorithm,
            parameter_hash,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        if cached is not None:
            self._last_materialized_rows = 0
            self._check_budget(deadline, cancellation_check)
            if self._active_generation_id(
                partition_key,
                deadline=deadline,
                cancellation_check=cancellation_check,
                validate_rows=False,
            ) != generation_id:
                raise GraphStoreError("graph_generation_changed")
            self._check_budget(deadline, cancellation_check)
            result = GraphCentralityResult.model_validate_json(cached)
            self._check_budget(deadline, cancellation_check)
            if self._active_generation_id(
                partition_key,
                deadline=deadline,
                cancellation_check=cancellation_check,
                validate_rows=False,
            ) != generation_id:
                raise GraphStoreError("graph_generation_changed")
            self._check_budget(deadline, cancellation_check)
            return result
        try:
            store, truncated = self._bounded_algorithm_store(
                partition_key,
                bounds,
                cancellation_check,
                deadline,
                expected_generation_id=generation_id,
            )
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        computed = store.compute_centrality(
            partition_key,
            algorithm=algorithm,
            bounds=bounds,
            seed=seed,
            parameters=normalized_parameters,
            cancellation_check=cancellation_check,
            _deadline=deadline,
        )
        result = computed.model_copy(
            update={"truncated": computed.truncated or truncated}
        )
        self._check_budget(deadline, cancellation_check)
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        self._cache_algorithm(
            generation_id,
            partition_key,
            "centrality",
            result.parameter_hash,
            result,
            cancellation_check=cancellation_check,
            deadline=deadline,
        )
        self._check_budget(deadline, cancellation_check)
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            self._delete_algorithm_cache(
                generation_id,
                partition_key,
                "centrality",
                result.algorithm,
                result.parameter_hash,
            )
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
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
    ) -> GraphCommunityResult:
        partition_key = validated_account_ref(partition_key)
        deadline = time.monotonic() + bounds.wall_clock_ms / 1000
        self._check_budget(deadline, cancellation_check)
        normalized_parameters = normalize_algorithm_parameters(algorithm, parameters)
        parameter_hash = algorithm_parameter_hash(
            algorithm=algorithm,
            seed=seed,
            parameters=normalized_parameters,
            bounds=bounds,
        )
        generation_id = self._required_active_generation(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        self._check_budget(deadline, cancellation_check)
        cached = self._algorithm_cache(
            generation_id,
            partition_key,
            "community",
            algorithm,
            parameter_hash,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        if cached is not None:
            self._last_materialized_rows = 0
            self._check_budget(deadline, cancellation_check)
            if self._active_generation_id(
                partition_key,
                deadline=deadline,
                cancellation_check=cancellation_check,
                validate_rows=False,
            ) != generation_id:
                raise GraphStoreError("graph_generation_changed")
            self._check_budget(deadline, cancellation_check)
            result = GraphCommunityResult.model_validate_json(cached)
            self._check_budget(deadline, cancellation_check)
            if self._active_generation_id(
                partition_key,
                deadline=deadline,
                cancellation_check=cancellation_check,
                validate_rows=False,
            ) != generation_id:
                raise GraphStoreError("graph_generation_changed")
            self._check_budget(deadline, cancellation_check)
            return result
        try:
            store, truncated = self._bounded_algorithm_store(
                partition_key,
                bounds,
                cancellation_check,
                deadline,
                expected_generation_id=generation_id,
            )
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        computed = store.detect_communities(
            partition_key,
            algorithm=algorithm,
            bounds=bounds,
            seed=seed,
            parameters=normalized_parameters,
            cancellation_check=cancellation_check,
            _deadline=deadline,
        )
        result = computed.model_copy(
            update={"truncated": computed.truncated or truncated}
        )
        self._check_budget(deadline, cancellation_check)
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        self._cache_algorithm(
            generation_id,
            partition_key,
            "community",
            result.parameter_hash,
            result,
            cancellation_check=cancellation_check,
            deadline=deadline,
        )
        self._check_budget(deadline, cancellation_check)
        if self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=False,
        ) != generation_id:
            self._delete_algorithm_cache(
                generation_id,
                partition_key,
                "community",
                result.algorithm,
                result.parameter_hash,
            )
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        return result

    def _bounded_traversal_store(
        self,
        partition_key: str,
        bounds: GraphTraversalBounds,
        roots: set[str],
        expansion_roots: set[str],
        direction: GraphDirection,
        relations: set[GraphRelation] | None,
        cancellation_check: CancellationCheck | None,
        deadline: float,
        *,
        max_depth: int,
    ) -> tuple[_SnapshotGraphStore, bool, str]:
        if bounds.creator_account_id != partition_key:
            raise ValueError("graph_account_mismatch")
        generation_id = self._required_active_generation(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        effective = bounds.edge_kinds
        if relations is not None:
            effective = effective.intersection(relations)
        relation_values = sorted(item.value for item in effective)
        kind_values = sorted(item.value for item in bounds.node_kinds)
        selected_nodes: dict[str, GraphNode] = {}
        selected_edges: dict[str, GraphEdge] = {}
        examined_edges: set[str] = set()
        truncated = False
        materialized = 0
        with self.database.read() as connection:
            self._install_progress_handler(connection, deadline, cancellation_check)
            root_nodes = self._nodes_by_ids(
                connection,
                generation_id,
                partition_key,
                roots,
                deadline=deadline,
                cancellation_check=cancellation_check,
            )
            materialized += len(root_nodes)
            if {item.node_id for item in root_nodes} != roots:
                raise KeyError("graph_node_unavailable")
            for node in root_nodes:
                selected_nodes[node.node_id] = node
                in_scope = (
                    node.kind in bounds.node_kinds
                    and _time_in_window(
                        _timestamp(node.occurred_at),
                        bounds.start_time,
                        bounds.end_time,
                        bounds.include_timeless,
                    )
                )
                if not in_scope and bounds.root_policy == "require_in_scope":
                    raise KeyError("graph_node_out_of_scope")
            frontier = {
                node_id
                for node_id in expansion_roots
                if node_id in selected_nodes
                and selected_nodes[node_id].kind in bounds.node_kinds
                and _time_in_window(
                    _timestamp(selected_nodes[node_id].occurred_at),
                    bounds.start_time,
                    bounds.end_time,
                    bounds.include_timeless,
                )
            }
            if not relation_values or not kind_values:
                frontier.clear()
            relation_marks = ",".join("?" for _ in relation_values)
            kind_marks = ",".join("?" for _ in kind_values)
            timeless_source = (
                "OR s.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            timeless_target = (
                "OR t.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            timeless_edge = (
                "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            for _depth in range(max_depth):
                self._check_budget(deadline, cancellation_check)
                if not frontier:
                    break
                remaining = bounds.max_edges_examined - len(examined_edges)
                if direction == "outgoing":
                    frontier_sql = "e.source_id IN (SELECT value FROM json_each(?))"
                    frontier_params = [_json(sorted(frontier))]
                elif direction == "incoming":
                    frontier_sql = "e.target_id IN (SELECT value FROM json_each(?))"
                    frontier_params = [_json(sorted(frontier))]
                else:
                    frontier_sql = "(e.source_id IN (SELECT value FROM json_each(?)) OR e.target_id IN (SELECT value FROM json_each(?)))"
                    frontier_params = [
                        _json(sorted(frontier)),
                        _json(sorted(frontier)),
                    ]
                # max_results bounds returned paths/edges, not the SQL frontier.
                # Fetch only the remaining examination budget plus one sentinel;
                # queue/visited caps are applied while expanding distinct nodes.
                row_limit = max(1, remaining + 1)
                rows = connection.execute(
                    f"""
                    SELECT e.* FROM graph_edges AS e
                    JOIN graph_nodes AS s
                      ON s.generation_id=e.generation_id
                     AND s.creator_account_id=e.creator_account_id
                     AND s.node_id=e.source_id
                    JOIN graph_nodes AS t
                      ON t.generation_id=e.generation_id
                     AND t.creator_account_id=e.creator_account_id
                     AND t.node_id=e.target_id
                    WHERE e.generation_id=? AND e.creator_account_id=?
                      AND {frontier_sql}
                      AND e.edge_id NOT IN (SELECT value FROM json_each(?))
                      AND e.relation IN ({relation_marks})
                      AND s.kind IN ({kind_marks}) AND t.kind IN ({kind_marks})
                      AND ((s.occurred_at BETWEEN ? AND ?) {timeless_source})
                      AND ((t.occurred_at BETWEEN ? AND ?) {timeless_target})
                      AND ((e.occurred_at BETWEEN ? AND ?) {timeless_edge})
                    ORDER BY e.edge_id LIMIT ?
                    """,
                    (
                        generation_id,
                        partition_key,
                        *frontier_params,
                        _json(sorted(examined_edges)),
                        *relation_values,
                        *kind_values,
                        *kind_values,
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        _timestamp(bounds.start_time),
                        _timestamp(bounds.end_time),
                        row_limit,
                    ),
                ).fetchall()
                if len(rows) > remaining:
                    truncated = True
                edge_rows = rows[: max(0, remaining)]
                examined_edges.update(row["edge_id"] for row in edge_rows)
                materialized += len(edge_rows)
                endpoint_ids = {
                    value
                    for row in edge_rows
                    for value in (row["source_id"], row["target_id"])
                }
                endpoint_nodes = {
                    item.node_id: item
                    for item in self._nodes_by_ids(
                        connection,
                        generation_id,
                        partition_key,
                        endpoint_ids,
                        deadline=deadline,
                        cancellation_check=cancellation_check,
                    )
                }
                materialized += len(endpoint_nodes)
                next_frontier: set[str] = set()
                for row in edge_rows:
                    self._check_budget(deadline, cancellation_check)
                    edge = _edge(row)
                    missing = [
                        endpoint_nodes[node_id]
                        for node_id in (edge.source_id, edge.target_id)
                        if node_id not in selected_nodes
                    ]
                    if missing and len(selected_nodes) + len(missing) > bounds.max_visited:
                        truncated = True
                        continue
                    for node in missing:
                        selected_nodes[node.node_id] = node
                        if len(next_frontier) < bounds.max_queue:
                            next_frontier.add(node.node_id)
                        else:
                            truncated = True
                    if (
                        edge.source_id in selected_nodes
                        and edge.target_id in selected_nodes
                    ):
                        selected_edges[edge.edge_id] = edge
                frontier = next_frontier
            else:
                if frontier and self._traversal_extension_exists(
                    connection,
                    generation_id,
                    partition_key,
                    frontier,
                    set(selected_nodes),
                    set(selected_edges),
                    direction,
                    relation_values,
                    kind_values,
                    bounds,
                    deadline,
                    cancellation_check,
                ):
                    truncated = True
        self._last_materialized_rows = materialized
        store = self._memory_snapshot(
            partition_key,
            [selected_nodes[key] for key in sorted(selected_nodes)],
            [selected_edges[key] for key in sorted(selected_edges)],
            0,
        )
        return store, truncated, generation_id

    def _traversal_extension_exists(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        partition_key: str,
        frontier: set[str],
        selected_nodes: set[str],
        selected_edges: set[str],
        direction: GraphDirection,
        relation_values: list[str],
        kind_values: list[str],
        bounds: GraphTraversalBounds,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> bool:
        """Return whether one eligible unseen endpoint exists past the hop cap."""

        self._check_budget(deadline, cancellation_check)
        if not frontier or not relation_values or not kind_values:
            return False
        relation_marks = ",".join("?" for _ in relation_values)
        kind_marks = ",".join("?" for _ in kind_values)
        if direction == "outgoing":
            frontier_sql = "e.source_id IN (SELECT value FROM json_each(?))"
            frontier_params = [_json(sorted(frontier))]
            adjacent_sql = "e.target_id"
            adjacent_params: list[str] = []
        elif direction == "incoming":
            frontier_sql = "e.target_id IN (SELECT value FROM json_each(?))"
            frontier_params = [_json(sorted(frontier))]
            adjacent_sql = "e.source_id"
            adjacent_params = []
        else:
            frontier_sql = "(e.source_id IN (SELECT value FROM json_each(?)) OR e.target_id IN (SELECT value FROM json_each(?)))"
            frontier_params = [_json(sorted(frontier)), _json(sorted(frontier))]
            adjacent_sql = "CASE WHEN e.source_id IN (SELECT value FROM json_each(?)) AND e.target_id NOT IN (SELECT value FROM json_each(?)) THEN e.target_id ELSE e.source_id END"
            adjacent_params = [_json(sorted(frontier)), _json(sorted(frontier))]
        timeless_source = "OR s.occurred_at IS NULL" if bounds.include_timeless else ""
        timeless_target = "OR t.occurred_at IS NULL" if bounds.include_timeless else ""
        timeless_edge = "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
        row = connection.execute(
            f"""
            SELECT 1 FROM graph_edges AS e
            JOIN graph_nodes AS s
              ON s.generation_id=e.generation_id
             AND s.creator_account_id=e.creator_account_id
             AND s.node_id=e.source_id
            JOIN graph_nodes AS t
              ON t.generation_id=e.generation_id
             AND t.creator_account_id=e.creator_account_id
             AND t.node_id=e.target_id
            WHERE e.generation_id=? AND e.creator_account_id=?
              AND {frontier_sql}
              AND e.edge_id NOT IN (SELECT value FROM json_each(?))
              AND {adjacent_sql} NOT IN (SELECT value FROM json_each(?))
              AND e.relation IN ({relation_marks})
              AND s.kind IN ({kind_marks}) AND t.kind IN ({kind_marks})
              AND ((s.occurred_at BETWEEN ? AND ?) {timeless_source})
              AND ((t.occurred_at BETWEEN ? AND ?) {timeless_target})
              AND ((e.occurred_at BETWEEN ? AND ?) {timeless_edge})
            LIMIT 1
            """,
            (
                generation_id,
                partition_key,
                *frontier_params,
                *adjacent_params,
                _json(sorted(selected_edges)),
                _json(sorted(selected_nodes)),
                *relation_values,
                *kind_values,
                *kind_values,
                _timestamp(bounds.start_time),
                _timestamp(bounds.end_time),
                _timestamp(bounds.start_time),
                _timestamp(bounds.end_time),
                _timestamp(bounds.start_time),
                _timestamp(bounds.end_time),
            ),
        ).fetchone()
        self._check_budget(deadline, cancellation_check)
        return row is not None

    def _bounded_algorithm_store(
        self,
        partition_key: str,
        bounds: GraphAlgorithmBounds,
        cancellation_check: CancellationCheck | None,
        deadline: float,
        *,
        expected_generation_id: str | None = None,
    ) -> tuple[_SnapshotGraphStore, bool]:
        if bounds.creator_account_id != partition_key:
            raise ValueError("graph_account_mismatch")
        generation_id = self._required_active_generation(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        if (
            expected_generation_id is not None
            and generation_id != expected_generation_id
        ):
            raise GraphStoreError("graph_generation_changed")
        self._check_budget(deadline, cancellation_check)
        if bounds.root_node_id is not None:
            return self._bounded_root_algorithm_store(
                generation_id,
                partition_key,
                bounds,
                deadline,
                cancellation_check,
            )
        kinds = sorted(item.value for item in bounds.node_kinds)
        if not kinds:
            self._last_materialized_rows = 0
            return self._memory_snapshot(partition_key, [], [], 0), False
        marks = ",".join("?" for _ in kinds)
        timeless = "OR occurred_at IS NULL" if bounds.include_timeless else ""
        try:
            with self.database.read() as connection:
                self._install_progress_handler(connection, deadline, cancellation_check)
                rows = connection.execute(
                f"""
                SELECT * FROM graph_nodes
                WHERE generation_id=? AND creator_account_id=?
                  AND kind IN ({marks})
                  AND ((julianday(occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless})
                ORDER BY node_id LIMIT ?
                """,
                (
                    generation_id,
                    partition_key,
                    *kinds,
                    bounds.start_time.isoformat(),
                    bounds.end_time.isoformat(),
                    bounds.max_nodes + 1,
                ),
                ).fetchall()
                truncated = len(rows) > bounds.max_nodes
                node_rows = rows[: bounds.max_nodes]
                nodes: list[GraphNode] = []
                for row in node_rows:
                    self._check_budget(deadline, cancellation_check)
                    nodes.append(_node(row))
                node_ids = {item.node_id for item in nodes}
                if bounds.root_node_id is not None and bounds.root_node_id not in node_ids:
                    root_rows = self._nodes_by_ids(
                        connection,
                        generation_id,
                        partition_key,
                        {bounds.root_node_id},
                        deadline=deadline,
                        cancellation_check=cancellation_check,
                    )
                    if not root_rows:
                        raise KeyError("graph_root_unavailable")
                    root = root_rows[0]
                    root_in_scope = (
                        root.kind in bounds.node_kinds
                        and _time_in_window(
                            None if root.occurred_at is None else root.occurred_at.isoformat(),
                            bounds.start_time,
                            bounds.end_time,
                            bounds.include_timeless,
                        )
                    )
                    if not root_in_scope:
                        if bounds.root_policy == "require_in_scope":
                            raise KeyError("graph_root_out_of_scope")
                        self._last_materialized_rows = len(rows) + 1
                        return self._memory_snapshot(
                            partition_key, [root], [], 0
                        ), True
                    if len(nodes) >= bounds.max_nodes:
                        nodes[-1] = root
                        truncated = True
                    else:
                        nodes.append(root)
                    nodes.sort(key=lambda item: item.node_id)
                    node_ids = {item.node_id for item in nodes}
                relation_values = sorted(item.value for item in bounds.edge_kinds)
                if not relation_values or not node_ids:
                    edge_rows = []
                else:
                    relation_marks = ",".join("?" for _ in relation_values)
                    node_id_json = _json(sorted(node_ids))
                    timeless_edge = "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
                    edge_rows = connection.execute(
                    f"""
                SELECT * FROM graph_edges
                AS e
                WHERE e.generation_id=? AND e.creator_account_id=?
                  AND e.source_id IN (SELECT value FROM json_each(?))
                  AND e.target_id IN (SELECT value FROM json_each(?))
                  AND e.relation IN ({relation_marks})
                  AND ((julianday(e.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_edge})
                ORDER BY e.edge_id LIMIT ?
                    """,
                    (
                        generation_id,
                        partition_key,
                        node_id_json,
                        node_id_json,
                        *relation_values,
                        bounds.start_time.isoformat(),
                        bounds.end_time.isoformat(),
                        bounds.max_edges + 1,
                    ),
                    ).fetchall()
                self._check_budget(deadline, cancellation_check)
        except sqlite3.OperationalError as exc:
            self._translate_interrupted(exc, deadline, cancellation_check)
            raise
        edges: list[GraphEdge] = []
        for row in edge_rows[: bounds.max_edges]:
            self._check_budget(deadline, cancellation_check)
            if row["source_id"] in node_ids and row["target_id"] in node_ids:
                edges.append(_edge(row))
        truncated = truncated or len(edge_rows) > bounds.max_edges
        self._last_materialized_rows = len(node_rows) + len(edge_rows)
        return (
            self._memory_snapshot(
                partition_key,
                nodes,
                edges,
                0,
            ),
            truncated,
        )

    def _root_algorithm_extension_exists(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        partition_key: str,
        frontier: set[str],
        selected_edges: set[str],
        bounds: GraphAlgorithmBounds,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> bool:
        """Report only genuinely eligible work beyond the final hop."""

        self._check_budget(deadline, cancellation_check)
        kinds = sorted(item.value for item in bounds.node_kinds)
        relations = sorted(item.value for item in bounds.edge_kinds)
        if not frontier or not kinds or not relations:
            return False
        kind_marks = ",".join("?" for _ in kinds)
        relation_marks = ",".join("?" for _ in relations)
        timeless_source = "OR s.occurred_at IS NULL" if bounds.include_timeless else ""
        timeless_target = "OR t.occurred_at IS NULL" if bounds.include_timeless else ""
        timeless_edge = "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
        frontier_json = _json(sorted(frontier))
        selected_edge_json = _json(sorted(selected_edges))
        row = connection.execute(
            f"""
            SELECT 1 FROM graph_edges AS e
            JOIN graph_nodes AS s
              ON s.generation_id=e.generation_id
             AND s.creator_account_id=e.creator_account_id
             AND s.node_id=e.source_id
            JOIN graph_nodes AS t
              ON t.generation_id=e.generation_id
             AND t.creator_account_id=e.creator_account_id
             AND t.node_id=e.target_id
            WHERE e.generation_id=? AND e.creator_account_id=?
              AND (
                e.source_id IN (SELECT value FROM json_each(?))
                OR e.target_id IN (SELECT value FROM json_each(?))
              )
              AND e.edge_id NOT IN (SELECT value FROM json_each(?))
              AND e.relation IN ({relation_marks})
              AND s.kind IN ({kind_marks})
              AND t.kind IN ({kind_marks})
              AND ((julianday(s.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_source})
              AND ((julianday(t.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_target})
              AND ((julianday(e.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_edge})
            LIMIT 1
            """,
            (
                generation_id,
                partition_key,
                frontier_json,
                frontier_json,
                selected_edge_json,
                *relations,
                *kinds,
                *kinds,
                bounds.start_time.isoformat(),
                bounds.end_time.isoformat(),
                bounds.start_time.isoformat(),
                bounds.end_time.isoformat(),
                bounds.start_time.isoformat(),
                bounds.end_time.isoformat(),
            ),
        ).fetchone()
        self._check_budget(deadline, cancellation_check)
        return row is not None

    def _bounded_root_algorithm_store(
        self,
        generation_id: str,
        partition_key: str,
        bounds: GraphAlgorithmBounds,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> tuple[_SnapshotGraphStore, bool]:
        """Read a bounded root-anchored slice with endpoint scope in SQL."""

        assert bounds.root_node_id is not None
        kinds = sorted(item.value for item in bounds.node_kinds)
        relations = sorted(item.value for item in bounds.edge_kinds)
        with self.database.read() as connection:
            self._install_progress_handler(connection, deadline, cancellation_check)
            root_row = connection.execute(
                """
                SELECT * FROM graph_nodes
                WHERE generation_id=? AND creator_account_id=? AND node_id=?
                """,
                (generation_id, partition_key, bounds.root_node_id),
            ).fetchone()
            if root_row is None:
                raise KeyError("graph_root_unavailable")
            root = _node(root_row)
            root_in_scope = (
                root.kind in bounds.node_kinds
                and _time_in_window(
                    root_row["occurred_at"],
                    bounds.start_time,
                    bounds.end_time,
                    bounds.include_timeless,
                )
            )
            if not root_in_scope:
                if bounds.root_policy == "require_in_scope":
                    raise KeyError("graph_root_out_of_scope")
                self._last_materialized_rows = 1
                return self._memory_snapshot(
                    partition_key, [root], [], 0
                ), False
            if not kinds or not relations:
                self._last_materialized_rows = 1
                return self._memory_snapshot(
                    partition_key,
                    [root],
                    [],
                    0,
                ), False

            kind_marks = ",".join("?" for _ in kinds)
            relation_marks = ",".join("?" for _ in relations)
            timeless_source = (
                "OR s.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            timeless_target = (
                "OR t.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            timeless_edge = (
                "OR e.occurred_at IS NULL" if bounds.include_timeless else ""
            )
            selected_nodes: dict[str, GraphNode] = {root.node_id: root}
            selected_edges: dict[str, GraphEdge] = {}
            frontier = {root.node_id}
            truncated = False
            for _depth in range(bounds.max_hops):
                self._check_budget(deadline, cancellation_check)
                if not frontier:
                    break
                remaining_edges = bounds.max_edges - len(selected_edges)
                if remaining_edges <= 0:
                    truncated = self._root_algorithm_extension_exists(
                        connection,
                        generation_id,
                        partition_key,
                        frontier,
                        set(selected_edges),
                        bounds,
                        deadline,
                        cancellation_check,
                    )
                    break
                frontier_json = _json(sorted(frontier))
                selected_edge_json = _json(sorted(selected_edges))
                edge_rows = connection.execute(
                    f"""
                    SELECT e.* FROM graph_edges AS e
                    JOIN graph_nodes AS s
                      ON s.generation_id=e.generation_id
                     AND s.creator_account_id=e.creator_account_id
                     AND s.node_id=e.source_id
                    JOIN graph_nodes AS t
                      ON t.generation_id=e.generation_id
                     AND t.creator_account_id=e.creator_account_id
                     AND t.node_id=e.target_id
                    WHERE e.generation_id=? AND e.creator_account_id=?
                      AND (
                        e.source_id IN (SELECT value FROM json_each(?))
                        OR e.target_id IN (SELECT value FROM json_each(?))
                      )
                      AND e.edge_id NOT IN (SELECT value FROM json_each(?))
                      AND e.relation IN ({relation_marks})
                      AND s.kind IN ({kind_marks})
                      AND t.kind IN ({kind_marks})
                      AND ((julianday(s.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_source})
                      AND ((julianday(t.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_target})
                      AND ((julianday(e.occurred_at) BETWEEN julianday(?) AND julianday(?)) {timeless_edge})
                    ORDER BY e.edge_id LIMIT ?
                    """,
                    (
                        generation_id,
                        partition_key,
                        frontier_json,
                        frontier_json,
                        selected_edge_json,
                        *relations,
                        *kinds,
                        *kinds,
                        bounds.start_time.isoformat(),
                        bounds.end_time.isoformat(),
                        bounds.start_time.isoformat(),
                        bounds.end_time.isoformat(),
                        bounds.start_time.isoformat(),
                        bounds.end_time.isoformat(),
                        remaining_edges + 1,
                    ),
                ).fetchall()
                if len(edge_rows) > remaining_edges:
                    truncated = True
                candidate_edges: list[GraphEdge] = []
                for row in edge_rows[:remaining_edges]:
                    self._check_budget(deadline, cancellation_check)
                    candidate_edges.append(_edge(row))
                endpoint_ids = {
                    node_id
                    for edge in candidate_edges
                    for node_id in (edge.source_id, edge.target_id)
                }
                endpoint_json = _json(sorted(endpoint_ids))
                endpoint_rows = connection.execute(
                    """
                    SELECT * FROM graph_nodes
                    WHERE generation_id=? AND creator_account_id=?
                      AND node_id IN (SELECT value FROM json_each(?))
                    ORDER BY node_id
                    """,
                    (generation_id, partition_key, endpoint_json),
                ).fetchall()
                endpoint_nodes: dict[str, GraphNode] = {}
                for row in endpoint_rows:
                    self._check_budget(deadline, cancellation_check)
                    endpoint_nodes[row["node_id"]] = _node(row)
                next_frontier: set[str] = set()
                for edge in candidate_edges:
                    self._check_budget(deadline, cancellation_check)
                    missing = list(
                        {
                            node_id: endpoint_nodes[node_id]
                            for node_id in (edge.source_id, edge.target_id)
                            if node_id not in selected_nodes
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
            else:
                if frontier and self._root_algorithm_extension_exists(
                    connection,
                    generation_id,
                    partition_key,
                    frontier,
                    set(selected_edges),
                    bounds,
                    deadline,
                    cancellation_check,
                ):
                    truncated = True
            self._check_budget(deadline, cancellation_check)
        self._last_materialized_rows = len(selected_nodes) + len(selected_edges)
        return (
            self._memory_snapshot(
                partition_key,
                [selected_nodes[key] for key in sorted(selected_nodes)],
                [selected_edges[key] for key in sorted(selected_edges)],
                0,
            ),
            truncated,
        )

    @staticmethod
    def _memory_snapshot(
        partition_key: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        revision: int,
    ) -> _SnapshotGraphStore:
        store = _SnapshotGraphStore()
        store.replace_partition(
            partition_key,
            nodes=nodes,
            edges=edges,
            source_revision=revision,
        )
        return store

    def _active_generation_id(
        self,
        partition_key: str,
        *,
        deadline: float | None = None,
        cancellation_check: CancellationCheck | None = None,
        validate_rows: bool = False,
    ) -> str | None:
        if deadline is not None:
            self._check_budget(deadline, cancellation_check)
        generation_id = self._active_resolver(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
            validate_rows=validate_rows,
        )
        if deadline is not None:
            self._check_budget(deadline, cancellation_check)
        else:
            check_cancelled(cancellation_check)
        return generation_id

    def _required_active_generation(
        self,
        partition_key: str,
        *,
        deadline: float | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> str:
        generation_id = self._active_generation_id(
            partition_key,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        if generation_id is None:
            raise KeyError("graph_partition_unavailable")
        return generation_id

    def _algorithm_cache(
        self,
        generation_id: str,
        partition_key: str,
        metric_kind: str,
        algorithm: str,
        parameter_hash: str,
        *,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> str | None:
        self._check_budget(deadline, cancellation_check)
        try:
            with self.database.read() as connection:
                self._install_progress_handler(connection, deadline, cancellation_check)
                row = connection.execute(
                    """
                    SELECT result_json FROM graph_algorithm_metrics
                    WHERE generation_id=? AND creator_account_id=? AND metric_kind=?
                      AND algorithm=? AND parameter_hash=?
                    """,
                    (
                        generation_id,
                        partition_key,
                        metric_kind,
                        algorithm,
                        parameter_hash,
                    ),
                ).fetchone()
        except sqlite3.OperationalError as error:
            self._translate_interrupted(error, deadline, cancellation_check)
            raise GraphStoreError("graph_cache_unavailable") from None
        self._check_budget(deadline, cancellation_check)
        return None if row is None else row[0]

    def _cache_algorithm(
        self,
        generation_id: str,
        partition_key: str,
        metric_kind: str,
        parameter_hash: str,
        result: GraphCentralityResult | GraphCommunityResult,
        *,
        cancellation_check: CancellationCheck | None = None,
        deadline: float,
    ) -> None:
        self._check_budget(deadline, cancellation_check)
        try:
            with self.database.transaction() as connection:
                self._install_progress_handler(connection, deadline, cancellation_check)
                self._check_budget(deadline, cancellation_check)
                active = connection.execute(
                    """
                    SELECT activation_intent_id, witness_sequence, publication_epoch
                    FROM projection_generations
                    WHERE generation_id=? AND creator_account_id=? AND status='active'
                    """,
                    (generation_id, partition_key),
                ).fetchone()
                if active is None:
                    return
                connection.execute(
                    """
                    INSERT OR IGNORE INTO graph_algorithm_metrics (
                        generation_id, creator_account_id, metric_kind, algorithm,
                        parameter_hash, result_json, computed_at,
                        activation_intent_id, witness_sequence, publication_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation_id,
                        partition_key,
                        metric_kind,
                        result.algorithm,
                        parameter_hash,
                        result.model_dump_json(),
                        _timestamp(_now()),
                        active["activation_intent_id"],
                        active["witness_sequence"],
                        active["publication_epoch"],
                    ),
                )
                self._check_budget(deadline, cancellation_check)
        except sqlite3.OperationalError as error:
            self._translate_interrupted(error, deadline, cancellation_check)
            raise GraphStoreError("graph_cache_unavailable") from None
        self._check_budget(deadline, cancellation_check)

    def _delete_algorithm_cache(
        self,
        generation_id: str,
        partition_key: str,
        metric_kind: str,
        algorithm: str,
        parameter_hash: str,
    ) -> None:
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    DELETE FROM graph_algorithm_metrics
                    WHERE generation_id=? AND creator_account_id=?
                      AND metric_kind=? AND algorithm=? AND parameter_hash=?
                    """,
                    (
                        generation_id,
                        partition_key,
                        metric_kind,
                        algorithm,
                        parameter_hash,
                    ),
                )
        except sqlite3.DatabaseError:
            # Generation-change is the stable public failure; a disposable
            # cache cleanup failure must not mask it.
            pass

    @classmethod
    def _nodes_by_ids(
        cls,
        connection: sqlite3.Connection,
        generation_id: str,
        partition_key: str,
        node_ids: set[str],
        *,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> list[GraphNode]:
        if not node_ids:
            return []
        node_id_json = _json(sorted(node_ids))
        nodes: list[GraphNode] = []
        for row in connection.execute(
            """
            SELECT * FROM graph_nodes
            WHERE generation_id=? AND creator_account_id=?
              AND node_id IN (SELECT value FROM json_each(?))
            ORDER BY node_id
            """,
            (generation_id, partition_key, node_id_json),
        ):
            cls._check_budget(deadline, cancellation_check)
            nodes.append(_node(row))
        return nodes

    def _root_nodes(
        self,
        generation_id: str,
        partition_key: str,
        roots: set[str],
        *,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> list[GraphNode]:
        with self.database.read() as connection:
            self._install_progress_handler(connection, deadline, cancellation_check)
            nodes = self._nodes_by_ids(
                connection,
                generation_id,
                partition_key,
                roots,
                deadline=deadline,
                cancellation_check=cancellation_check,
            )
        self._check_budget(deadline, cancellation_check)
        return nodes

    @staticmethod
    def _install_progress_handler(
        connection: sqlite3.Connection,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> None:
        def interrupted() -> int:
            return int(
                time.monotonic() > deadline
                or (cancellation_check is not None and cancellation_check())
            )

        connection.set_progress_handler(interrupted, 1_000)

    @staticmethod
    def _check_budget(
        deadline: float, cancellation_check: CancellationCheck | None
    ) -> None:
        check_cancelled(cancellation_check)
        if time.monotonic() > deadline:
            raise GraphDeadlineExceeded("graph_deadline_exceeded")

    @classmethod
    def _translate_interrupted(
        cls,
        error: sqlite3.OperationalError,
        deadline: float,
        cancellation_check: CancellationCheck | None,
    ) -> None:
        if "interrupted" not in str(error).lower():
            return
        cls._check_budget(deadline, cancellation_check)
        raise GraphDeadlineExceeded("graph_deadline_exceeded") from None

    @staticmethod
    def _validated_nodes(
        partition_key: str, nodes: list[GraphNode]
    ) -> dict[str, GraphNode]:
        result: dict[str, GraphNode] = {}
        for node in nodes:
            if node.partition_key != partition_key:
                raise GraphStoreError("graph_account_mismatch")
            if node.node_id in result and result[node.node_id] != node:
                raise GraphStoreError("graph_node_identity_conflict")
            result[node.node_id] = node
        return dict(sorted(result.items()))

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
            if edge.edge_id in result and result[edge.edge_id] != edge:
                raise GraphStoreError("graph_edge_identity_conflict")
            result[edge.edge_id] = edge
        return dict(sorted(result.items()))

    @staticmethod
    def _validate_edge(edge: GraphEdge, nodes: dict[str, GraphNode]) -> None:
        source = nodes.get(edge.source_id)
        target = nodes.get(edge.target_id)
        if source is None or target is None:
            raise GraphReferentialIntegrityError("graph_endpoint_absent")
        if source.partition_key != edge.partition_key or target.partition_key != edge.partition_key:
            raise GraphReferentialIntegrityError("graph_account_mismatch")


class SQLiteGraphGenerationWriter:
    """Owner- and lease-fenced writer for one SQLite building generation."""

    def __init__(
        self,
        database: ProjectionsDatabase,
        *,
        generation_id: str,
        partition_key: str,
        owner: BuildOwner,
        lease_seconds: float,
    ) -> None:
        if not generation_id or not partition_key or lease_seconds <= 0:
            raise ValueError("graph_writer_input_invalid")
        self.database = database
        self._generation_id = generation_id
        self.partition_key = partition_key
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._chunk_size = 500
        self._worst_operation_seconds = 0.0
        self._lease_deadline_monotonic: float | None = (
            time.monotonic() + lease_seconds
        )
        self._write_gate = threading.Lock()
        self._state_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_error: BaseException | None = None
        self._lease_session_owner: int | None = None
        self._lease_session_depth = 0
        self._terminal_transition = False
        self._valid = True

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def upsert_node(self, node: GraphNode) -> UpsertOutcome:
        safe = safe_graph_node(node)
        if safe.partition_key != self.partition_key:
            raise GraphStoreError("graph_account_mismatch")
        with self._owned_transaction() as connection:
            existing_row = connection.execute(
                """
                SELECT * FROM graph_nodes
                WHERE generation_id=? AND creator_account_id=? AND node_id=?
                """,
                (self._generation_id, self.partition_key, safe.node_id),
            ).fetchone()
            existing = None if existing_row is None else _node(existing_row)
            if existing is not None and existing.kind != safe.kind:
                raise GraphStoreError("graph_node_identity_conflict")
            if existing == safe:
                return "unchanged"
            incident = []
            if existing is not None:
                incident = connection.execute(
                    """
                    SELECT * FROM graph_edges
                    WHERE generation_id=? AND creator_account_id=?
                      AND (source_id=? OR target_id=?)
                    ORDER BY edge_id
                    """,
                    (
                        self._generation_id,
                        self.partition_key,
                        safe.node_id,
                        safe.node_id,
                    ),
                ).fetchall()
                connection.execute(
                    """
                    DELETE FROM graph_nodes
                    WHERE generation_id=? AND creator_account_id=? AND node_id=?
                    """,
                    (self._generation_id, self.partition_key, safe.node_id),
                )
            connection.execute(
                """
                INSERT INTO graph_nodes (
                    generation_id, creator_account_id, node_id, kind,
                    occurred_at, properties_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                _node_parameters(self._generation_id, safe),
            )
            for row in incident:
                connection.execute(
                    """
                    INSERT INTO graph_edges (
                        generation_id,creator_account_id,edge_id,source_id,
                        target_id,relation,occurred_at,sequence,properties_json
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    tuple(row[key] for key in (
                        "generation_id","creator_account_id","edge_id","source_id",
                        "target_id","relation","occurred_at","sequence","properties_json"
                    )),
                )
            return "created" if existing is None else "updated"

    def upsert_edge(self, edge: GraphEdge) -> UpsertOutcome:
        safe = safe_graph_edge(edge)
        if safe.partition_key != self.partition_key:
            raise GraphStoreError("graph_account_mismatch")
        with self._owned_transaction() as connection:
            endpoint_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM graph_nodes
                    WHERE generation_id=? AND creator_account_id=?
                      AND node_id IN (SELECT value FROM json_each(?))
                    """,
                    (
                        self._generation_id,
                        self.partition_key,
                        _json(sorted({safe.source_id, safe.target_id})),
                    ),
                ).fetchone()[0]
            )
            expected_count = len({safe.source_id, safe.target_id})
            if endpoint_count != expected_count:
                raise GraphReferentialIntegrityError("graph_endpoint_absent")
            existing_row = connection.execute(
                """
                SELECT * FROM graph_edges
                WHERE generation_id=? AND creator_account_id=? AND edge_id=?
                """,
                (self._generation_id, self.partition_key, safe.edge_id),
            ).fetchone()
            existing = None if existing_row is None else _edge(existing_row)
            if existing is not None and (
                existing.source_id,
                existing.target_id,
                existing.relation,
            ) != (safe.source_id, safe.target_id, safe.relation):
                raise GraphStoreError("graph_edge_identity_conflict")
            if existing == safe:
                return "unchanged"
            if existing is not None:
                connection.execute(
                    """
                    DELETE FROM graph_edges
                    WHERE generation_id=? AND creator_account_id=? AND edge_id=?
                    """,
                    (self._generation_id, self.partition_key, safe.edge_id),
                )
            connection.execute(
                """
                INSERT INTO graph_edges (
                    generation_id, creator_account_id, edge_id, source_id,
                    target_id, relation, occurred_at, sequence, properties_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _edge_parameters(self._generation_id, safe),
            )
            return "created" if existing is None else "updated"

    def replace(self, *, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
        with self.lease_session():

            def keepalive() -> None:
                self._check_heartbeat()

            safe_nodes, safe_edges = safe_graph_records(
                nodes, edges, check=keepalive
            )
            node_map: dict[str, GraphNode] = {}
            for node in safe_nodes:
                keepalive()
                if node.partition_key != self.partition_key:
                    raise GraphStoreError("graph_account_mismatch")
                existing = node_map.get(node.node_id)
                if existing is not None and existing != node:
                    raise GraphStoreError("graph_node_identity_conflict")
                node_map[node.node_id] = node
            edge_map: dict[str, GraphEdge] = {}
            for edge in safe_edges:
                keepalive()
                if edge.partition_key != self.partition_key:
                    raise GraphStoreError("graph_account_mismatch")
                SQLiteGraphReader._validate_edge(edge, node_map)
                existing = edge_map.get(edge.edge_id)
                if existing is not None and existing != edge:
                    raise GraphStoreError("graph_edge_identity_conflict")
                edge_map[edge.edge_id] = edge

            ordered_nodes = list(node_map.values())
            ordered_nodes.sort(key=lambda value: value.node_id)
            keepalive()
            ordered_edges = list(edge_map.values())
            ordered_edges.sort(key=lambda value: value.edge_id)
            keepalive()

            with self._owned_transaction() as connection:
                connection.execute(
                    "DELETE FROM graph_edges WHERE generation_id=? AND creator_account_id=?",
                    (self._generation_id, self.partition_key),
                )
                connection.execute(
                    "DELETE FROM graph_nodes WHERE generation_id=? AND creator_account_id=?",
                    (self._generation_id, self.partition_key),
                )
                connection.execute(
                    "DELETE FROM graph_partition_stats WHERE generation_id=? AND creator_account_id=?",
                    (self._generation_id, self.partition_key),
                )

            node_start = 0
            while node_start < len(ordered_nodes):
                keepalive()
                chunk_size = self._chunk_size
                parameter_started = time.monotonic()
                node_parameters = [
                    _trusted_node_parameters(self._generation_id, item)
                    for item in ordered_nodes[node_start : node_start + chunk_size]
                ]
                self._record_operation_duration(time.monotonic() - parameter_started)
                transaction_started = time.monotonic()
                with self._owned_transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO graph_nodes (
                            generation_id, creator_account_id, node_id, kind,
                            occurred_at, properties_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        node_parameters,
                    )
                self._record_operation_duration(time.monotonic() - transaction_started)
                node_start += chunk_size
                self._wait_for_heartbeat(
                    max(0.001, min(0.1, self._lease_seconds / 5))
                )

            edge_start = 0
            while edge_start < len(ordered_edges):
                keepalive()
                chunk_size = self._chunk_size
                parameter_started = time.monotonic()
                edge_parameters = [
                    _trusted_edge_parameters(self._generation_id, item)
                    for item in ordered_edges[edge_start : edge_start + chunk_size]
                ]
                self._record_operation_duration(time.monotonic() - parameter_started)
                transaction_started = time.monotonic()
                with self._owned_transaction() as connection:
                    connection.executemany(
                        """
                        INSERT INTO graph_edges (
                            generation_id, creator_account_id, edge_id, source_id,
                            target_id, relation, occurred_at, sequence, properties_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        edge_parameters,
                    )
                self._record_operation_duration(time.monotonic() - transaction_started)
                edge_start += chunk_size
                self._wait_for_heartbeat(
                    max(0.001, min(0.1, self._lease_seconds / 5))
                )
            self.refresh()

    def _record_operation_duration(self, elapsed: float) -> None:
        self._worst_operation_seconds = max(
            elapsed,
            self._worst_operation_seconds * 0.75,
        )
        target_duration = self._lease_seconds / 5
        if elapsed > target_duration and self._chunk_size > 1:
            scaled = max(1, int(self._chunk_size * target_duration / elapsed))
            self._chunk_size = min(self._chunk_size - 1, scaled)

    def _remaining_lease_seconds(self) -> float:
        with self._state_lock:
            deadline = self._lease_deadline_monotonic
        if deadline is None:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    def _lease_wait_hint_seconds(self) -> float:
        remaining = self._remaining_lease_seconds()
        if remaining <= 0:
            return min(0.25, max(0.01, self._lease_seconds / 2))
        return min(0.25, max(0.01, remaining / 2))

    def _wait_for_heartbeat(self, interval: float) -> None:
        thread = self._heartbeat_thread
        if thread is None:
            return
        deadline = time.monotonic() + interval
        initial = self._remaining_lease_seconds()
        while self._remaining_lease_seconds() <= initial:
            self._check_heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.001, remaining))

    @contextmanager
    def lease_session(self):
        thread_id = threading.get_ident()
        with self._state_lock:
            if self._lease_session_depth:
                if self._lease_session_owner != thread_id:
                    raise GraphStoreError("graph_lease_session_busy")
                self._lease_session_depth += 1
                nested = True
            else:
                self._lease_session_owner = thread_id
                self._lease_session_depth = 1
                nested = False
        if nested:
            try:
                self._check_heartbeat()
                yield
            finally:
                with self._state_lock:
                    self._lease_session_depth -= 1
            return

        operation_error: BaseException | None = None
        started = False
        try:
            self.refresh()
            with self._state_lock:
                self._terminal_transition = False
            self._start_heartbeat()
            started = True
            self._check_heartbeat()
            yield
        except BaseException as error:
            operation_error = error
            raise
        finally:
            heartbeat_error = self._stop_heartbeat() if started else None
            with self._state_lock:
                self._lease_session_depth = 0
                self._lease_session_owner = None
            if operation_error is None and heartbeat_error is not None:
                self._check_heartbeat()

    def _start_heartbeat(self) -> None:
        stop = threading.Event()
        ready = threading.Event()
        thread = threading.Thread(
            target=self._heartbeat_worker,
            args=(stop, ready),
            name=f"graph-lease-{self._generation_id[:8]}",
            daemon=False,
        )
        with self._state_lock:
            if self._heartbeat_thread is not None:
                raise GraphStoreError("graph_heartbeat_already_running")
            self._heartbeat_error = None
            self._heartbeat_stop = stop
            self._heartbeat_thread = thread
        thread.start()
        ready.wait()

    def _stop_heartbeat(self) -> BaseException | None:
        with self._state_lock:
            thread = self._heartbeat_thread
            stop = self._heartbeat_stop
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._state_lock:
            self._heartbeat_thread = None
            self._heartbeat_stop = None
            return self._heartbeat_error

    def _quiesce_heartbeat_for_terminal_transition(self) -> None:
        with self._state_lock:
            self._terminal_transition = True
            stop = self._heartbeat_stop
            thread = self._heartbeat_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._state_lock:
            self._heartbeat_thread = None
            self._heartbeat_stop = None

    def _heartbeat_worker(
        self,
        stop: threading.Event,
        ready: threading.Event,
    ) -> None:
        connection: sqlite3.Connection | None = None
        interval = max(0.001, min(0.1, self._lease_seconds / 5))
        retry_interval = min(0.01, interval / 4)
        try:
            connection = self.database.connect()
            connection.execute(
                f"PRAGMA busy_timeout={max(1, int(retry_interval * 1_000))}"
            )
            self._renew_on_connection(connection)
            ready.set()
            while not stop.wait(interval):
                while not stop.is_set():
                    try:
                        renewed = self._renew_on_connection(
                            connection,
                            nonblocking=True,
                        )
                    except sqlite3.OperationalError as error:
                        if "locked" not in str(error).lower():
                            raise
                        renewed = False
                    if renewed:
                        break
                    if stop.wait(retry_interval):
                        break
        except BaseException as error:
            with self._state_lock:
                if not self._terminal_transition:
                    self._heartbeat_error = error
                    self._valid = False
            stop.set()
            ready.set()
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                connection.close()

    def _check_heartbeat(self) -> None:
        with self._state_lock:
            error = self._heartbeat_error
            valid = self._valid
        if error is not None:
            if isinstance(error, GraphStoreError):
                raise error
            raise GraphStoreError("graph_generation_ownership_lost") from error
        if not valid:
            raise GraphStoreError("graph_writer_invalid")

    def refresh(self) -> None:
        self._check_heartbeat()
        connection = self.database.connect()
        try:
            self._renew_on_connection(connection)
        finally:
            connection.close()

    def write_stats(
        self,
        *,
        source_revision: int,
        node_count: int,
        edge_count: int,
        graph_digest: str,
    ) -> None:
        with self._owned_transaction() as connection:
            connection.execute(
                """
                INSERT INTO graph_partition_stats (
                    generation_id, creator_account_id, source_revision,
                    node_count, edge_count, graph_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self._generation_id,
                    self.partition_key,
                    source_revision,
                    node_count,
                    edge_count,
                    graph_digest,
                ),
            )

    def validate(self) -> str:
        from app.analytics.sqlite_projection_store import recompute_generation

        with self.lease_session():
            self._check_heartbeat()
            with self._write_gate:
                self._check_heartbeat()
                self._quiesce_heartbeat_for_terminal_transition()
                connection = self.database.connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._renew_owned(connection)

                    def keepalive() -> None:
                        self._check_heartbeat()

                    values = recompute_generation(
                        connection,
                        self._generation_id,
                        check=keepalive,
                    )
                    keepalive()
                    now = _now()
                    updated = connection.execute(
                        """
                        UPDATE projection_generations
                        SET status='validated', projection_digest=?, graph_digest=?,
                            node_count=?, edge_count=?, validated_at=?, lease_expires_at=?
                        WHERE generation_id=? AND creator_account_id=?
                          AND status='building' AND owner_id=? AND owner_pid=?
                          AND owner_process_started_at=? AND owner_instance_nonce=?
                          AND owner_capability_digest=?
                        """,
                        (
                            values["projection_digest"],
                            values["graph_digest"],
                            values["node_count"],
                            values["edge_count"],
                            _timestamp(now),
                            _timestamp(now + timedelta(seconds=self._lease_seconds)),
                            self._generation_id,
                            self.partition_key,
                            self._owner.owner_id,
                            self._owner.pid,
                            self._owner.process_started_at,
                            self._owner.instance_nonce,
                            self._owner.capability_digest,
                        ),
                    )
                    if updated.rowcount != 1:
                        self._invalidate_ownership()
                        raise GraphStoreError("graph_generation_ownership_lost")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            with self._state_lock:
                self._valid = False
                self._lease_deadline_monotonic = None
            return str(values["graph_digest"])

    def discard(self) -> None:
        self._check_heartbeat()
        now = _now()
        with self._write_gate:
            self._quiesce_heartbeat_for_terminal_transition()
            with self.database.transaction() as connection:
                updated = connection.execute(
                    """
                    UPDATE projection_generations SET status='retired', retired_at=?
                    WHERE generation_id=? AND creator_account_id=?
                      AND status='building' AND owner_id=? AND owner_pid=?
                      AND owner_process_started_at=? AND owner_instance_nonce=?
                      AND owner_capability_digest=?
                    """,
                    (
                        _timestamp(now),
                        self._generation_id,
                        self.partition_key,
                        self._owner.owner_id,
                        self._owner.pid,
                        self._owner.process_started_at,
                        self._owner.instance_nonce,
                        self._owner.capability_digest,
                    ),
                )
                if updated.rowcount != 1:
                    self._invalidate_ownership()
                    raise GraphStoreError("graph_generation_ownership_lost")
        with self._state_lock:
            self._valid = False
            self._lease_deadline_monotonic = None

    @contextmanager
    def _owned_transaction(self):
        self._check_heartbeat()
        connection = self.database.connect()
        try:
            with self._write_gate:
                self._check_heartbeat()
                connection.execute(
                    f"PRAGMA busy_timeout={max(1, int(self._lease_wait_hint_seconds() * 1_000))}"
                )
                connection.execute("BEGIN IMMEDIATE")
                self._renew_owned(connection)
                try:
                    yield connection
                    self._check_heartbeat()
                    self._renew_owned(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        finally:
            connection.close()
        self._check_heartbeat()

    def _renew_on_connection(
        self,
        connection: sqlite3.Connection,
        *,
        nonblocking: bool = False,
    ) -> bool:
        acquired = self._write_gate.acquire(blocking=not nonblocking)
        if not acquired:
            return False
        started = time.monotonic()
        try:
            connection.execute(
                f"PRAGMA busy_timeout={max(1, int(self._lease_wait_hint_seconds() * 1_000))}"
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._renew_owned(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            self._write_gate.release()
        self._record_operation_duration(time.monotonic() - started)
        return True

    def _renew_owned(self, connection: sqlite3.Connection) -> None:
        now = _now()
        started = time.monotonic()
        updated = connection.execute(
            """
            UPDATE projection_generations SET lease_expires_at=?
            WHERE generation_id=? AND creator_account_id=? AND status='building'
              AND owner_id=? AND owner_pid=?
              AND owner_process_started_at=? AND owner_instance_nonce=?
              AND owner_capability_digest=?
            """,
            (
                _timestamp(now + timedelta(seconds=self._lease_seconds)),
                self._generation_id,
                self.partition_key,
                self._owner.owner_id,
                self._owner.pid,
                self._owner.process_started_at,
                self._owner.instance_nonce,
                self._owner.capability_digest,
            ),
        )
        if updated.rowcount != 1:
            self._invalidate_ownership()
            raise GraphStoreError("graph_generation_ownership_lost")
        with self._state_lock:
            self._lease_deadline_monotonic = started + self._lease_seconds

    def _invalidate_ownership(self) -> None:
        with self._state_lock:
            self._valid = False
            self._lease_deadline_monotonic = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("graph_time_timezone_required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _trusted_node_parameters(generation_id: str, node: GraphNode) -> tuple[Any, ...]:
    return (
        generation_id,
        node.partition_key,
        node.node_id,
        node.kind.value,
        _timestamp(node.occurred_at),
        _json(node.properties),
    )


def _node_parameters(generation_id: str, node: GraphNode) -> tuple[Any, ...]:
    return _trusted_node_parameters(generation_id, safe_graph_node(node))


def _trusted_edge_parameters(generation_id: str, edge: GraphEdge) -> tuple[Any, ...]:
    return (
        generation_id,
        edge.partition_key,
        edge.edge_id,
        edge.source_id,
        edge.target_id,
        edge.relation.value,
        _timestamp(edge.occurred_at),
        edge.sequence,
        _json(edge.properties),
    )


def _edge_parameters(generation_id: str, edge: GraphEdge) -> tuple[Any, ...]:
    return _trusted_edge_parameters(generation_id, safe_graph_edge(edge))


def _node(row: sqlite3.Row) -> GraphNode:
    return GraphNode(
        node_id=row["node_id"],
        partition_key=row["creator_account_id"],
        kind=GraphNodeKind(row["kind"]),
        occurred_at=(
            None if row["occurred_at"] is None else datetime.fromisoformat(row["occurred_at"])
        ),
        properties=json.loads(row["properties_json"]),
    )


def _edge(row: sqlite3.Row) -> GraphEdge:
    return GraphEdge(
        edge_id=row["edge_id"],
        partition_key=row["creator_account_id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        relation=GraphRelation(row["relation"]),
        occurred_at=(
            None if row["occurred_at"] is None else datetime.fromisoformat(row["occurred_at"])
        ),
        sequence=(None if row["sequence"] is None else int(row["sequence"])),
        properties=json.loads(row["properties_json"]),
    )


def _graph_digest(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    check: Callable[[], None] | None = None,
) -> str:
    return graph_content_digest(nodes, edges, check=check)


def _time_in_window(
    value: str | None,
    start: datetime,
    end: datetime,
    include_timeless: bool,
) -> bool:
    if value is None:
        return include_timeless
    parsed = datetime.fromisoformat(value)
    return start <= parsed <= end
