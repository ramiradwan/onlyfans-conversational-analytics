from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import threading
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analytics import networkx_adapter
from app.analytics.database import ProjectionsDatabase
from app.analytics.graph_identity import graph_id
from app.analytics.graph_privacy import graph_content_digest
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.graph_store import (
    GraphReader,
    GraphStoreError,
    InMemoryGraphRepository,
    _SnapshotGraphStore,
)
from app.analytics.ownership import current_build_owner
from app.analytics.opaque_refs import account_ref, validated_account_ref
from app.analytics.sqlite_graph_store import (
    SQLiteGraphGenerationWriter,
    SQLiteGraphReader,
)
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphRelation,
    GraphTraversalBounds,
)


NOW = datetime.now(timezone.utc)


def sql_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def nid(
    label: str,
    *,
    partition: str = "account-a",
    kind: GraphNodeKind = GraphNodeKind.MESSAGE,
) -> str:
    return graph_id(validated_account_ref(account_ref(partition)), kind.value, label)


def node(
    label: str,
    *,
    partition: str = "account-a",
    kind: GraphNodeKind = GraphNodeKind.MESSAGE,
    occurred_at: datetime | None = NOW,
    node_id: str | None = None,
    character_count: int | None = None,
) -> GraphNode:
    properties: dict[str, object]
    if kind is GraphNodeKind.MESSAGE:
        properties = {
            "character_count": len(label) if character_count is None else character_count
        }
    elif kind is GraphNodeKind.TOPIC:
        properties = {"taxonomy_id": "support", "label": "Support"}
    else:
        properties = {}
    return GraphNode(
        node_id=node_id or nid(label, partition=partition, kind=kind),
        partition_key=account_ref(partition),
        kind=kind,
        occurred_at=occurred_at,
        properties=properties,
    )


def edge(
    label: str,
    source_id: str,
    target_id: str,
    *,
    partition: str = "account-a",
    relation: GraphRelation = GraphRelation.PRECEDES,
    occurred_at: datetime | None = NOW,
) -> GraphEdge:
    return GraphEdge(
        edge_id=graph_id(
            validated_account_ref(account_ref(partition)),
            "edge",
            relation.value,
            source_id,
            target_id,
            label,
        ),
        partition_key=account_ref(partition),
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        occurred_at=occurred_at,
        properties={"scope": "message"}
        if relation is GraphRelation.PRECEDES
        else {},
    )


def traversal_bounds(**updates) -> GraphTraversalBounds:
    values = {
        "account_ref": account_ref("account-a"),
        "start_time": NOW - timedelta(days=1),
        "end_time": NOW + timedelta(days=1),
        "max_hops": 4,
        "max_results": 20,
        "max_visited": 100,
    }
    values.update(updates)
    return GraphTraversalBounds(**values)


def algorithm_bounds(**updates) -> GraphAlgorithmBounds:
    values = {
        "account_ref": account_ref("account-a"),
        "start_time": NOW - timedelta(days=1),
        "end_time": NOW + timedelta(days=1),
        "max_hops": 4,
        "max_nodes": 20,
        "max_edges": 40,
    }
    values.update(updates)
    return GraphAlgorithmBounds(**values)


@dataclass
class GraphHarness:
    reader: GraphReader
    publish: object


def _memory_harness() -> GraphHarness:
    repository = InMemoryGraphRepository(rollback_retention=1)

    def publish(
        partition: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        revision: int,
    ) -> None:
        expected = repository.reader.active_generation_id(account_ref(partition))
        writer = repository.begin_generation(account_ref(partition), source_revision=revision)
        writer.replace(nodes=nodes, edges=edges)
        writer.validate()
        repository.activate(
            writer.generation_id,
            expected_active=expected,
            owner_token=writer._owner_token,
        )

    return GraphHarness(repository.reader, publish)


