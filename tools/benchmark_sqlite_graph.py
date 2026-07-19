"""Synthetic witnessed-generation SQLite graph benchmark.

No platform identifiers or content are used.  Fixture creation, durable
generation publication, bounded queries, and retained-history checks are
measured independently.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.analytics.graph_identity import graph_id
from app.analytics.graph_privacy import graph_content_digest
from app.analytics.graph_store import InMemoryGraphRepository
from app.analytics.identity import canonical_identity
from app.analytics.opaque_refs import account_ref, validated_account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.projection_store import projection_content_digest
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphProjectionSummary,
    GraphRelation,
    GraphTraversalBounds,
    RebuildArtifact,
)
from app.persistence.factory import create_canonical_repositories


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACCOUNT_ID = "synthetic-benchmark-account"
ROLLBACK_RETENTION = 1
_Result = TypeVar("_Result")


if sys.platform == "win32":
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    _get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    _get_current_process.restype = wintypes.HANDLE
    _get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    _get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    _get_process_memory_info.restype = wintypes.BOOL


def _resident_bytes() -> int:
    """Return current RSS without the large runtime distortion of tracemalloc."""

    if sys.platform == "win32":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = _get_current_process()
        if _get_process_memory_info(process, ctypes.byref(counters), counters.cb):
            return int(counters.working_set_size)
        raise OSError("benchmark_rss_unavailable")
    proc_statm = Path("/proc/self/statm")
    if proc_statm.exists():
        resident_pages = int(proc_statm.read_text(encoding="ascii").split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def _measure(callable_: Callable[[], _Result]) -> tuple[_Result, float, float]:
    peak_bytes = _resident_bytes()
    stop = threading.Event()

    def sample_resident_set() -> None:
        nonlocal peak_bytes
        while not stop.wait(0.01):
            peak_bytes = max(peak_bytes, _resident_bytes())

    sampler = threading.Thread(target=sample_resident_set, daemon=True)
    started = time.perf_counter()
    sampler.start()
    try:
        result = callable_()
        elapsed = time.perf_counter() - started
    finally:
        peak_bytes = max(peak_bytes, _resident_bytes())
        stop.set()
        sampler.join()
    return result, elapsed, peak_bytes / 1024 / 1024


def build_graph(size: int) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes = [
        GraphNode(
            node_id=graph_id(validated_account_ref(account_ref(ACCOUNT_ID)), "message", str(index)),
            partition_key=account_ref(ACCOUNT_ID),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=NOW + timedelta(seconds=index),
            properties={"source_ordinal": index, "character_count": 1},
        )
        for index in range(size)
    ]
    edges: list[GraphEdge] = []
    for index in range(size - 1):
        for distance in (1, 17):
            target = index + distance
            if target >= size:
                continue
            edges.append(
                GraphEdge(
                    edge_id=graph_id(validated_account_ref(account_ref(ACCOUNT_ID)),
                        "edge",
                        GraphRelation.PRECEDES.value,
                        str(index),
                        str(target),
                        "synthetic-benchmark",
                    ),
                    partition_key=account_ref(ACCOUNT_ID),
                    source_id=nodes[index].node_id,
                    target_id=nodes[target].node_id,
                    relation=GraphRelation.PRECEDES,
                    occurred_at=NOW + timedelta(seconds=target),
                    properties={
                        "scope": "message",
                        "interval_seconds": distance,
                    },
                )
            )
    return nodes, edges


def benchmark_in_memory_edge_upserts() -> dict[str, Any]:
    """Time O(1) mutable-generation edge inserts at three doubling sizes."""

    timings: dict[str, float] = {}
    for size in (500, 1_000, 2_000):
        samples: list[float] = []
        for sample in range(5):
            repository = InMemoryGraphRepository()
            writer = repository.begin_generation(
                account_ref(ACCOUNT_ID),
                source_revision=sample + 1,
                lease_seconds=120,
            )
            nodes = [
                GraphNode(
                    node_id=graph_id(validated_account_ref(account_ref(ACCOUNT_ID)),
                        "message",
                        f"memory-{size}-{sample}-{index}",
                    ),
                    partition_key=account_ref(ACCOUNT_ID),
                    kind=GraphNodeKind.MESSAGE,
                    occurred_at=NOW,
                    properties={"character_count": 1},
                )
                for index in range(size + 1)
            ]
            writer.replace(nodes=nodes, edges=[])
            edges = [
                GraphEdge(
                    edge_id=graph_id(validated_account_ref(account_ref(ACCOUNT_ID)),
                        "edge",
                        GraphRelation.PRECEDES.value,
                        nodes[index].node_id,
                        nodes[index + 1].node_id,
                        f"memory-{size}-{sample}-{index}",
                    ),
                    partition_key=account_ref(ACCOUNT_ID),
                    source_id=nodes[index].node_id,
                    target_id=nodes[index + 1].node_id,
                    relation=GraphRelation.PRECEDES,
                    occurred_at=NOW,
                    properties={"scope": "message"},
                )
                for index in range(size)
            ]
            gc.collect()
            cyclic_gc_enabled = gc.isenabled()
            if cyclic_gc_enabled:
                gc.disable()
            try:
                started = time.perf_counter()
                for edge in edges:
                    writer.upsert_edge(edge)
                samples.append(time.perf_counter() - started)
            finally:
                if cyclic_gc_enabled:
                    gc.enable()
        timings[str(size)] = round(statistics.median(samples), 6)
    ratios = {
        "500_to_1000": round(timings["1000"] / max(timings["500"], 1e-9), 3),
        "1000_to_2000": round(timings["2000"] / max(timings["1000"], 1e-9), 3),
    }
    return {
        "seconds": timings,
        "doubling_ratios": ratios,
        "max_doubling_ratio": 3.5,
        "subquadratic": all(value < 3.5 for value in ratios.values()),
    }


def _artifact(
    pipeline: AnalyticsPipeline,
    account,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    projection_generation: int,
) -> RebuildArtifact:
    base = pipeline._build(
        ACCOUNT_ID,
        account,
        projection_generation=projection_generation,
    )
    summary = GraphProjectionSummary(
        account_ref=account_ref(ACCOUNT_ID),
        source_revision=account.view_revision,
        node_count=len(nodes),
        edge_count=len(edges),
        node_counts_by_kind=dict(Counter(item.kind.value for item in nodes)),
        edge_counts_by_relation=dict(
            Counter(item.relation.value for item in edges)
        ),
    )
    projection = base.projection.model_copy(
        update={
            "graph": summary,
            "graph_digest": graph_content_digest(nodes, edges),
            "projection_digest": "sha256:" + "0" * 64,
        }
    )
    projection = projection.model_copy(
        update={"projection_digest": projection_content_digest(projection)}
    )
    return RebuildArtifact(projection=projection, nodes=nodes, edges=edges)


def run(path: Path, size: int, *, retention_updates: int = 2) -> dict[str, Any]:
    (nodes, edges), fixture_seconds, fixture_peak = _measure(
        lambda: build_graph(size)
    )

    with tempfile.TemporaryDirectory() as authority_directory:
        canonical_path = Path(authority_directory) / "canonical.sqlite3"

        def build_and_publish():
            repositories = create_canonical_repositories(
                "sqlite", canonical_path=canonical_path
            )
            assert repositories.database is not None
            with repositories.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO account_read_models VALUES (?, 1)",
                    (ACCOUNT_ID,),
                )

            def read_identity(account_id: str):
                if not repositories.ingestion.account_exists(account_id):
                    return None
                return canonical_identity(
                    repositories.ingestion.account_read_model(account_id)
                )

            store = SQLiteAnalyticsProjectionStore(
                path,
                activation=repositories.projection_activation,
                canonical_identity_reader=read_identity,
                rollback_retention=ROLLBACK_RETENTION,
                gc_batch_size=8,
            )
            pipeline = AnalyticsPipeline(
                repositories.ingestion,
                projections=store,
                graph=store.graph,
            )
            account = repositories.ingestion.account_read_model(ACCOUNT_ID)
            artifact = _artifact(
                pipeline,
                account,
                nodes,
                edges,
                projection_generation=1,
            )
            changed = store.replace_artifact(
                artifact,
                creator_account_id=ACCOUNT_ID,
                canonical_identity=canonical_identity(account),
            )
            if not changed:
                raise RuntimeError("benchmark_generation_not_published")
            return repositories, store, pipeline

        (repositories, store, pipeline), build_seconds, build_peak = _measure(
            build_and_publish
        )

        def publish_updates() -> None:
            assert repositories.database is not None
            for revision in range(2, retention_updates + 2):
                with repositories.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE account_read_models SET view_revision=?
                        WHERE creator_account_id=?
                        """,
                        (revision, ACCOUNT_ID),
                    )
                account = repositories.ingestion.account_read_model(ACCOUNT_ID)
                artifact = _artifact(
                    pipeline,
                    account,
                    nodes,
                    edges,
                    projection_generation=revision,
                )
                if not store.replace_artifact(
                    artifact,
                    creator_account_id=ACCOUNT_ID,
                    canonical_identity=canonical_identity(account),
                ):
                    raise RuntimeError("benchmark_update_not_published")

        _, update_seconds, update_peak = _measure(publish_updates)

        max_retained_generations = 1 + ROLLBACK_RETENTION
        with store.database.read() as connection:
            generation_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM projection_generations"
                ).fetchone()[0]
            )
            retained_node_rows = int(
                connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
            )
            retained_edge_rows = int(
                connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            )
        retention_within_bound = (
            generation_rows <= max_retained_generations
            and retained_node_rows <= len(nodes) * max_retained_generations
            and retained_edge_rows <= len(edges) * max_retained_generations
        )

        def bounded_queries():
            neighborhood = store.graph.neighborhood(
                account_ref(ACCOUNT_ID),
                nodes[size // 2].node_id,
                bounds=GraphTraversalBounds(
                    creator_account_id=account_ref(ACCOUNT_ID),
                    start_time=NOW,
                    end_time=NOW + timedelta(seconds=size),
                    max_hops=4,
                    max_results=500,
                    max_visited=2_000,
                    max_edges_examined=8_000,
                    wall_clock_ms=30_000,
                ),
            )
            neighborhood_materialized_rows = store.graph.last_materialized_rows
            centrality = store.graph.compute_centrality(
                account_ref(ACCOUNT_ID),
                algorithm="degree",
                bounds=GraphAlgorithmBounds(
                    creator_account_id=account_ref(ACCOUNT_ID),
                    start_time=NOW,
                    end_time=NOW + timedelta(seconds=size),
                    max_hops=5,
                    max_nodes=2_000,
                    max_edges=8_000,
                    root_node_id=nodes[size // 2].node_id,
                    wall_clock_ms=30_000,
                ),
                seed=1729,
            )
            centrality_materialized_rows = store.graph.last_materialized_rows
            return (
                neighborhood,
                centrality,
                neighborhood_materialized_rows,
                centrality_materialized_rows,
            )

        (
            (
                neighborhood,
                centrality,
                neighborhood_materialized_rows,
                centrality_materialized_rows,
            ),
            query_seconds,
            query_peak,
        ) = _measure(bounded_queries)
        materialization_guard = (
            neighborhood_materialized_rows <= 18_001
            and centrality_materialized_rows <= 10_001
        )

    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "fixture_seconds": round(fixture_seconds, 6),
        "fixture_peak_mebibytes": round(fixture_peak, 3),
        "build_seconds": round(build_seconds, 6),
        "build_peak_mebibytes": round(build_peak, 3),
        "incremental_update_seconds": round(update_seconds, 6),
        "incremental_update_peak_mebibytes": round(update_peak, 3),
        "bounded_query_seconds": round(query_seconds, 6),
        "query_peak_mebibytes": round(query_peak, 3),
        "neighborhood_nodes": len(neighborhood.nodes),
        "centrality_nodes": centrality.node_count,
        "neighborhood_materialized_rows": neighborhood_materialized_rows,
        "centrality_materialized_rows": centrality_materialized_rows,
        "materialization_row_guard": materialization_guard,
        "truncated": neighborhood.truncated or centrality.truncated,
        "retention_updates": retention_updates,
        "retained_generations": generation_rows,
        "retained_node_rows": retained_node_rows,
        "retained_edge_rows": retained_edge_rows,
        "max_retained_generations": max_retained_generations,
        "incremental_retention_guard": retention_within_bound,
        "memory_metric": "process_resident_set_peak",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=5_000)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--retention-updates", type=int, default=2)
    parser.add_argument(
        "--compare-doubling",
        action="store_true",
        help="also run twice the size and reject near-quadratic growth",
    )
    parser.add_argument("--max-doubling-ratio", type=float, default=3.5)
    arguments = parser.parse_args()
    if arguments.size < 100:
        raise SystemExit("--size must be at least 100")
    if arguments.retention_updates < 2:
        raise SystemExit("--retention-updates must be at least 2")
    if arguments.compare_doubling and arguments.path is not None:
        raise SystemExit("--path cannot be combined with --compare-doubling")
    if arguments.max_doubling_ratio <= 0 or arguments.max_doubling_ratio >= 4:
        raise SystemExit("--max-doubling-ratio must be between zero and four")
    if arguments.compare_doubling:
        in_memory = benchmark_in_memory_edge_upserts()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            smaller = run(
                root / "projections-1x.sqlite3",
                arguments.size,
                retention_updates=arguments.retention_updates,
            )
            larger = run(
                root / "projections-2x.sqlite3",
                arguments.size * 2,
                retention_updates=arguments.retention_updates,
            )
        ratio_keys = (
            "build_seconds",
            "incremental_update_seconds",
            "bounded_query_seconds",
            "build_peak_mebibytes",
            "incremental_update_peak_mebibytes",
            "query_peak_mebibytes",
        )
        ratios = {
            key: round(float(larger[key]) / max(float(smaller[key]), 1e-9), 3)
            for key in ratio_keys
        }
        result = {
            "smaller": smaller,
            "larger": larger,
            "doubling_ratios": ratios,
            "max_doubling_ratio": arguments.max_doubling_ratio,
            "subquadratic": all(
                value < arguments.max_doubling_ratio for value in ratios.values()
            ),
            "incremental_retention_guard": bool(
                smaller["incremental_retention_guard"]
                and larger["incremental_retention_guard"]
            ),
            "materialization_row_guard": bool(
                smaller["materialization_row_guard"]
                and larger["materialization_row_guard"]
            ),
            "in_memory_edge_upserts": in_memory,
        }
        print(json.dumps(result, sort_keys=True))
        if not result["incremental_retention_guard"]:
            raise SystemExit("generation retention exceeded configured bound")
        if not result["materialization_row_guard"]:
            raise SystemExit("query materialization exceeded bounded row budget")
        if not in_memory["subquadratic"]:
            raise SystemExit("in-memory edge upserts exceeded scaling guard")
        if not result["subquadratic"]:
            raise SystemExit("bounded graph benchmark exceeded scaling guard")
        return 0
    if arguments.path is not None:
        result = run(
            arguments.path,
            arguments.size,
            retention_updates=arguments.retention_updates,
        )
    else:
        with tempfile.TemporaryDirectory() as directory:
            result = run(
                Path(directory) / "projections.sqlite3",
                arguments.size,
                retention_updates=arguments.retention_updates,
            )
    in_memory = benchmark_in_memory_edge_upserts()
    result["in_memory_edge_upserts"] = in_memory
    print(json.dumps(result, sort_keys=True))
    if not result["incremental_retention_guard"]:
        raise SystemExit("generation retention exceeded configured bound")
    if not result["materialization_row_guard"]:
        raise SystemExit("query materialization exceeded bounded row budget")
    if not in_memory["subquadratic"]:
        raise SystemExit("in-memory edge upserts exceeded scaling guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
