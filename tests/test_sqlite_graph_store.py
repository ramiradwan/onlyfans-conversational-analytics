from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analytics import networkx_adapter
from app.analytics.database import ProjectionsDatabase
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.graph_identity import graph_id
from app.analytics.graph_privacy import graph_content_digest, safe_graph_records
from app.analytics.graph_store import GraphDeadlineExceeded, GraphStoreError
from app.analytics.ownership import current_build_owner
from app.analytics.opaque_refs import account_ref, validated_account_ref
from app.analytics.sqlite_graph_store import (
    SQLiteGraphGenerationWriter,
    SQLiteGraphReader,
)
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.models.analytics import (
    GraphAlgorithmBounds,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphRelation,
    GraphTraversalBounds,
)
from app.persistence.migrations import MigrationChecksumError
from app.persistence.private_files import _windows_acl_is_owner_only
from app.persistence.projection_activation import (
    InMemoryProjectionActivationRepository,
)


NOW = datetime.now(timezone.utc)


def sql_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def graph_node(label: str) -> GraphNode:
    return GraphNode(
        node_id=graph_id(validated_account_ref(account_ref("account-a")), "message", label),
        partition_key=account_ref("account-a"),
        kind=GraphNodeKind.MESSAGE,
        occurred_at=NOW,
        properties={"character_count": len(label)},
    )


def graph_edge(label: str, source_id: str, target_id: str) -> GraphEdge:
    return GraphEdge(
        edge_id=graph_id(validated_account_ref(account_ref("account-a")), "edge", GraphRelation.PRECEDES.value, source_id, target_id, label
        ),
        partition_key=account_ref("account-a"),
        source_id=source_id,
        target_id=target_id,
        relation=GraphRelation.PRECEDES,
        occurred_at=NOW,
        properties={"scope": "message"},
    )


class SQLiteGraphHarness:
    def __init__(self, path: Path) -> None:
        self.database = ProjectionsDatabase(path)
        self.owner = current_build_owner()
        self.account_ref = account_ref("account-a")
        self.epoch = str(uuid4())
        self.active: str | None = None
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projection_publication_epochs (
                    publication_epoch, scheduler_owner_id,
                    scheduler_capability_digest, state, opened_at
                ) VALUES (?, ?, ?, 'open', ?)
                """,
                (
                    self.epoch,
                    self.owner.owner_id,
                    self.owner.capability_digest,
                    sql_time(datetime.now(timezone.utc)),
                ),
            )
        self.reader = SQLiteGraphReader(
            self.database,
            active_generation_resolver=lambda account, **_: (
                self.active if account == self.account_ref else None
            ),
        )

    def begin_build(self, revision: int = 1) -> str:
        generation_id = str(uuid4())
        with self.database.transaction() as connection:
            previous_revision = None
            if self.active is not None:
                previous_revision = int(
                    connection.execute(
                        "SELECT canonical_revision FROM projection_generations WHERE generation_id=?",
                        (self.active,),
                    ).fetchone()[0]
                )
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
                ) VALUES (?, ?, 'building', 3, 'test', ?, ?, '{}',
                          'test', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    self.account_ref,
                    revision,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "sha256:" + "3" * 64,
                    self.active,
                    previous_revision,
                    self.epoch,
                    self.owner.owner_id,
                    self.owner.pid,
                    self.owner.process_started_at,
                    self.owner.instance_nonce,
                    self.owner.capability_digest,
                    sql_time(datetime.now(timezone.utc) + timedelta(hours=1)),
                    sql_time(datetime.now(timezone.utc)),
                ),
            )
        return generation_id

    def publish(
        self, nodes: list[GraphNode], edges: list[GraphEdge], revision: int = 1
    ) -> str:
        generation_id = self.begin_build(revision)
        writer = SQLiteGraphGenerationWriter(
            self.database,
            generation_id=generation_id,
            partition_key=self.account_ref,
            owner=self.owner,
            lease_seconds=3600,
        )
        writer.replace(nodes=nodes, edges=edges)
        digest = graph_content_digest(nodes, edges)
        with self.database.transaction() as connection:
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
                SET status='activation_pending', activation_intent_id=?, witness_sequence=?
                WHERE generation_id=?
                """,
                (str(uuid4()), revision, generation_id),
            )
            if self.active is not None:
                connection.execute(
                    """
                    UPDATE projection_generations SET status='retired', retired_at=?
                    WHERE generation_id=?
                    """,
                    (sql_time(NOW), self.active),
                )
            connection.execute(
                """
                UPDATE projection_generations SET status='active', activated_at=?
                WHERE generation_id=?
                """,
                (sql_time(NOW), generation_id),
            )
        self.active = generation_id
        return generation_id


