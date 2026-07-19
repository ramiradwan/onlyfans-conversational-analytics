from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.analytics.errors import (
    CanonicalRevisionChanged,
    ProjectionStorageUnavailable,
)
from app.analytics.graph_identity import graph_id
from app.analytics.graph_privacy import graph_content_digest
from app.analytics.graph_store import GraphDeadlineExceeded, GraphStoreError
from app.analytics.database import ProjectionsDatabase
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import CanonicalIdentity, canonical_identity
from app.analytics.opaque_refs import account_ref, validated_account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.projection_store import projection_content_digest
from app.analytics.resilient_projection_store import (
    LazySQLiteAnalyticsProjectionStore,
)
from app.analytics.scheduling import InProcessProjectionScheduler
from app.analytics.sqlite_projection_store import (
    ProjectionValidationError,
    SQLiteAnalyticsProjectionStore,
)
from app.analytics.sqlite_graph_store import SQLiteGraphGenerationWriter
from app.core.config import Settings
from app.models.analytics import (
    AvailabilityStatus,
    GraphAlgorithmBounds,
    GraphNode,
    GraphNodeKind,
    GraphProjectionSummary,
    RebuildArtifact,
)
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.projection_activation import ProjectionActivationConflict
from app.protocol.payloads import IngestSnapshotPayload
from app.transport.ingestion import IngestionService, StreamKey


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"
WORKER = Path(__file__).with_name("projection_crash_worker.py")
CONCURRENT_WORKER = Path(__file__).with_name("projection_concurrent_worker.py")


def identity_reader(repositories: CanonicalRepositories):
    def read(account_id: str) -> CanonicalIdentity | None:
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    return read


def prepare_empty_canonical(path: Path) -> CanonicalRepositories:
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=path,
        projection_path=path.with_name("history-projections.sqlite3"),
    )
    assert repositories.database is not None
    if not repositories.ingestion.account_exists("account-a"):
        with repositories.database.transaction() as connection:
            connection.execute(
                "INSERT INTO account_heads(creator_account_id, updated_at)"
                " VALUES ('account-a', '2026-07-19T00:00:00+00:00')"
            )
    return repositories


def make_store(
    path: Path,
    repositories: CanonicalRepositories,
    *,
    lease_seconds: float = 120.0,
    rollback_retention: int = 1,
    gc_batch_size: int = 8,
) -> SQLiteAnalyticsProjectionStore:
    return SQLiteAnalyticsProjectionStore(
        path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        lease_seconds=lease_seconds,
        rollback_retention=rollback_retention,
        gc_batch_size=gc_batch_size,
    )


def advance(repositories: CanonicalRepositories, revision: int) -> None:
    assert repositories.database is not None
    with repositories.database.transaction() as connection:
        connection.execute(
            """
            UPDATE account_heads SET canonical_revision=?
            WHERE creator_account_id='account-a'
            """,
            (revision,),
        )


def pipeline_for(
    repositories: CanonicalRepositories, store: SQLiteAnalyticsProjectionStore
) -> AnalyticsPipeline:
    return AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
    )


def test_projection_database_path_has_a_separate_default(monkeypatch) -> None:
    monkeypatch.delenv("PROJECTION_DATABASE_PATH", raising=False)
    configured = Settings(_env_file=None)
    assert configured.projection_database_path == Path("projections.sqlite3")
    assert configured.projection_database_path != configured.canonical_database_path