def _sqlite_harness(path: Path) -> GraphHarness:
    database = ProjectionsDatabase(path)
    active: dict[str, str] = {}
    reader = SQLiteGraphReader(
        database,
        active_generation_resolver=lambda partition, **_: active.get(partition),
    )
    owner = current_build_owner()
    epoch = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO projection_publication_epochs (
                publication_epoch, scheduler_owner_id,
                scheduler_capability_digest, state, opened_at
            ) VALUES (?, ?, ?, 'open', ?)
            """,
            (
                epoch,
                owner.owner_id,
                owner.capability_digest,
                sql_time(datetime.now(timezone.utc)),
            ),
        )

    def publish(
        partition: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        revision: int,
    ) -> None:
        partition_ref = account_ref(partition)
        generation_id = str(uuid4())
        previous = active.get(partition_ref)
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projection_generations (
                    generation_id, creator_account_id, status, schema_version,
                    build_version, canonical_revision, canonical_content_digest,
                    canonical_high_water_json, pipeline_revision,
                    pipeline_config_digest, pipeline_identity_digest,
                    expected_active_generation_id, expected_active_revision,
                    publication_epoch, owner_id, owner_pid,
                    owner_process_started_at, owner_instance_nonce,
                    owner_capability_digest, lease_expires_at, started_at
                ) VALUES (?, ?, 'building', 3, 'contract', ?, ?, '{}',
                          'contract', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    partition_ref,
                    revision,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64,
                    previous,
                    None if previous is None else revision - 1,
                    epoch,
                    owner.owner_id,
                    owner.pid,
                    owner.process_started_at,
                    owner.instance_nonce,
                    owner.capability_digest,
                    sql_time(datetime.now(timezone.utc) + timedelta(hours=1)),
                    sql_time(datetime.now(timezone.utc)),
                ),
            )
        writer = SQLiteGraphGenerationWriter(
            database,
            generation_id=generation_id,
            partition_key=partition_ref,
            owner=owner,
            lease_seconds=3600,
        )
        writer.replace(nodes=nodes, edges=edges)
        digest = graph_content_digest(nodes, edges)
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE projection_generations
                SET status='validated', projection_digest=?, graph_digest=?,
                    node_count=?, edge_count=?, validated_at=?
                WHERE generation_id=?
                """,
                (
                    "sha256:" + "4" * 64,
                    digest,
                    len(nodes),
                    len(edges),
                    sql_time(NOW),
                    generation_id,
                ),
            )
            connection.execute(
                """
                UPDATE projection_generations
                SET status='activation_pending', activation_intent_id=?,
                    witness_sequence=1
                WHERE generation_id=?
                """,
                (str(uuid4()), generation_id),
            )
            if previous is not None:
                connection.execute(
                    """
                    UPDATE projection_generations
                    SET status='retired', retired_at=? WHERE generation_id=?
                    """,
                    (sql_time(NOW), previous),
                )
            connection.execute(
                """
                UPDATE projection_generations
                SET status='active', activated_at=? WHERE generation_id=?
                """,
                (sql_time(NOW), generation_id),
            )
        active[partition_ref] = generation_id

    return GraphHarness(reader, publish)


@pytest.fixture(params=["memory", "sqlite"])
def graph_case(request, tmp_path: Path) -> GraphHarness:
    if request.param == "memory":
        return _memory_harness()
    return _sqlite_harness(tmp_path / "projections.sqlite3")


def publish(
    case: GraphHarness,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    partition: str = "account-a",
    revision: int = 1,
) -> None:
    case.publish(partition, nodes, edges, revision)  # type: ignore[operator]


def test_reader_is_read_only_and_writer_is_invalid_after_validation() -> None:
    repository = InMemoryGraphRepository()
    writer = repository.begin_generation(account_ref("account-a"), source_revision=1)
    first = node("first")
    assert writer.upsert_node(first) == "created"
    assert writer.upsert_node(first) == "unchanged"
    writer.validate()
    with pytest.raises(GraphStoreError, match="graph_writer_invalid"):
        writer.refresh()
    assert not hasattr(repository.reader, "upsert_node")
    assert not hasattr(repository.reader, "replace_partition")
    assert not hasattr(repository.reader, "clear_partition")