def algorithm_bounds(**updates) -> GraphAlgorithmBounds:
    values = {
        "account_ref": account_ref("account-a"),
        "start_time": NOW - timedelta(days=1),
        "end_time": NOW + timedelta(days=1),
        "max_hops": 4,
        "max_nodes": 100,
        "max_edges": 200,
    }
    values.update(updates)
    return GraphAlgorithmBounds(**values)


def test_projection_database_uses_required_profile_and_lifecycle_schema(
    tmp_path: Path,
) -> None:
    database = ProjectionsDatabase(tmp_path / "projections.sqlite3")
    with database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "projection_generation_transition_monotonic",
            "projection_generation_identity_immutable",
            "projection_generation_delete_retired_only",
            "graph_metric_active_insert",
        } <= triggers


def test_direct_sql_topic_pair_requires_both_matching_fields(tmp_path: Path) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    generation_id = harness.begin_build()
    topic_id = graph_id(
        validated_account_ref(harness.account_ref), "topic", "direct-sql-topic"
    )
    for properties in (
        {"taxonomy_id": "support"},
        {"label": "Support"},
        {"taxonomy_id": "support", "label": None},
    ):
        with pytest.raises(sqlite3.IntegrityError, match="graph_property_invalid"):
            with harness.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO graph_nodes (
                        generation_id, creator_account_id, node_id, kind,
                        occurred_at, properties_json
                    ) VALUES (?, ?, ?, 'topic', ?, ?)
                    """,
                    (
                        generation_id,
                        harness.account_ref,
                        topic_id,
                        sql_time(NOW),
                        json.dumps(properties),
                    ),
                )


@pytest.mark.slow
@pytest.mark.parametrize("sample", range(3))
def test_50k_node_short_lease_build_stays_live_under_repeated_runs(
    tmp_path: Path,
    sample: int,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / f"short-lease-{sample}.sqlite3")
    nodes = [graph_node(f"short-lease-{sample}-{index:05d}") for index in range(50_000)]
    generation_id = harness.begin_build()
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE projection_generations SET lease_expires_at=? WHERE generation_id=?",
            (
                sql_time(datetime.now(timezone.utc) + timedelta(seconds=0.5)),
                generation_id,
            ),
        )
    writer = SQLiteGraphGenerationWriter(
        harness.database,
        generation_id=generation_id,
        partition_key=harness.account_ref,
        owner=harness.owner,
        lease_seconds=0.5,
    )
    writer.replace(nodes=nodes, edges=[])
    with harness.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0] == 50_000
    assert writer._heartbeat_thread is None
    assert not any(
        thread.name == f"graph-lease-{generation_id[:8]}"
        for thread in threading.enumerate()
    )
    assert harness.database.open_connection_count(harness.database.path) == 0


def test_expired_owner_renewal_and_reclaim_are_linearized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "renew-reclaim-race.sqlite3")
    generation_id = harness.begin_build()
    writer = SQLiteGraphGenerationWriter(
        harness.database,
        generation_id=generation_id,
        partition_key=harness.account_ref,
        owner=harness.owner,
        lease_seconds=30,
    )
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE projection_generations SET lease_expires_at=? WHERE generation_id=?",
            (
                sql_time(datetime.now(timezone.utc) - timedelta(seconds=1)),
                generation_id,
            ),
        )

    renewal_locked = threading.Event()
    allow_renewal = threading.Event()
    original_renew_owned = writer._renew_owned

    def paused_renewal(connection: sqlite3.Connection) -> None:
        renewal_locked.set()
        assert allow_renewal.wait(timeout=3)
        original_renew_owned(connection)

    monkeypatch.setattr(writer, "_renew_owned", paused_renewal)
    outcomes: dict[str, object] = {}

    def renew() -> None:
        try:
            writer.refresh()
            outcomes["renew"] = "won"
        except BaseException as error:
            outcomes["renew"] = error

    def reclaim() -> None:
        assert renewal_locked.wait(timeout=3)
        try:
            with harness.database.transaction() as connection:
                now = datetime.now(timezone.utc)
                updated = connection.execute(
                    """
                    UPDATE projection_generations
                    SET status='retired', retired_at=?
                    WHERE generation_id=? AND status='building'
                      AND owner_id=? AND owner_pid=?
                      AND owner_process_started_at=? AND owner_instance_nonce=?
                      AND owner_capability_digest=? AND lease_expires_at<=?
                    """,
                    (
                        sql_time(now),
                        generation_id,
                        harness.owner.owner_id,
                        harness.owner.pid,
                        harness.owner.process_started_at,
                        harness.owner.instance_nonce,
                        harness.owner.capability_digest,
                        sql_time(now),
                    ),
                )
            outcomes["reclaim"] = updated.rowcount
        except BaseException as error:
            outcomes["reclaim"] = error

    renewing = threading.Thread(target=renew)
    reclaiming = threading.Thread(target=reclaim)
    renewing.start()
    assert renewal_locked.wait(timeout=3)
    reclaiming.start()
    allow_renewal.set()
    renewing.join(timeout=3)
    reclaiming.join(timeout=3)
    assert not renewing.is_alive() and not reclaiming.is_alive()

    assert outcomes == {"renew": "won", "reclaim": 0}
    writer.upsert_node(graph_node("renewal-winner"))

    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE projection_generations SET lease_expires_at=? WHERE generation_id=?",
            (
                sql_time(datetime.now(timezone.utc) - timedelta(seconds=1)),
                generation_id,
            ),
        )
    reclaim_locked = threading.Event()
    release_reclaimer = threading.Event()

    def winning_reclaim() -> None:
        with harness.database.transaction() as connection:
            reclaim_locked.set()
            assert release_reclaimer.wait(timeout=3)
            now = datetime.now(timezone.utc)
            updated = connection.execute(
                """
                UPDATE projection_generations
                SET status='retired', retired_at=?
                WHERE generation_id=? AND status='building'
                  AND owner_id=? AND owner_pid=?
                  AND owner_process_started_at=? AND owner_instance_nonce=?
                  AND owner_capability_digest=? AND lease_expires_at<=?
                """,
                (
                    sql_time(now),
                    generation_id,
                    harness.owner.owner_id,
                    harness.owner.pid,
                    harness.owner.process_started_at,
                    harness.owner.instance_nonce,
                    harness.owner.capability_digest,
                    sql_time(now),
                ),
            )
        outcomes["winning_reclaim"] = updated.rowcount

    def late_renewal() -> None:
        assert reclaim_locked.wait(timeout=3)
        try:
            original_renew_owned_connection = harness.database.connect()
            try:
                original_renew_owned_connection.execute("BEGIN IMMEDIATE")
                original_renew_owned(original_renew_owned_connection)
                original_renew_owned_connection.commit()
                outcomes["late_renew"] = "won"
            finally:
                original_renew_owned_connection.close()
        except BaseException as error:
            outcomes["late_renew"] = error

    reclaimer = threading.Thread(target=winning_reclaim)
    late = threading.Thread(target=late_renewal)
    reclaimer.start()
    assert reclaim_locked.wait(timeout=3)
    late.start()
    release_reclaimer.set()
    reclaimer.join(timeout=3)
    late.join(timeout=3)
    assert not reclaimer.is_alive() and not late.is_alive()

    assert outcomes["winning_reclaim"] == 1
    assert isinstance(outcomes["late_renew"], GraphStoreError)
    with pytest.raises(GraphStoreError):
        writer.upsert_node(graph_node("late-old-owner"))
    with harness.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE generation_id=? AND node_id=?",
            (generation_id, graph_node("late-old-owner").node_id),
        ).fetchone()[0] == 0


def test_heartbeat_failure_stops_thread_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "heartbeat-error.sqlite3")
    generation_id = harness.begin_build()
    writer = SQLiteGraphGenerationWriter(
        harness.database,
        generation_id=generation_id,
        partition_key=harness.account_ref,
        owner=harness.owner,
        lease_seconds=30,
    )
    original_safe_records = safe_graph_records

    def revoke_during_validation(nodes, edges, *, check=None):
        with harness.database.transaction() as connection:
            connection.execute(
                """
                UPDATE projection_generations
                SET owner_capability_digest=?, lease_expires_at=?
                WHERE generation_id=? AND status='building'
                """,
                (
                    "sha256:" + "f" * 64,
                    sql_time(datetime.now(timezone.utc) - timedelta(seconds=1)),
                    generation_id,
                ),
            )
        assert check is not None
        check()
        return original_safe_records(nodes, edges, check=check)

    monkeypatch.setattr(
        "app.analytics.sqlite_graph_store.safe_graph_records",
        revoke_during_validation,
    )
    with pytest.raises(GraphStoreError, match="graph_generation_ownership_lost"):
        writer.replace(nodes=[graph_node("heartbeat-error")], edges=[])

    assert writer._heartbeat_thread is None
    assert not any(
        thread.name == f"graph-lease-{generation_id[:8]}"
        for thread in threading.enumerate()
    )
    assert harness.database.open_connection_count(harness.database.path) == 0


def test_chunked_write_losing_lease_stays_invisible_and_is_reclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    generation_id = harness.begin_build()
    with harness.database.transaction() as connection:
        connection.execute(
            "UPDATE projection_generations SET lease_expires_at=? WHERE generation_id=?",
            (sql_time(datetime.now(timezone.utc) + timedelta(seconds=30)), generation_id),
        )
    writer = SQLiteGraphGenerationWriter(
        harness.database,
        generation_id=generation_id,
        partition_key=harness.account_ref,
        owner=harness.owner,
        lease_seconds=30,
    )
    nodes = [graph_node(f"lease-loss-{index:04d}") for index in range(1_200)]
    original_owned_transaction = writer._owned_transaction
    lease_revoked = False

    @contextmanager
    def revoke_after_first_chunk():
        nonlocal lease_revoked
        if lease_revoked:
            with original_owned_transaction() as connection:
                yield connection
            return
        with original_owned_transaction() as connection:
            yield connection
        with harness.database.read() as connection:
            written = int(
                connection.execute(
                    "SELECT COUNT(*) FROM graph_nodes WHERE generation_id=?",
                    (generation_id,),
                ).fetchone()[0]
            )
        if written >= 500:
            with harness.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE projection_generations
                    SET owner_capability_digest=?, lease_expires_at=?
                    WHERE generation_id=? AND status='building'
                    """,
                    (
                        "sha256:" + "f" * 64,
                        sql_time(datetime.now(timezone.utc) - timedelta(seconds=1)),
                        generation_id,
                    ),
                )
            lease_revoked = True

    monkeypatch.setattr(writer, "_owned_transaction", revoke_after_first_chunk)
    with pytest.raises(GraphStoreError, match="graph_generation_ownership_lost"):
        writer.replace(nodes=nodes, edges=[])

    assert lease_revoked
    assert harness.reader.nodes(account_ref("account-a")) == []
    with harness.database.read() as connection:
        generation = connection.execute(
            "SELECT status FROM projection_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        partial_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM graph_nodes WHERE generation_id=?",
                (generation_id,),
            ).fetchone()[0]
        )
    assert generation is not None and generation["status"] == "building"
    assert 500 <= partial_count < len(nodes)

    SQLiteAnalyticsProjectionStore(
        harness.database.path,
        activation=InMemoryProjectionActivationRepository(lambda _: None),
        canonical_identity_reader=lambda _: None,
    )
    with harness.database.read() as connection:
        reclaimed = connection.execute(
            "SELECT status FROM projection_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
    assert reclaimed is not None and reclaimed["status"] == "retired"
    assert harness.reader.nodes(account_ref("account-a")) == []


def test_direct_sql_cannot_reopen_mutate_or_delete_active_generation(
    tmp_path: Path,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    first, second = graph_node("one"), graph_node("two")
    generation_id = harness.publish(
        [first, second], [graph_edge("edge", first.node_id, second.node_id)]
    )
    statements = [
        ("UPDATE projection_generations SET status='building' WHERE generation_id=?",),
        (
            "UPDATE projection_generations SET graph_digest=? WHERE generation_id=?",
            "sha256:" + "9" * 64,
        ),
        ("DELETE FROM projection_generations WHERE generation_id=?",),
        (
            "UPDATE graph_nodes SET properties_json='{}' WHERE generation_id=?",
        ),
        (
            "UPDATE projection_generations SET activation_intent_id='changed' "
            "WHERE generation_id=?",
        ),
    ]
    for item in statements:
        statement, *extra = item
        parameters = (*extra, generation_id) if extra else (generation_id,)
        with pytest.raises(sqlite3.IntegrityError):
            with harness.database.transaction() as connection:
                connection.execute(statement, parameters)


def test_default_algorithm_parameters_hit_generation_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    left, right = graph_node("left"), graph_node("right")
    harness.publish([left, right], [graph_edge("edge", left.node_id, right.node_id)])
    calls = 0
    original = networkx_adapter.BoundedNetworkXAlgorithms.centrality

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(networkx_adapter.BoundedNetworkXAlgorithms, "centrality", counted)
    first = harness.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="betweenness",
        bounds=algorithm_bounds(max_nodes=2, max_edges=1),
        seed=7,
    )
    assert harness.reader.last_materialized_rows == 3
    second = harness.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="betweenness",
        bounds=algorithm_bounds(max_nodes=2, max_edges=1),
        seed=7,
        parameters={},
    )
    assert first == second
    assert harness.reader.last_materialized_rows == 0
    assert calls == 1
    with harness.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 1