@pytest.mark.asyncio
async def test_scheduler_recovery_publishes_sqlite_once_through_owned_executor(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    scheduler = InProcessProjectionScheduler(
        pipeline_for(repositories, store), worker_count=2, queue_capacity=4
    )
    await scheduler.start(recover=True)
    state = await scheduler.wait("account-a")
    assert state.availability is AvailabilityStatus.AVAILABLE
    first = store.database.active_generation("account-a")
    assert first is not None

    await scheduler.start(recover=True)
    assert store.database.active_generation("account-a") == first
    assert len(store.database.generations("account-a")) == 1
    assert await scheduler.close(timeout=2)


def test_projection_and_graph_survive_restart_with_exact_completed_witness(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    repositories = prepare_empty_canonical(canonical_path)
    stores = create_analytics_stores(
        "sqlite",
        projections_path=projections_path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    first = pipeline.project_account("account-a")

    assert first.changed and stores.database is not None
    active = stores.database.active_generation("account-a")
    assert active is not None and active.status == "active"
    expected_identity = identity_reader(repositories)("account-a")
    assert expected_identity is not None
    assert active.canonical_revision == expected_identity.revision
    assert active.canonical_content_digest == expected_identity.content_digest
    assert json.loads(active.canonical_high_water_json) == {
        "content_digest": expected_identity.content_digest,
        "view_revision": expected_identity.revision,
    }
    assert active.projection_digest == first.artifact.projection.content_digest
    assert active.graph_digest
    intent = repositories.projection_activation.get(active.generation_id)
    assert intent is not None and intent.state == "completed"
    assert intent.graph_digest == active.graph_digest
    assert intent.pipeline_identity_digest == active.pipeline_identity_digest

    restarted_repositories = prepare_empty_canonical(canonical_path)
    restarted = make_store(projections_path, restarted_repositories)
    second = pipeline_for(restarted_repositories, restarted).project_account(
        "account-a"
    )
    assert not second.changed
    assert second.artifact == first.artifact


def test_canonical_advance_immediately_hides_stale_projection_and_graph(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline_for(repositories, store).project_account("account-a")
    assert store.get("account-a") is not None
    partition_ref = account_ref("account-a")
    assert store.graph.nodes(partition_ref)

    advance(repositories, 1)
    assert store.get("account-a") is None
    assert store.graph.nodes(partition_ref) == []
    assert store.graph.partition_revision(partition_ref) is None


def test_production_graph_mutators_cannot_touch_active_generation(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline_for(repositories, store).project_account("account-a")
    active = store.database.active_generation("account-a")
    partition_ref = account_ref("account-a")
    nodes = store.graph.nodes(partition_ref)
    edges = store.graph.edges(partition_ref)
    assert active is not None and nodes

    assert not hasattr(store.graph, "upsert_node")
    assert not hasattr(store.graph, "upsert_edge")
    assert not hasattr(store.graph, "replace_partition")
    assert not hasattr(store.graph, "clear_partition")
    assert store.database.active_generation("account-a") == active


def test_production_graph_deadline_includes_active_identity_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline_for(repositories, store).project_account("account-a")
    original = store.canonical_identity_reader

    def delayed_identity_read(*args, **kwargs):
        time.sleep(0.02)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "canonical_identity_reader", delayed_identity_read)
    with pytest.raises(GraphDeadlineExceeded, match="graph_deadline_exceeded"):
        store.graph.compute_centrality(
            account_ref("account-a"),
            algorithm="degree",
            bounds=GraphAlgorithmBounds(
                creator_account_id=account_ref("account-a"),
                start_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
                end_time=datetime(2100, 1, 1, tzinfo=timezone.utc),
                max_hops=1,
                max_nodes=1,
                max_edges=1,
                wall_clock_ms=1,
                include_timeless=True,
            ),
            seed=47,
        )


def test_canonical_and_projection_paths_must_be_distinct(tmp_path: Path) -> None:
    repositories = create_canonical_repositories("memory")
    path = tmp_path / "one.sqlite3"
    with pytest.raises(ValueError, match="separate files"):
        create_analytics_stores(
            "sqlite",
            projections_path=path,
            canonical_path=path,
            activation=repositories.projection_activation,
            canonical_identity_reader=lambda account_id: None,
        )


@pytest.mark.slow
@pytest.mark.parametrize(
    "crash_stage",
    [
        "built",
        "validated",
        "canonical_intent_reserved",
        "intent_reserved",
        "canonical_completed",
        "activated",
    ],
)
def test_subprocess_crash_reopen_at_every_activation_boundary(
    tmp_path: Path, crash_stage: str
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    prepare_empty_canonical(canonical_path)
    result = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            crash_stage,
            str(canonical_path),
            str(projections_path),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        timeout=15,
    )
    assert result.returncode == 91

    repositories = prepare_empty_canonical(canonical_path)
    reopened = make_store(projections_path, repositories, lease_seconds=1.0)
    if crash_stage in {"built", "validated"}:
        generations = reopened.database.generations("account-a")
        assert len(generations) == 1
        # Positive dead-owner detection may accelerate reclaim; the bounded
        # lease remains the portable fallback on platforms where process
        # liveness cannot be proven immediately.
        assert generations[0].status in {
            "retired",
            "building" if crash_stage == "built" else "validated",
        }
        assert reopened.get("account-a") is None
        time.sleep(1.05)
        recovered = make_store(projections_path, repositories, lease_seconds=1.0)
        generations = recovered.database.generations("account-a")
        assert not generations or generations[0].status == "retired"
    else:
        active = reopened.database.active_generation("account-a")
        assert active is not None
        assert reopened.get("account-a") is not None
        assert repositories.projection_activation.pending() == []


def test_reserved_activation_is_cancelled_after_advance_and_reopen(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    prepare_empty_canonical(canonical_path)
    result = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "intent_reserved",
            str(canonical_path),
            str(projections_path),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        timeout=15,
    )
    assert result.returncode == 91
    repositories = prepare_empty_canonical(canonical_path)
    pending = repositories.projection_activation.pending()
    assert len(pending) == 1
    advance(repositories, 1)

    reopened = make_store(projections_path, repositories)
    cancelled = repositories.projection_activation.get(pending[0].generation_id)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert reopened.database.active_generation("account-a") is None
    assert reopened.get("account-a") is None


@pytest.mark.slow
def test_concurrent_process_cannot_retire_live_build_or_rollback_winner(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    ready = tmp_path / "writer.ready"
    release = tmp_path / "writer.release"
    prepare_empty_canonical(canonical_path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(CONCURRENT_WORKER),
            str(canonical_path),
            str(projections_path),
            str(ready),
            str(release),
        ],
        cwd=Path(__file__).parents[1],
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("first projection writer did not reach the durable boundary")
            time.sleep(0.01)
        assert process.poll() is None

        repositories = prepare_empty_canonical(canonical_path)
        competing = make_store(
            projections_path, repositories, lease_seconds=10
        )
        generations = competing.database.generations("account-a")
        assert len(generations) == 1 and generations[0].status == "building"
        with pytest.raises(ProjectionActivationConflict):
            competing.discard_generation(generations[0].generation_id)
        winner = pipeline_for(repositories, competing).project_account("account-a")
        winner_generation = competing.database.active_generation("account-a")
        assert winner.changed and winner_generation is not None

        release.write_text("release\n", encoding="utf-8")
        assert process.wait(timeout=15) == 92
        recovered = make_store(projections_path, repositories, lease_seconds=10)
        active = recovered.database.active_generation("account-a")
        assert active is not None
        assert active.generation_id == winner_generation.generation_id
        assert sum(
            item.status == "active"
            for item in recovered.database.generations("account-a")
        ) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_copied_persisted_owner_fields_without_capability_cannot_write(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projection_path = tmp_path / "projections.sqlite3"
    repositories = prepare_empty_canonical(canonical_path)

    class StopBeforeValidation(RuntimeError):
        pass

    def stop_at_built(stage: str, generation_id: str) -> None:
        del generation_id
        if stage == "built":
            raise StopBeforeValidation()

    original = SQLiteAnalyticsProjectionStore(
        projection_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        crash_hook=stop_at_built,
        owner_id="copied-owner-id",
        lease_seconds=30,
    )
    with pytest.raises(StopBeforeValidation):
        pipeline_for(repositories, original).build_candidate("account-a")
    generation = original.database.generations("account-a")[0]
    assert generation.status == "building"

    copied = SQLiteAnalyticsProjectionStore(
        projection_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        reconcile=False,
        owner_id=generation.owner_id,
        lease_seconds=30,
    )
    assert copied.build_owner.persisted_identity[:-1] == (
        generation.owner_id,
        generation.owner_pid,
        generation.owner_process_started_at,
        generation.owner_instance_nonce,
    )
    assert copied.build_owner.capability_digest != generation.owner_capability_digest

    with pytest.raises(ProjectionActivationConflict):
        copied.discard_generation(generation.generation_id)
    attacker_writer = SQLiteGraphGenerationWriter(
        copied.database,
        generation_id=generation.generation_id,
        partition_key=generation.account_ref,
        owner=copied.build_owner,
        lease_seconds=30,
    )
    with pytest.raises(GraphStoreError, match="graph_generation_ownership_lost"):
        attacker_writer.refresh()

    original_writer = SQLiteGraphGenerationWriter(
        original.database,
        generation_id=generation.generation_id,
        partition_key=generation.account_ref,
        owner=original.build_owner,
        lease_seconds=30,
    )
    original_writer.refresh()


@pytest.mark.slow
def test_50000_node_stage_renews_short_writer_lease_through_validation(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(
        tmp_path / "projections.sqlite3",
        repositories,
        lease_seconds=0.5,
    )
    pipeline = pipeline_for(repositories, store)
    canonical = repositories.ingestion.account_read_model("account-a")
    base = pipeline._build("account-a", canonical, projection_generation=1)
    occurred_at = datetime.now(timezone.utc)
    nodes = [
        GraphNode(
            node_id=graph_id(validated_account_ref(account_ref("account-a")), "message", f"bulk-{index}"),
            partition_key=account_ref("account-a"),
            kind=GraphNodeKind.MESSAGE,
            occurred_at=occurred_at,
            properties={"character_count": 1},
        )
        for index in range(50_000)
    ]
    graph_digest = graph_content_digest(nodes, [])
    projection = base.projection.model_copy(
        update={
            "graph_digest": graph_digest,
            "graph": GraphProjectionSummary(
                account_ref=account_ref("account-a"),
                source_revision=canonical.view_revision,
                node_count=len(nodes),
                edge_count=0,
                node_counts_by_kind={"message": len(nodes)},
                edge_counts_by_relation={},
            ),
            "projection_digest": "sha256:" + "0" * 64,
        }
    )
    projection = projection.model_copy(
        update={"projection_digest": projection_content_digest(projection)}
    )
    artifact = RebuildArtifact(projection=projection, nodes=nodes, edges=[])
    identity = canonical_identity(canonical)

    started = time.monotonic()
    generation_id = store.stage_artifact(
        artifact,
        creator_account_id="account-a",
        canonical_identity=identity,
    )
    assert time.monotonic() - started > store.lease_seconds
    generation = store.database.generation(generation_id)
    assert generation is not None and generation.status == "validated"
    assert generation.node_count == 50_000


@pytest.mark.parametrize("damage", ["null_local_intent", "missing", "cancelled"])
def test_startup_quarantines_active_generation_without_exact_witness(
    tmp_path: Path, damage: str
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline_for(repositories, store).project_account("account-a")
    active = store.database.active_generation("account-a")
    assert active is not None and repositories.database is not None

    if damage == "null_local_intent":
        with store.database.transaction() as connection:
            connection.execute("DROP TRIGGER projection_generation_witness_immutable")
            connection.execute(
                """
                UPDATE projection_generations SET activation_intent_id=NULL
                WHERE generation_id=?
                """,
                (active.generation_id,),
            )
    elif damage == "missing":
        with repositories.database.transaction() as connection:
            connection.execute(
                "DELETE FROM analytics_projection_activation_intents WHERE generation_id=?",
                (active.generation_id,),
            )
    else:
        with repositories.database.transaction() as connection:
            connection.execute(
                "DROP TRIGGER analytics_projection_activation_state_is_terminal"
            )
            connection.execute(
                """
                UPDATE analytics_projection_activation_intents
                SET state='cancelled', cancelled_at=? WHERE generation_id=?
                """,
                (datetime.now(timezone.utc).isoformat(), active.generation_id),
            )

    reopened = make_store(tmp_path / "projections.sqlite3", repositories)
    assert reopened.database.active_generation("account-a") is None
    assert reopened.get("account-a") is None


def test_tampered_pending_generation_is_cancelled_and_never_activated(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    prepare_empty_canonical(canonical_path)
    result = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "intent_reserved",
            str(canonical_path),
            str(projections_path),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        timeout=15,
    )
    assert result.returncode == 91
    repositories = prepare_empty_canonical(canonical_path)
    pending = repositories.projection_activation.pending()[0]
    database = ProjectionsDatabase(projections_path)
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER graph_node_building_update")
        connection.execute(
            """
            UPDATE graph_nodes SET properties_json='{"character_count":999}'
            WHERE generation_id=? AND node_id=(
                SELECT MIN(node_id) FROM graph_nodes WHERE generation_id=?
            )
            """,
            (pending.generation_id, pending.generation_id),
        )
    reopened = make_store(projections_path, repositories)
    assert reopened.database.active_generation("account-a") is None
    assert reopened.get("account-a") is None
    witness = repositories.projection_activation.get(pending.generation_id)
    assert witness is not None and witness.state == "cancelled"


def test_active_rows_are_schema_immutable_and_digest_checked_on_read(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline_for(repositories, store).project_account("account-a")
    active = store.database.active_generation("account-a")
    assert active is not None
    statement = """
        UPDATE graph_nodes SET properties_json='{"character_count":999}'
        WHERE generation_id=? AND node_id=(
            SELECT MIN(node_id) FROM graph_nodes WHERE generation_id=?
        )
    """
    with store.database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="projection_child_write_blocked"):
            connection.execute(statement, (active.generation_id, active.generation_id))
    with store.database.transaction() as connection:
        connection.execute("DROP TRIGGER graph_node_building_update")
        connection.execute(statement, (active.generation_id, active.generation_id))
    with pytest.raises(ProjectionValidationError):
        store.get("account-a")


def test_concurrent_generation_cas_prevents_delayed_replacement(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline = pipeline_for(repositories, store)
    pipeline.project_account("account-a")
    advance(repositories, 1)
    first = pipeline.build_candidate("account-a")
    delayed = pipeline.build_candidate("account-a")
    assert pipeline.publish_candidate(first).changed
    with pytest.raises(ProjectionActivationConflict, match="active generation changed"):
        pipeline.publish_candidate(delayed)
    active = store.database.active_generation("account-a")
    assert active is not None and active.canonical_revision == 1
    pipeline.discard_candidate(delayed)


def test_delayed_lower_revision_cannot_rollback_newer_generation(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline = pipeline_for(repositories, store)
    delayed_revision_zero = pipeline.build_candidate("account-a")
    advance(repositories, 1)
    current = pipeline.build_candidate("account-a")
    pipeline.publish_candidate(current)
    with pytest.raises(CanonicalRevisionChanged):
        pipeline.publish_candidate(delayed_revision_zero)
    active = store.database.active_generation("account-a")
    assert active is not None and active.canonical_revision == 1


def test_active_before_canonical_completion_is_retired_and_reservation_cancelled(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    path = tmp_path / "projections.sqlite3"
    store = make_store(path, repositories)
    pipeline = pipeline_for(repositories, store)
    candidate = pipeline.build_candidate("account-a")
    assert candidate.staged_generation_id is not None
    generation = store.database.generation(candidate.staged_generation_id)
    identity = identity_reader(repositories)("account-a")
    assert generation is not None and identity is not None
    with store.database.read() as connection:
        publication_digest = connection.execute(
            """
            SELECT scheduler_capability_digest FROM projection_publication_epochs
            WHERE publication_epoch=?
            """,
            (generation.publication_epoch,),
        ).fetchone()[0]
    intent = repositories.projection_activation.reserve(
        creator_account_id="account-a",
        account_ref=generation.account_ref,
        generation_id=generation.generation_id,
        canonical_identity=identity,
        projection_digest=generation.projection_digest or "",
        graph_digest=generation.graph_digest or "",
        pipeline_revision=generation.pipeline_revision,
        pipeline_config_digest=generation.pipeline_config_digest,
        pipeline_identity_digest=generation.pipeline_identity_digest,
        expected_previous_generation_id=generation.expected_active_generation_id,
        expected_previous_revision=generation.expected_active_revision,
        publication_epoch=generation.publication_epoch or "",
        writer_owner=store.build_owner,
        publication_capability_digest=publication_digest,
    )
    with store.database.transaction() as connection:
        connection.execute(
            """
            UPDATE projection_generations
            SET status='activation_pending', activation_intent_id=?, witness_sequence=?
            WHERE generation_id=?
            """,
            (intent.intent_id, intent.witness_sequence, generation.generation_id),
        )
        connection.execute(
            "UPDATE projection_generations SET status='active' WHERE generation_id=?",
            (generation.generation_id,),
        )

    reopened = make_store(path, repositories)
    cancelled = repositories.projection_activation.get(generation.generation_id)
    assert cancelled is not None and cancelled.state == "cancelled"
    assert repositories.projection_activation.pending() == []
    assert reopened.database.active_generation("account-a") is None
    assert pipeline_for(repositories, reopened).project_account("account-a").changed


def test_completed_witness_that_loses_local_epoch_is_cancelled_and_never_visible(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")

    class PausedAfterCompletion(RuntimeError):
        pass

    def checkpoint(stage: str, generation_id: str) -> None:
        del generation_id
        if stage == "canonical_completed":
            raise PausedAfterCompletion()

    store = SQLiteAnalyticsProjectionStore(
        tmp_path / "projections.sqlite3",
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        crash_hook=checkpoint,
    )
    pipeline = pipeline_for(repositories, store)
    candidate = pipeline.build_candidate("account-a")
    assert candidate.publication_epoch is not None
    with pytest.raises(PausedAfterCompletion):
        pipeline.publish_candidate(candidate)
    completed = repositories.projection_activation.get(
        candidate.staged_generation_id or ""
    )
    assert completed is not None and completed.state == "completed"

    pipeline.revoke_publication_epoch(
        candidate.publication_epoch,
        f"direct-pipeline-{id(pipeline):x}",
    )
    reopened = make_store(tmp_path / "projections.sqlite3", repositories)
    reconciled = repositories.projection_activation.get(completed.generation_id)
    assert reconciled is not None and reconciled.state == "cancelled"
    assert reopened.database.active_generation("account-a") is None
    assert reopened.get("account-a") is None


@pytest.mark.asyncio
async def test_publication_paused_after_open_check_cannot_activate_after_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(tmp_path / "projections.sqlite3", repositories)
    pipeline = pipeline_for(repositories, store)
    scheduler = InProcessProjectionScheduler(
        pipeline, worker_count=1, queue_capacity=2
    )
    await scheduler.start(recover=False)
    entered = threading.Event()
    release = threading.Event()
    original_publish = pipeline.publish_candidate
    observed_candidates = []

    def paused_publish(candidate):
        observed_candidates.append(candidate)
        entered.set()
        assert release.wait(timeout=3)
        return original_publish(candidate)

    pipeline.publish_candidate = paused_publish  # type: ignore[method-assign]
    await scheduler.schedule("account-a", 0)
    assert await asyncio.to_thread(entered.wait, 2)
    revocations = 0
    original_revoke = repositories.projection_activation.revoke_publication_epoch

    def flaky_canonical_revoke(*args, **kwargs):
        nonlocal revocations
        revocations += 1
        if revocations == 1:
            raise sqlite3.OperationalError("synthetic_canonical_revocation_failure")
        return original_revoke(*args, **kwargs)

    monkeypatch.setattr(
        repositories.projection_activation,
        "revoke_publication_epoch",
        flaky_canonical_revoke,
    )
    close_started = time.monotonic()
    assert not await scheduler.close(timeout=0.02)
    assert time.monotonic() - close_started < 0.08
    assert revocations >= 1
    assert repositories.database is not None
    with repositories.database.read() as connection:
        canonical_epoch = connection.execute(
            "SELECT state FROM analytics_projection_publication_epochs WHERE publication_epoch=?",
            (observed_candidates[0].publication_epoch,),
        ).fetchone()
    assert canonical_epoch is not None and canonical_epoch["state"] == "revoked"
    assert store.database.active_generation("account-a") is None
    release.set()
    deadline = time.monotonic() + 3
    while scheduler.executor_thread_count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert scheduler.executor_thread_count == 0
    assert store.database.active_generation("account-a") is None
    assert len(observed_candidates) == 1
    witnessed = repositories.projection_activation.get(
        observed_candidates[0].staged_generation_id
    )
    assert witnessed is None or witnessed.state == "cancelled"
    assert repositories.projection_activation.pending() == []


def test_verified_dead_owner_reclaims_live_lease_without_waiting_for_timeout(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")

    class StopAfterBuild(RuntimeError):
        pass

    def checkpoint(stage: str, generation_id: str) -> None:
        del generation_id
        if stage == "built":
            raise StopAfterBuild()

    path = tmp_path / "projections.sqlite3"
    store = SQLiteAnalyticsProjectionStore(
        path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        crash_hook=checkpoint,
        lease_seconds=3_600,
    )
    with pytest.raises(StopAfterBuild):
        pipeline_for(repositories, store).build_candidate("account-a")
    generation = store.database.generations("account-a")[0]
    assert generation.status == "building"
    with store.database.transaction() as connection:
        connection.execute(
            """
            UPDATE projection_generations
            SET owner_id='verified-dead-owner', owner_pid=2000000000
            WHERE generation_id=?
            """,
            (generation.generation_id,),
        )
    reopened = make_store(path, repositories, lease_seconds=3_600)
    reclaimed = reopened.database.generation(generation.generation_id)
    assert reclaimed is not None and reclaimed.status == "retired"


async def _wait_for_lazy_projection(
    scheduler: InProcessProjectionScheduler,
    repositories: CanonicalRepositories,
    *,
    timeout: float = 5.0,
):
    account = repositories.ingestion.account_read_model("account-a")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            projection = await scheduler.active_projection("account-a", account)
        except ProjectionStorageUnavailable:
            projection = None
        if projection is not None:
            return projection
        await asyncio.sleep(0.01)
    pytest.fail("lazy projection recovery did not publish in time")


@pytest.mark.asyncio
async def test_deleted_projection_file_returns_unavailable_then_rebuilds_once(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    path = tmp_path / "projections.sqlite3"
    stores = create_analytics_stores(
        "sqlite",
        projections_path=path,
        canonical_path=tmp_path / "canonical.sqlite3",
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        lazy=True,
    )
    assert isinstance(stores.projections, LazySQLiteAnalyticsProjectionStore)
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=2, queue_capacity=4)
    await scheduler.start(recover=True)
    await _wait_for_lazy_projection(scheduler, repositories)
    await scheduler.wait("account-a")
    build_count = 0
    original_build = pipeline._build

    def counted_build(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        return original_build(*args, **kwargs)

    pipeline._build = counted_build  # type: ignore[method-assign]
    path.unlink()
    account = repositories.ingestion.account_read_model("account-a")
    with pytest.raises(ProjectionStorageUnavailable):
        await scheduler.active_projection("account-a", account)
    await scheduler.request_recovery("account-a", account.view_revision)
    await scheduler.request_recovery("account-a", account.view_revision)
    projection = await _wait_for_lazy_projection(scheduler, repositories)
    assert projection.source_revision == account.view_revision
    assert build_count == 1
    assert stores.projections.recovery_count == 2
    assert await scheduler.close(timeout=2)


@pytest.mark.asyncio
async def test_valid_empty_projection_file_replacement_is_quarantined_and_rebuilt_once(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    repositories = prepare_empty_canonical(canonical_path)
    path = tmp_path / "projections.sqlite3"
    stores = create_analytics_stores(
        "sqlite",
        projections_path=path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        lazy=True,
    )
    assert isinstance(stores.projections, LazySQLiteAnalyticsProjectionStore)
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=2, queue_capacity=4)
    await scheduler.start(recover=True)
    await _wait_for_lazy_projection(scheduler, repositories)
    await scheduler.wait("account-a")

    build_count = 0
    original_build = pipeline._build

    def counted_build(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        return original_build(*args, **kwargs)

    pipeline._build = counted_build  # type: ignore[method-assign]
    replacement_path = tmp_path / "valid-empty-replacement.sqlite3"
    replacement = ProjectionsDatabase(replacement_path)
    current = stores.projections.database
    assert current is not None
    for database in (current, replacement):
        with database.read() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for candidate in (
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{replacement_path}-wal"),
        Path(f"{replacement_path}-shm"),
    ):
        candidate.unlink(missing_ok=True)
    os.replace(replacement_path, path)

    account = repositories.ingestion.account_read_model("account-a")
    with pytest.raises(ProjectionStorageUnavailable):
        await scheduler.active_projection("account-a", account)
    await scheduler.request_recovery("account-a", account.view_revision)
    await scheduler.request_recovery("account-a", account.view_revision)
    recovered = await _wait_for_lazy_projection(scheduler, repositories)
    assert recovered.source_revision == account.view_revision
    assert build_count == 1
    assert stores.projections.recovery_count == 2
    assert len(list(path.parent.glob(f".{path.name}.*.quarantine"))) == 1
    assert await scheduler.close(timeout=2)


@pytest.mark.asyncio
async def test_graph_digest_tamper_quarantines_projection_and_self_heals(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    repositories = prepare_empty_canonical(canonical_path)
    path = tmp_path / "projections.sqlite3"
    stores = create_analytics_stores(
        "sqlite",
        projections_path=path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        lazy=True,
    )
    assert isinstance(stores.projections, LazySQLiteAnalyticsProjectionStore)
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=2, queue_capacity=4)
    await scheduler.start(recover=True)
    await _wait_for_lazy_projection(scheduler, repositories)
    database = stores.projections.database
    assert database is not None
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER graph_node_building_update")
        connection.execute(
            """
            UPDATE graph_nodes SET properties_json='{"character_count":999}'
            WHERE node_id=(SELECT MIN(node_id) FROM graph_nodes)
            """
        )
    account = repositories.ingestion.account_read_model("account-a")
    with pytest.raises(ProjectionStorageUnavailable):
        await scheduler.active_projection("account-a", account)
    await scheduler.request_recovery("account-a", account.view_revision)
    recovered = await _wait_for_lazy_projection(scheduler, repositories)
    assert recovered.source_revision == account.view_revision
    assert stores.projections.recovery_count == 2
    assert list(path.parent.glob(f".{path.name}.*.quarantine"))
    assert await scheduler.close(timeout=2)


@pytest.mark.asyncio
async def test_non_sqlite_projection_file_cannot_block_canonical_readiness(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    repositories = prepare_empty_canonical(canonical_path)
    path = tmp_path / "projections.sqlite3"
    path.write_bytes(b"synthetic-not-a-sqlite-projection")
    assert repositories.ingestion.account_read_model("account-a").view_revision == 0
    stores = create_analytics_stores(
        "sqlite",
        projections_path=path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        lazy=True,
    )
    assert isinstance(stores.projections, LazySQLiteAnalyticsProjectionStore)
    scheduler = InProcessProjectionScheduler(
        AnalyticsPipeline(
            repositories.ingestion,
            projections=stores.projections,
            graph=stores.graph,
        )
    )
    await scheduler.start(recover=True)
    projection = await _wait_for_lazy_projection(scheduler, repositories)
    assert projection.source_revision == 0
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    quarantines = list(path.parent.glob(f".{path.name}.*.quarantine"))
    assert quarantines and quarantines[0].read_bytes() == b"synthetic-not-a-sqlite-projection"
    assert await scheduler.close(timeout=2)


def test_retired_generation_gc_is_bounded_and_preserves_pending(
    tmp_path: Path,
) -> None:
    repositories = prepare_empty_canonical(tmp_path / "canonical.sqlite3")
    store = make_store(
        tmp_path / "projections.sqlite3",
        repositories,
        rollback_retention=1,
        gc_batch_size=2,
    )
    pipeline = pipeline_for(repositories, store)
    pipeline.project_account("account-a")
    for revision in range(1, 5):
        advance(repositories, revision)
        pipeline.project_account("account-a")
    advance(repositories, 5)
    pending = pipeline.build_candidate("account-a")
    store.collect_garbage(account_ref("account-a"))

    generations = store.database.generations("account-a")
    assert sum(item.status == "active" for item in generations) == 1
    assert sum(item.status == "validated" for item in generations) == 1
    assert sum(item.status == "retired" for item in generations) <= 1
    assert any(item.generation_id == pending.staged_generation_id for item in generations)
    pipeline.discard_candidate(pending)


@pytest.mark.skip(
    reason="Exercises the canonical ingestion write-path "
    "(SQLiteIngestionRepository over canonical_chats/canonical_messages) and the "
    "unified single-model IngestSnapshotPayload. This branch still carries the "
    "protocol-v2 canonical schema and the discriminated-union payload, so this "
    "belongs to the later backend ingestion port."
)
@pytest.mark.asyncio
async def test_deleting_projections_allows_deterministic_canonical_rebuild(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path
    )
    payload = IngestSnapshotPayload.model_validate_json(
        (FIXTURES / "creator-beta.snapshot.json").read_text(encoding="utf-8")
    )
    outcome = await IngestionService(repositories.ingestion).ingest_snapshot(
        StreamKey(
            payload.creator_account_id,
            payload.agent_installation_id,
            payload.agent_stream_id,
        ),
        payload,
    )
    assert outcome.status == "accepted"

    def build() -> RebuildArtifact:
        stores = create_analytics_stores(
            "sqlite",
            projections_path=projections_path,
            activation=repositories.projection_activation,
            canonical_identity_reader=identity_reader(repositories),
        )
        return AnalyticsPipeline(
            repositories.ingestion,
            projections=stores.projections,
            graph=stores.graph,
        ).rebuild_account(payload.creator_account_id).artifact

    first = build()
    projections_path.unlink()
    rebuilt = build()
    assert rebuilt == first