def test_expired_validated_memory_generation_cannot_activate_and_is_reclaimable() -> None:
    repository = InMemoryGraphRepository(rollback_retention=0)
    writer = repository.begin_generation(
        account_ref("account-a"),
        source_revision=1,
        owner_token="owner-token",
        lease_seconds=0.01,
    )
    writer.replace(nodes=[node("expired")], edges=[])
    writer.validate()
    generation = repository._required(writer.generation_id)
    generation.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(GraphStoreError, match="graph_generation_ownership_lost"):
        repository.activate(
            writer.generation_id,
            expected_active=None,
            owner_token="owner-token",
        )
    repository.reclaim_expired_generation(writer.generation_id)
    assert writer.generation_id not in repository._generations
    assert repository.reader.active_generation_id(account_ref("account-a")) is None


def test_partitioned_point_lookups_resist_cross_account_id_collision(
    graph_case: GraphHarness,
) -> None:
    collision_id = nid("collision")
    publish(
        graph_case,
        [node("a", node_id=collision_id, character_count=1)],
        [],
        partition="account-a",
    )
    publish(
        graph_case,
        [
            node(
                "b",
                partition="account-b",
                node_id=collision_id,
                character_count=2,
            )
        ],
        [],
        partition="account-b",
    )
    assert graph_case.reader.get_node(account_ref("account-a"), collision_id).properties == {  # type: ignore[union-attr]
        "character_count": 1
    }
    assert graph_case.reader.get_node(account_ref("account-b"), collision_id).properties == {  # type: ignore[union-attr]
        "character_count": 2
    }
    assert graph_case.reader.get_node(account_ref("account-c"), collision_id) is None


def test_canonical_account_equal_to_another_partition_ref_cannot_alias(
    graph_case: GraphHarness,
) -> None:
    account_b_ref = account_ref("account-b")
    b_node = node("account-b-only", partition="account-b")
    publish(graph_case, [b_node], [], partition="account-b")

    assert account_ref(account_b_ref) != account_b_ref
    assert graph_case.reader.get_node(account_ref(account_b_ref), b_node.node_id) is None
    assert graph_case.reader.nodes(account_ref(account_b_ref)) == []


def test_shortest_paths_are_deterministic_and_never_return_longer_paths(
    graph_case: GraphHarness,
) -> None:
    ids = {label: nid(label) for label in "abcde"}
    nodes = [node(label) for label in "abcde"]
    edges = [
        edge("01", ids["a"], ids["b"]),
        edge("02", ids["b"], ids["d"]),
        edge("03", ids["a"], ids["c"]),
        edge("04", ids["c"], ids["d"]),
        edge("05", ids["c"], ids["e"]),
        edge("06", ids["e"], ids["d"]),
    ]
    publish(graph_case, nodes, edges)
    result = graph_case.reader.find_paths(
        account_ref("account-a"),
        ids["a"],
        ids["d"],
        bounds=traversal_bounds(max_results=10),
    )
    assert len(result.paths) == 2
    assert {len(item.edge_ids) for item in result.paths} == {2}
    assert result.paths == sorted(result.paths, key=lambda item: (item.node_ids, item.edge_ids))


def test_degree_max_results_counts_edges_not_materialized_nodes(
    graph_case: GraphHarness,
) -> None:
    root = node("degree-root")
    left = node("degree-left")
    right = node("degree-right")
    publish(
        graph_case,
        [root, left, right],
        [
            edge("degree-left", root.node_id, left.node_id),
            edge("degree-right", root.node_id, right.node_id),
        ],
    )
    result = graph_case.reader.degree(
        account_ref("account-a"),
        root.node_id,
        bounds=traversal_bounds(max_results=2, max_visited=3),
    )
    assert result.degree == 2
    assert not result.truncated


def test_degree_examination_cap_is_truthful(
    graph_case: GraphHarness,
) -> None:
    root = node("degree-cap-root")
    leaves = [node(f"degree-cap-{index}") for index in range(3)]
    publish(
        graph_case,
        [root, *leaves],
        [edge(str(index), root.node_id, leaf.node_id) for index, leaf in enumerate(leaves)],
    )
    result = graph_case.reader.degree(
        account_ref("account-a"),
        root.node_id,
        bounds=traversal_bounds(
            max_results=10,
            max_edges_examined=1,
            max_visited=4,
        ),
    )
    assert result.degree == 1
    assert result.truncated