def test_generation_replacement_never_returns_a_stale_cached_result(
    tmp_path: Path,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    left, right = graph_node("cached-left"), graph_node("cached-right")
    harness.publish(
        [left, right],
        [graph_edge("cached-edge", left.node_id, right.node_id)],
    )
    first = harness.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="degree",
        bounds=algorithm_bounds(max_nodes=2, max_edges=1),
        seed=43,
    )
    assert first.node_count == 2

    harness.publish([left], [], revision=2)
    replacement = harness.reader.compute_centrality(
        account_ref("account-a"),
        algorithm="degree",
        bounds=algorithm_bounds(max_nodes=2, max_edges=1),
        seed=43,
    )

    assert replacement.node_count == 1
    assert set(replacement.values) == {left.node_id}
    with harness.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 2


def test_inflight_algorithm_cannot_return_or_cache_after_generation_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    left, right = graph_node("left"), graph_node("right")
    harness.publish([left, right], [graph_edge("edge", left.node_id, right.node_id)])
    entered = threading.Event()
    release = threading.Event()
    original = networkx_adapter.BoundedNetworkXAlgorithms.centrality

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(networkx_adapter.BoundedNetworkXAlgorithms, "centrality", delayed)
    outcome: list[BaseException | object] = []

    def run() -> None:
        try:
            outcome.append(
                harness.reader.compute_centrality(
                    account_ref("account-a"),
                    algorithm="degree",
                    bounds=algorithm_bounds(max_nodes=2, max_edges=1),
                    seed=9,
                )
            )
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(timeout=2)
    harness.publish([left], [], revision=2)
    release.set()
    thread.join(timeout=3)
    assert len(outcome) == 1 and isinstance(outcome[0], GraphStoreError)
    with harness.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 0