def test_path_max_results_is_path_count_not_frontier_node_cap(
    graph_case: GraphHarness,
) -> None:
    source, middle, target = node("path-source"), node("path-middle"), node("path-target")
    publish(
        graph_case,
        [source, middle, target],
        [
            edge("path-first", source.node_id, middle.node_id),
            edge("path-second", middle.node_id, target.node_id),
        ],
    )
    result = graph_case.reader.find_paths(
        account_ref("account-a"),
        source.node_id,
        target.node_id,
        bounds=traversal_bounds(max_results=1, max_visited=3),
    )
    assert len(result.paths) == 1
    assert len(result.paths[0].edge_ids) == 2
    assert not result.truncated


def test_parallel_edges_do_not_consume_traversal_queue_slots(
    graph_case: GraphHarness,
) -> None:
    source, target = node("parallel-source"), node("parallel-target")
    parallels = [
        edge(f"parallel-{index}", source.node_id, target.node_id)
        for index in range(10)
    ]
    publish(graph_case, [source, target], parallels)
    paths = graph_case.reader.find_paths(
        account_ref("account-a"),
        source.node_id,
        target.node_id,
        bounds=traversal_bounds(
            max_results=10,
            max_visited=2,
            max_queue=1,
            max_edges_examined=10,
        ),
    )
    assert len(paths.paths) == 10
    assert not paths.truncated


def test_continuation_past_hop_boundary_is_truthfully_truncated(
    graph_case: GraphHarness,
) -> None:
    root, middle, leaf = node("hop-root"), node("hop-middle"), node("hop-leaf")
    publish(
        graph_case,
        [root, middle, leaf],
        [
            edge("hop-first", root.node_id, middle.node_id),
            edge("hop-second", middle.node_id, leaf.node_id),
        ],
    )
    result = graph_case.reader.neighborhood(
        account_ref("account-a"),
        root.node_id,
        bounds=traversal_bounds(max_hops=1, max_results=2, max_visited=3),
    )
    assert result.truncated
    assert {item.node_id for item in result.nodes} == {root.node_id, middle.node_id}


def test_scope_filters_endpoints_and_edges_before_budget_with_timeless_opt_in(
    graph_case: GraphHarness,
) -> None:
    message_a = node("a")
    message_b = node("b")
    old = node("old", occurred_at=NOW - timedelta(days=10))
    timeless = node("timeless", occurred_at=None)
    topic = node("topic", kind=GraphNodeKind.TOPIC, occurred_at=NOW)
    edges = [
        edge("valid", message_a.node_id, message_b.node_id),
        edge("old", message_b.node_id, old.node_id),
        edge("timeless", message_b.node_id, timeless.node_id, occurred_at=None),
        edge(
            "topic",
            message_b.node_id,
            topic.node_id,
            relation=GraphRelation.MENTIONS_TOPIC,
        ),
    ]
    publish(graph_case, [message_a, message_b, old, timeless, topic], edges)
    bounds = traversal_bounds(
        max_edges_examined=1,
        node_kinds={GraphNodeKind.MESSAGE},
        edge_kinds={GraphRelation.PRECEDES},
    )
    result = graph_case.reader.neighborhood(
        account_ref("account-a"), message_b.node_id, bounds=bounds
    )
    assert [item.node_id for item in result.nodes] == sorted(
        [message_a.node_id, message_b.node_id]
    )
    assert not result.truncated
    included = graph_case.reader.neighborhood(
        account_ref("account-a"),
        message_b.node_id,
        bounds=bounds.model_copy(update={"include_timeless": True, "max_edges_examined": 2}),
    )
    assert {item.node_id for item in included.nodes} == {
        message_a.node_id,
        message_b.node_id,
        timeless.node_id,
    }


def test_truncation_is_false_when_exact_hop_boundary_exhausts_graph(
    graph_case: GraphHarness,
) -> None:
    left, right = node("left"), node("right")
    publish(graph_case, [left, right], [edge("only", left.node_id, right.node_id)])
    result = graph_case.reader.neighborhood(
        account_ref("account-a"),
        left.node_id,
        bounds=traversal_bounds(max_hops=1, max_results=2, max_visited=2),
    )
    assert not result.truncated
    assert {item.node_id for item in result.nodes} == {left.node_id, right.node_id}


def test_parallel_edges_are_preserved_and_algorithm_counts_are_truthful(
    graph_case: GraphHarness,
) -> None:
    left, right = node("left"), node("right")
    edges = [
        edge("parallel-a", left.node_id, right.node_id),
        edge("parallel-b", left.node_id, right.node_id),
    ]
    publish(graph_case, [left, right], edges)
    result = graph_case.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="degree",
        bounds=algorithm_bounds(max_nodes=2, max_edges=2),
        seed=17,
    )
    assert result.node_count == 2
    assert result.edge_count == 2
    assert result.source_edge_count == 2
    assert result.algorithm_edge_count == 2


def test_inflight_algorithm_rechecks_active_generation_in_both_backends(
    graph_case: GraphHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    left, right = node("inflight-left"), node("inflight-right")
    publish(
        graph_case,
        [left, right],
        [edge("inflight-edge", left.node_id, right.node_id)],
    )
    entered = threading.Event()
    release = threading.Event()
    original = networkx_adapter.BoundedNetworkXAlgorithms.centrality

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        networkx_adapter.BoundedNetworkXAlgorithms,
        "centrality",
        delayed,
    )
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(
                graph_case.reader.compute_centrality(
                    account_ref("account-a"),
                    algorithm="degree",
                    bounds=algorithm_bounds(max_nodes=2, max_edges=1),
                    seed=41,
                )
            )
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(timeout=2)
    publish(graph_case, [left], [], revision=2)
    release.set()
    thread.join(timeout=3)

    assert len(outcome) == 1
    assert isinstance(outcome[0], GraphStoreError)
    assert str(outcome[0]) == "graph_generation_changed"


def test_memory_reader_rechecks_cancellation_immediately_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _memory_harness()
    left, right = node("cancel-left"), node("cancel-right")
    publish(case, [left, right], [edge("cancel-edge", left.node_id, right.node_id)])
    cancelled = False
    original = _SnapshotGraphStore.degree

    def delayed(*args, **kwargs):
        nonlocal cancelled
        result = original(*args, **kwargs)
        cancelled = True
        return result

    monkeypatch.setattr(_SnapshotGraphStore, "degree", delayed)
    with pytest.raises(ProjectionBuildCancelled):
        case.reader.degree(
            account_ref("account-a"),
            left.node_id,
            bounds=traversal_bounds(max_results=1, max_visited=2),
            cancellation_check=lambda: cancelled,
        )


def test_exact_undirected_edge_cap_is_not_falsely_truncated(
    graph_case: GraphHarness,
) -> None:
    left, right = node("exact-left"), node("exact-right")
    publish(graph_case, [left, right], [edge("exact", left.node_id, right.node_id)])

    result = graph_case.reader.neighborhood(
        account_ref("account-a"),
        left.node_id,
        bounds=traversal_bounds(max_results=1, max_visited=2),
        direction="both",
    )

    assert len(result.edges) == 1
    assert not result.truncated


def test_caps_never_return_edges_without_both_selected_endpoints(
    graph_case: GraphHarness,
) -> None:
    root = node("root")
    leaves = [node(f"leaf-{index}") for index in range(10)]
    publish(
        graph_case,
        [root, *leaves],
        [edge(str(index), root.node_id, leaf.node_id) for index, leaf in enumerate(leaves)],
    )
    result = graph_case.reader.neighborhood(
        account_ref("account-a"),
        root.node_id,
        bounds=traversal_bounds(
            max_results=3,
            max_visited=3,
            max_queue=1,
            max_edges_examined=4,
        ),
    )
    selected = {item.node_id for item in result.nodes}
    assert result.truncated
    assert len(result.nodes) <= 3
    assert all(
        item.source_id in selected and item.target_id in selected
        for item in result.edges
    )