@pytest.mark.slow
def test_16400_disjoint_edges_materialize_only_the_root_frontier(
    tmp_path: Path,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    pairs = [
        (graph_node(f"left-{index}"), graph_node(f"right-{index}"))
        for index in range(16_400)
    ]
    edges = [
        graph_edge(f"edge-{index}", left.node_id, right.node_id)
        for index, (left, right) in enumerate(pairs)
    ]
    harness.publish(
        [item for pair in pairs for item in pair],
        edges,
    )
    root = pairs[0][0]
    result = harness.reader.neighborhood(
        account_ref("account-a"),
        root.node_id,
        bounds=GraphTraversalBounds(
            creator_account_id=account_ref("account-a"),
            start_time=NOW - timedelta(days=1),
            end_time=NOW + timedelta(days=1),
            max_hops=2,
            max_results=1,
            max_visited=1,
            max_queue=1,
            max_edges_examined=20_000,
            wall_clock_ms=30_000,
        ),
    )
    assert result.truncated
    assert len(result.nodes) == 1
    assert result.edges == []
    assert harness.reader.last_materialized_rows <= 5


def test_direct_sql_rejects_raw_ids_nested_properties_and_naive_times_before_wal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projections.sqlite3"
    harness = SQLiteGraphHarness(path)
    generation_id = harness.begin_build()
    valid_node_id = graph_node("valid-direct-node").node_id
    markers = (
        "SYNTHETIC-DIRECT-RAW-ID-701",
        "SYNTHETIC-DIRECT-NESTED-PROPERTY-702",
        "SYNTHETIC-DIRECT-NAIVE-TIME-703",
    )
    attempts = (
        (
            markers[0],
            sql_time(NOW),
            '{"character_count":1}',
        ),
        (
            valid_node_id,
            sql_time(NOW),
            json.dumps({"character_count": {markers[1]: 1}}),
        ),
        (
            valid_node_id,
            f"2026-07-19T12:00:00.{markers[2]}",
            '{"character_count":1}',
        ),
    )
    for node_id, occurred_at, properties_json in attempts:
        with pytest.raises(sqlite3.IntegrityError):
            with harness.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO graph_nodes (
                        generation_id, creator_account_id, node_id, kind,
                        occurred_at, properties_json
                    ) VALUES (?, ?, ?, 'message', ?, ?)
                    """,
                    (
                        generation_id,
                        harness.account_ref,
                        node_id,
                        occurred_at,
                        properties_json,
                    ),
                )

    with harness.database.read() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stored_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    for marker in markers:
        assert marker.encode() not in stored_bytes


def test_populated_projection_v2_metric_upgrade_discards_legacy_rows_and_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projections.sqlite3"
    legacy_catalog = tmp_path / "projection-v2-migrations"
    legacy_catalog.mkdir()
    source_catalog = Path(__file__).parents[1] / "app" / "analytics" / "sql"
    for name in ("0001_projection_store.sql", "0002_generation_lifecycle.sql"):
        shutil.copy2(source_catalog / name, legacy_catalog / name)

    legacy = ProjectionsDatabase(path, migrations_dir=legacy_catalog)
    generation_id = str(uuid4())
    epoch = str(uuid4())
    with legacy.transaction() as connection:
        connection.execute(
            """
            INSERT INTO projection_publication_epochs (
                publication_epoch, scheduler_owner_id, state, opened_at
            ) VALUES (?, 'legacy-owner', 'open', ?)
            """,
            (epoch, sql_time(NOW)),
        )
        connection.execute(
            """
            INSERT INTO projection_generations (
                generation_id, creator_account_id, status, schema_version,
                build_version, canonical_revision, canonical_content_digest,
                canonical_high_water_json, pipeline_revision,
                pipeline_config_digest, pipeline_identity_digest,
                projection_digest, graph_digest, node_count, edge_count,
                activation_intent_id, witness_sequence, publication_epoch,
                owner_id, owner_pid, owner_process_started_at,
                owner_instance_nonce, lease_expires_at, started_at,
                validated_at, activated_at
            ) VALUES (?, 'SYNTHETIC-LEGACY-ACCOUNT-704', 'active', 2,
                      'legacy', 7, ?, '{}', 'legacy', ?, ?, ?, ?, 0, 0,
                      'legacy-intent', 9, ?, 'legacy-owner', 1234,
                      'legacy-start', 'legacy-nonce', ?, ?, ?, ?)
            """,
            (
                generation_id,
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                epoch,
                sql_time(NOW + timedelta(hours=1)),
                sql_time(NOW),
                sql_time(NOW),
                sql_time(NOW),
            ),
        )
        connection.execute(
            """
            INSERT INTO graph_algorithm_metrics (
                generation_id, creator_account_id, metric_kind, algorithm,
                parameter_hash, result_json, computed_at,
                activation_intent_id, witness_sequence, publication_epoch
            ) VALUES (?, 'SYNTHETIC-LEGACY-ACCOUNT-704', 'centrality',
                      'degree', 'legacy-parameters', '{}', ?,
                      'legacy-intent', 9, ?)
            """,
            (generation_id, sql_time(NOW), epoch),
        )

    upgraded = ProjectionsDatabase(path)
    backup_path = upgraded.migration_runner.last_backup_path
    assert backup_path is not None and backup_path.exists()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 1
    with upgraded.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM projection_generations"
        ).fetchone()[0] == 0
        identity = connection.execute(
            "SELECT schema_identity FROM projection_store_identity"
        ).fetchone()[0]
        assert identity == "projection-v4"

    restarted = ProjectionsDatabase(path)
    with restarted.read() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_populated_projection_v1_metric_upgrade_through_v4_is_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projections-v1.sqlite3"
    legacy_catalog = tmp_path / "projection-v1-migrations"
    legacy_catalog.mkdir()
    source_catalog = Path(__file__).parents[1] / "app" / "analytics" / "sql"
    shutil.copy2(
        source_catalog / "0001_projection_store.sql",
        legacy_catalog / "0001_projection_store.sql",
    )
    legacy = ProjectionsDatabase(path, migrations_dir=legacy_catalog)
    generation_id = str(uuid4())
    marker = "SYNTHETIC-LEGACY-V1-ACCOUNT-881"
    with legacy.transaction() as connection:
        connection.execute(
            """
            INSERT INTO projection_generations (
                generation_id,creator_account_id,status,schema_version,
                build_version,canonical_revision,canonical_content_digest,
                canonical_high_water_json,pipeline_revision,
                pipeline_config_digest,pipeline_identity_digest,
                projection_digest,graph_digest,node_count,edge_count,
                owner_id,lease_expires_at,started_at,validated_at,activated_at
            ) VALUES (?, ?, 'active', 1, 'legacy-v1', 3, ?, '{}',
                      'legacy-v1', ?, ?, ?, ?, 0, 0, 'legacy-owner', ?, ?, ?, ?)
            """,
            (
                generation_id,
                marker,
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                sql_time(NOW + timedelta(hours=1)),
                sql_time(NOW),
                sql_time(NOW),
                sql_time(NOW),
            ),
        )
        connection.execute(
            """
            INSERT INTO graph_algorithm_metrics (
                generation_id,creator_account_id,metric_kind,algorithm,
                parameter_hash,result_json,computed_at
            ) VALUES (?, ?, 'community', 'label_propagation',
                      'legacy-v1-parameters', '{}', ?)
            """,
            (generation_id, marker, sql_time(NOW)),
        )

    upgraded = ProjectionsDatabase(path)
    backup_path = upgraded.migration_runner.last_backup_path
    assert backup_path is not None and backup_path.exists()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 1
    with upgraded.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM graph_algorithm_metrics"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM projection_generations"
        ).fetchone()[0] == 0
    with ProjectionsDatabase(path).read() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_deadline_and_cancellation_cover_sql_materialization(tmp_path: Path) -> None:
    harness = SQLiteGraphHarness(tmp_path / "projections.sqlite3")
    root = graph_node("root")
    leaves = [graph_node(f"leaf-{index}") for index in range(500)]
    harness.publish(
        [root, *leaves],
        [
            graph_edge(f"edge-{index}", root.node_id, leaf.node_id)
            for index, leaf in enumerate(leaves)
        ],
    )
    bounds = algorithm_bounds(max_nodes=501, max_edges=500, wall_clock_ms=1)
    with pytest.raises((GraphDeadlineExceeded, ProjectionBuildCancelled)):
        harness.reader.compute_centrality(
            account_ref("account-a"), algorithm="degree", bounds=bounds, seed=1
        )
    with pytest.raises(ProjectionBuildCancelled):
        harness.reader.compute_centrality(
            account_ref("account-a"),
            algorithm="degree",
            bounds=algorithm_bounds(max_nodes=501, max_edges=500),
            seed=1,
            cancellation_check=lambda: True,
        )


@pytest.mark.parametrize(
    "operation",
    ["neighborhood", "find_paths", "degree", "centrality", "communities"],
)
def test_cancellation_after_delayed_active_resolver_never_returns(
    tmp_path: Path,
    operation: str,
) -> None:
    harness = SQLiteGraphHarness(tmp_path / f"{operation}.sqlite3")
    left, right = graph_node("resolver-left"), graph_node("resolver-right")
    harness.publish(
        [left, right],
        [graph_edge("resolver-edge", left.node_id, right.node_id)],
    )
    cancelled = False

    def delayed_resolver(account: str, **_: object) -> str | None:
        nonlocal cancelled
        result = harness.active if account == harness.account_ref else None
        cancelled = True
        return result

    harness.reader._active_resolver = delayed_resolver
    cancellation_check = lambda: cancelled
    with pytest.raises(ProjectionBuildCancelled):
        if operation == "neighborhood":
            harness.reader.neighborhood(
                harness.account_ref,
                left.node_id,
                bounds=GraphTraversalBounds(
                    account_ref=harness.account_ref,
                    start_time=NOW - timedelta(days=1),
                    end_time=NOW + timedelta(days=1),
                    max_hops=1,
                    max_results=2,
                    max_visited=2,
                ),
                cancellation_check=cancellation_check,
            )
        elif operation == "find_paths":
            harness.reader.find_paths(
                harness.account_ref,
                left.node_id,
                right.node_id,
                bounds=GraphTraversalBounds(
                    account_ref=harness.account_ref,
                    start_time=NOW - timedelta(days=1),
                    end_time=NOW + timedelta(days=1),
                    max_hops=1,
                    max_results=1,
                    max_visited=2,
                ),
                cancellation_check=cancellation_check,
            )
        elif operation == "degree":
            harness.reader.degree(
                harness.account_ref,
                left.node_id,
                bounds=GraphTraversalBounds(
                    account_ref=harness.account_ref,
                    start_time=NOW - timedelta(days=1),
                    end_time=NOW + timedelta(days=1),
                    max_hops=1,
                    max_results=1,
                    max_visited=2,
                ),
                cancellation_check=cancellation_check,
            )
        elif operation == "centrality":
            harness.reader.compute_centrality(
                harness.account_ref,
                algorithm="degree",
                bounds=algorithm_bounds(max_nodes=2, max_edges=1),
                seed=1,
                cancellation_check=cancellation_check,
            )
        else:
            harness.reader.detect_communities(
                harness.account_ref,
                algorithm="connected_components",
                bounds=algorithm_bounds(max_nodes=2, max_edges=1),
                seed=1,
                cancellation_check=cancellation_check,
            )


def test_raw_source_identifier_never_reaches_sqlite_graph_bytes_or_errors(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC-RAW-PLATFORM-ID-881"
    path = tmp_path / "projections.sqlite3"
    harness = SQLiteGraphHarness(path)
    first = graph_node(marker)
    second = graph_node("safe-peer")
    harness.publish(
        [first, second],
        [graph_edge(marker, first.node_id, second.node_id)],
    )
    with harness.database.read() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    surfaces = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    assert marker.encode("utf-8") not in surfaces

    with pytest.raises(ValidationError) as captured:
        GraphNode(
            node_id=marker,
            partition_key=account_ref("account-a"),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=NOW,
            properties={"character_count": 1},
        )
    assert "graph_id_invalid" in str(captured.value)
    assert marker not in str(captured.value)


def test_migration_checksum_tamper_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "projections.sqlite3"
    database = ProjectionsDatabase(path)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='bad' WHERE version=1"
        )
    with pytest.raises(MigrationChecksumError):
        ProjectionsDatabase(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_windows_database_and_sidecars_have_verified_owner_only_dacls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projections.sqlite3"
    database = ProjectionsDatabase(path)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_migrations SET applied_at=applied_at WHERE version=1"
        )
        candidates = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
        assert all(candidate.exists() for candidate in candidates)
        assert all(_windows_acl_is_owner_only(candidate) for candidate in candidates)