def test_root_must_satisfy_kind_and_time_scope_in_both_backends(
    graph_case: GraphHarness,
) -> None:
    root = node("timeless-root", occurred_at=None)
    publish(graph_case, [root], [])
    with pytest.raises(KeyError, match="graph_node_out_of_scope"):
        graph_case.reader.neighborhood(
            account_ref("account-a"),
            root.node_id,
            bounds=traversal_bounds(),
        )
    with pytest.raises(KeyError, match="graph_root_out_of_scope"):
        graph_case.reader.compute_centrality(
            account_ref("account-a"),
            algorithm="degree",
            bounds=algorithm_bounds(root_node_id=root.node_id),
            seed=1,
        )


def test_include_only_returns_out_of_scope_root_without_expanding(
    graph_case: GraphHarness,
) -> None:
    root = node("include-only-root", occurred_at=None)
    neighbor = node("include-only-neighbor")
    publish(
        graph_case,
        [root, neighbor],
        [edge("include-only-edge", root.node_id, neighbor.node_id)],
    )
    traversal = graph_case.reader.neighborhood(
        account_ref("account-a"),
        root.node_id,
        bounds=traversal_bounds(root_policy="include_only"),
    )
    assert [item.node_id for item in traversal.nodes] == [root.node_id]
    assert traversal.edges == []
    assert traversal.visited_count == 1

    centrality = graph_case.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="degree",
        bounds=algorithm_bounds(
            root_node_id=root.node_id,
            root_policy="include_only",
        ),
        seed=13,
    )
    assert centrality.node_count == 1
    assert centrality.edge_count == 0
    assert set(centrality.values) == {root.node_id}


def test_root_algorithm_exact_hop_and_edge_caps_are_not_false_truncation(
    graph_case: GraphHarness,
) -> None:
    left, right = node("algorithm-left"), node("algorithm-right")
    publish(
        graph_case,
        [left, right],
        [edge("algorithm-only", left.node_id, right.node_id)],
    )
    result = graph_case.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="degree",
        bounds=algorithm_bounds(
            root_node_id=left.node_id,
            max_hops=1,
            max_nodes=2,
            max_edges=1,
        ),
        seed=2,
    )
    assert result.node_count == 2
    assert result.source_edge_count == 1
    assert not result.truncated


def test_randomized_memory_sqlite_traversal_parity(tmp_path: Path) -> None:
    rng = random.Random(1945)
    for sample in range(20):
        memory = _memory_harness()
        sqlite = _sqlite_harness(tmp_path / f"parity-{sample}.sqlite3")
        labels = [f"random-{sample}-{index}" for index in range(rng.randint(3, 8))]
        nodes = [node(label) for label in labels]
        edges: list[GraphEdge] = []
        for left in range(len(nodes)):
            for right in range(left + 1, len(nodes)):
                for parallel in range(rng.randint(0, 2)):
                    edges.append(
                        edge(
                            f"random-edge-{sample}-{left}-{right}-{parallel}",
                            nodes[left].node_id,
                            nodes[right].node_id,
                        )
                    )
        publish(memory, nodes, edges)
        publish(sqlite, nodes, edges)
        max_results = rng.randint(1, max(1, len(edges)))
        bounds = traversal_bounds(
            max_hops=rng.randint(0, 3),
            max_results=max_results,
            max_visited=len(nodes),
            max_queue=len(nodes),
            max_edges_examined=max(1, len(edges)),
        )
        root = rng.choice(nodes)
        memory_neighborhood = memory.reader.neighborhood(
            account_ref("account-a"), root.node_id, bounds=bounds
        )
        sqlite_neighborhood = sqlite.reader.neighborhood(
            account_ref("account-a"), root.node_id, bounds=bounds
        )
        assert memory_neighborhood == sqlite_neighborhood, (
            sample,
            bounds.model_dump(),
            root.node_id,
            memory_neighborhood.model_dump(),
            sqlite_neighborhood.model_dump(),
        )
        memory_degree = memory.reader.degree(
            account_ref("account-a"), root.node_id, bounds=bounds
        )
        sqlite_degree = sqlite.reader.degree(
            account_ref("account-a"), root.node_id, bounds=bounds
        )
        assert memory_degree == sqlite_degree, (
            sample,
            bounds.model_dump(),
            root.node_id,
            memory_degree,
            sqlite_degree,
        )
        target = root
        memory_paths = memory.reader.find_paths(
            account_ref("account-a"),
            root.node_id,
            target.node_id,
            bounds=bounds,
            directed=False,
        )
        sqlite_paths = sqlite.reader.find_paths(
            account_ref("account-a"),
            root.node_id,
            target.node_id,
            bounds=bounds,
            directed=False,
        )
        assert memory_paths == sqlite_paths, (
            sample,
            bounds.model_dump(),
            root.node_id,
            target.node_id,
            memory_paths.model_dump(),
            sqlite_paths.model_dump(),
        )


@pytest.mark.parametrize(
    ("properties", "code"),
    [
        ({"unknown": 1}, "graph_property_unknown"),
        ({"character_count": {"nested": 1}}, "graph_property_invalid"),
        ({"character_count": [1]}, "graph_property_invalid"),
        ({"character_count": float("nan")}, "graph_property_invalid"),
    ],
)
def test_graph_models_reject_properties_with_fixed_non_disclosing_codes(
    properties: dict[str, object], code: str
) -> None:
    marker = "SYNTHETIC-REJECTED-GRAPH-ID-991"
    with pytest.raises(ValidationError) as captured:
        GraphNode(
            node_id=graph_id(validated_account_ref(account_ref("account-a")), "message", marker),
            partition_key=account_ref("account-a"),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=NOW,
            properties=properties,
        )
    assert code in str(captured.value)
    assert marker not in str(captured.value)


def test_offset_equivalent_timestamps_normalize_to_identical_graph_content() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))
    utc_node = node("offset-equivalent", occurred_at=NOW)
    offset_node = node(
        "offset-equivalent",
        occurred_at=NOW.astimezone(offset),
    )
    assert offset_node == utc_node
    assert offset_node.occurred_at is not None
    assert offset_node.occurred_at.tzinfo is timezone.utc
    assert graph_content_digest([offset_node], []) == graph_content_digest(
        [utc_node], []
    )


def test_graph_models_require_opaque_ids_and_aware_timestamps() -> None:
    marker = "SYNTHETIC-RAW-GRAPH-ID-992"
    with pytest.raises(ValidationError) as raw_error:
        GraphNode(
            node_id=marker,
            partition_key=account_ref("account-a"),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=NOW,
            properties={"character_count": 1},
        )
    assert "graph_id_invalid" in str(raw_error.value)
    assert marker not in str(raw_error.value)

    with pytest.raises(ValidationError, match="graph_time_timezone_required"):
        GraphNode(
            node_id=nid("naive"),
            partition_key=account_ref("account-a"),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=NOW.replace(tzinfo=None),
            properties={"character_count": 1},
        )


@pytest.mark.parametrize(
    ("occurred_at", "properties", "code"),
    [
        (NOW.replace(tzinfo=None), {"character_count": 1}, "graph_time_timezone_required"),
        (NOW, {"character_count": float("inf")}, "graph_property_invalid"),
    ],
)
def test_writer_boundaries_revalidate_finite_and_aware_models_in_both_backends(
    graph_case: GraphHarness,
    occurred_at: datetime,
    properties: dict[str, object],
    code: str,
) -> None:
    bypassed = GraphNode.model_construct(
        node_id=nid("bypassed-model"),
        partition_key=account_ref("account-a"),
        kind=GraphNodeKind.MESSAGE,
        occurred_at=occurred_at,
        properties=properties,
    )

    with pytest.raises(ValidationError, match=code):
        publish(graph_case, [bypassed], [], revision=1)
