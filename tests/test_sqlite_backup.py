from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from app.analytics.factory import AnalyticsStores, create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.models.analytics import GraphAlgorithmBounds
from app.persistence import backup as backup_module
from app.persistence.backup import (
    SQLiteBackupError,
    backup_canonical_database,
    backup_projections_database,
    restore_backup,
    restore_backup_pair,
    verify_backup,
)
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.history import HistoryRepository, StreamKey
from app.persistence.private_files import _windows_acl_is_owner_only
from app.protocol.payloads import (
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"


@dataclass
class Runtime:
    canonical_path: Path
    projections_path: Path
    repositories: CanonicalRepositories
    stores: AnalyticsStores
    creator_account_id: str
    artifact: object


def identity_reader(repositories: CanonicalRepositories):
    def read(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    return read


def _fixture_identity(document: dict) -> dict:
    return {
        "connection_id": UUID(document["connection_id"]),
        "fencing_token": document["fencing_token"],
        "creator_account_id": document["creator_account_id"],
        "agent_installation_id": UUID(document["agent_installation_id"]),
        "agent_stream_id": UUID(document["agent_stream_id"]),
        "snapshot_id": UUID(document["snapshot_id"]),
    }


def seed_canonical_snapshot(history: HistoryRepository, fixture_name: str) -> str:
    """Write one fixture snapshot through the signer-v2 chunked canonical path."""
    document = json.loads(
        (FIXTURES / f"{fixture_name}.snapshot.json").read_text(encoding="utf-8")
    )
    identity = _fixture_identity(document)
    key = StreamKey(
        identity["creator_account_id"],
        identity["agent_installation_id"],
        identity["agent_stream_id"],
    )
    chats = document["chats"]
    messages = document["messages"]
    begin = IngestSnapshotBeginPayload(
        **identity,
        frame_kind="begin",
        through_seq=0,
        chunk_count=2,
        record_counts=SnapshotRecordCounts(
            chats=len(chats), messages=len(messages), coverage_evidence=0
        ),
        max_frame_bytes=524288,
    )
    assert history.begin_snapshot(key, begin).status == "accepted"
    chat_chunk = IngestSnapshotChunkPayload(
        **identity,
        frame_kind="chunk",
        chunk_index=0,
        entity_kind="chat",
        records=[
            {
                "tombstone": False,
                "chat": {
                    "record_kind": "full",
                    "chat_id": item["chat_id"],
                    "platform_user_id": item["platform_user_id"],
                    "display_name": item.get("display_name"),
                    "updated_at": item["updated_at"],
                },
            }
            for item in chats
        ],
    )
    assert history.add_snapshot_chunk(key, chat_chunk).status == "accepted"
    message_chunk = IngestSnapshotChunkPayload(
        **identity,
        frame_kind="chunk",
        chunk_index=1,
        entity_kind="message",
        records=[
            {
                "tombstone": False,
                "message": {
                    "message_id": item["message_id"],
                    "chat_id": item["chat_id"],
                    "sender_platform_user_id": item["sender_platform_user_id"],
                    "text": item["text"],
                    "sent_at": item["sent_at"],
                    "direction": item["direction"],
                },
            }
            for item in messages
        ],
    )
    assert history.add_snapshot_chunk(key, message_chunk).status == "accepted"
    commit = IngestSnapshotCommitPayload(**identity, frame_kind="commit", chunk_count=2)
    assert history.commit_snapshot(key, commit).status == "accepted"
    return identity["creator_account_id"]


async def seeded_runtime(tmp_path: Path) -> Runtime:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "analytics-projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path
    )
    creator_account_id = seed_canonical_snapshot(repositories.history, "creator-beta")
    stores = create_analytics_stores(
        "sqlite",
        projections_path=projections_path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
    )
    artifact = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    ).project_account(creator_account_id).artifact
    return Runtime(
        canonical_path,
        projections_path,
        repositories,
        stores,
        creator_account_id,
        artifact,
    )


def refresh_external_hash(backup_path: Path) -> None:
    manifest_path = backup_path.with_name(backup_path.name + ".manifest.json")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["file_sha256"] = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def populate_algorithm_metric(runtime: Runtime) -> None:
    partition_ref = account_ref(runtime.creator_account_id)
    result = runtime.stores.graph.compute_centrality(
        partition_ref,
        algorithm="degree",
        bounds=GraphAlgorithmBounds(
            creator_account_id=partition_ref,
            start_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2100, 1, 1, tzinfo=timezone.utc),
            max_hops=8,
            max_nodes=500,
            max_edges=2_000,
            max_queue=500,
        ),
        seed=1729,
    )
    assert result.parameter_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_canonical_only_restore_discards_projection_and_rebuilds(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    backup_path = tmp_path / "canonical.backup.sqlite3"
    manifest = backup_canonical_database(
        runtime.repositories.database, backup_path
    )
    assert verify_backup(backup_path, expected_store="canonical") == manifest
    identity = identity_reader(runtime.repositories)(
        runtime.creator_account_id
    )
    assert identity is not None
    assert manifest.high_water["account_identities"] == {
        runtime.creator_account_id: {
            "revision": identity.revision,
            "content_digest": identity.content_digest,
        }
    }

    restored_dir = tmp_path / "restored"
    restored_projection_path = restored_dir / "analytics-projections.sqlite3"
    restored_projection_path.parent.mkdir(parents=True)
    restored_projection_path.write_bytes(b"stale disposable projection")
    restored_canonical_path = restored_dir / "canonical.sqlite3"
    restore_backup(
        backup_path,
        restored_canonical_path,
        expected_store="canonical",
        discard_projections_path=restored_projection_path,
    )
    assert not restored_projection_path.exists()

    restored = create_canonical_repositories(
        "sqlite", canonical_path=restored_canonical_path
    )
    restored_stores = create_analytics_stores(
        "sqlite",
        projections_path=restored_projection_path,
        activation=restored.projection_activation,
        canonical_identity_reader=identity_reader(restored),
    )
    rebuilt = AnalyticsPipeline(
        restored.ingestion,
        projections=restored_stores.projections,
        graph=restored_stores.graph,
    ).rebuild_account(runtime.creator_account_id).artifact
    assert rebuilt == runtime.artifact


@pytest.mark.asyncio
async def test_matching_backup_pair_round_trips_full_completed_witness(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    canonical_backup = tmp_path / "canonical.backup.sqlite3"
    projection_backup = tmp_path / "projections.backup.sqlite3"
    canonical_manifest = backup_canonical_database(
        runtime.repositories.database, canonical_backup
    )
    projection_manifest = backup_projections_database(
        runtime.stores.database, projection_backup
    )
    active = projection_manifest.high_water["active_generations"][0]
    witness = canonical_manifest.canonical_witnesses[0]
    for key in (
        "account_ref",
        "generation_id",
        "canonical_revision",
        "canonical_content_digest",
        "projection_digest",
        "graph_digest",
        "pipeline_revision",
        "pipeline_config_digest",
        "pipeline_identity_digest",
        "witness_sequence",
    ):
        assert active[key] == witness[key]
    assert active["intent_id"] == witness["intent_id"]

    restored_canonical = tmp_path / "pair" / "canonical.sqlite3"
    restored_projection = tmp_path / "pair" / "analytics-projections.sqlite3"
    _, restored_projection_manifest = restore_backup_pair(
        canonical_backup,
        restored_canonical,
        projections_backup=projection_backup,
        projections_destination=restored_projection,
    )
    assert restored_projection_manifest == projection_manifest
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=restored_canonical
    )
    stores = create_analytics_stores(
        "sqlite",
        projections_path=restored_projection,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
    )
    assert isinstance(stores.projections, SQLiteAnalyticsProjectionStore)
    assert stores.projections.get_artifact(
        runtime.creator_account_id
    ) == runtime.artifact


@pytest.mark.asyncio
async def test_mismatched_backup_pair_exposes_no_projection_and_rebuilds(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    projection_backup = tmp_path / "old-projections.backup.sqlite3"
    backup_projections_database(runtime.stores.database, projection_backup)
    with runtime.repositories.database.transaction() as connection:
        connection.execute(
            """
            UPDATE account_heads SET canonical_revision=2
            WHERE creator_account_id=?
            """,
            (runtime.creator_account_id,),
        )
    canonical_backup = tmp_path / "new-canonical.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, canonical_backup)

    restored_canonical = tmp_path / "mismatch" / "canonical.sqlite3"
    restored_projection = tmp_path / "mismatch" / "analytics-projections.sqlite3"
    restored_projection.parent.mkdir(parents=True)
    restored_projection.write_bytes(b"must be discarded")
    _, projection_manifest = restore_backup_pair(
        canonical_backup,
        restored_canonical,
        projections_backup=projection_backup,
        projections_destination=restored_projection,
    )
    assert projection_manifest is None
    assert not restored_projection.exists()

    repositories = create_canonical_repositories(
        "sqlite", canonical_path=restored_canonical
    )
    stores = create_analytics_stores(
        "sqlite",
        projections_path=restored_projection,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
    )
    assert stores.projections.get(runtime.creator_account_id) is None
    rebuilt = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    ).project_account(runtime.creator_account_id)
    assert rebuilt.artifact.projection.source_revision == 2


@pytest.mark.asyncio
async def test_projection_backup_recomputes_rows_and_rejects_property_tamper(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.stores.database is not None
    backup_path = tmp_path / "projections.backup.sqlite3"
    backup_projections_database(runtime.stores.database, backup_path)
    marker = "SYNTHETIC-TAMPERED-PROPERTY-991"
    connection = sqlite3.connect(backup_path)
    try:
        connection.execute("DROP TRIGGER graph_node_building_update")
        connection.execute(
            """
            UPDATE graph_nodes SET properties_json=?
            WHERE node_id=(SELECT MIN(node_id) FROM graph_nodes)
            """,
            (json.dumps({"unsafe_marker": marker}),),
        )
        connection.commit()
    finally:
        connection.close()
    refresh_external_hash(backup_path)

    with pytest.raises(SQLiteBackupError, match="rows do not match digests"):
        verify_backup(backup_path, expected_store="projections")


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["result", "parameter_hash"])
async def test_projection_backup_rejects_algorithm_metric_tamper_after_sha_refresh(
    tmp_path: Path, tamper: str
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.stores.database is not None
    populate_algorithm_metric(runtime)
    backup_path = tmp_path / "projections.backup.sqlite3"
    backup_projections_database(runtime.stores.database, backup_path)

    connection = sqlite3.connect(backup_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("DROP TRIGGER graph_metric_update_blocked")
        row = connection.execute(
            "SELECT * FROM graph_algorithm_metrics LIMIT 1"
        ).fetchone()
        assert row is not None
        if tamper == "result":
            document = json.loads(row["result_json"])
            document["algorithm"] = "tampered"
            connection.execute(
                "UPDATE graph_algorithm_metrics SET result_json=?",
                (json.dumps(document, sort_keys=True),),
            )
        else:
            replacement = "sha256:" + "0" * 64
            assert replacement != row["parameter_hash"]
            connection.execute(
                "UPDATE graph_algorithm_metrics SET parameter_hash=?",
                (replacement,),
            )
        connection.commit()
    finally:
        connection.close()
    refresh_external_hash(backup_path)

    with pytest.raises(SQLiteBackupError, match="rows do not match digests"):
        verify_backup(backup_path, expected_store="projections")


@pytest.mark.asyncio
async def test_backup_verifies_external_hash_migration_checksums_and_limitation(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    backup_path = tmp_path / "canonical.backup.sqlite3"
    manifest = backup_canonical_database(runtime.repositories.database, backup_path)
    external_path = backup_path.with_name(backup_path.name + ".manifest.json")
    external = json.loads(external_path.read_text(encoding="utf-8"))
    assert external["file_sha256"] == hashlib.sha256(
        backup_path.read_bytes()
    ).hexdigest()
    assert external["integrity_limitation"] == (
        "SHA-256 detects accidental or uncoordinated changes; it is not authenticity."
    )
    assert verify_backup(backup_path) == manifest

    connection = sqlite3.connect(backup_path)
    try:
        connection.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=1",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    refresh_external_hash(backup_path)
    with pytest.raises(SQLiteBackupError, match="migration checksum"):
        verify_backup(backup_path, expected_store="canonical")


@pytest.mark.asyncio
async def test_restore_rejects_aliases_and_open_application_handles(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.stores.database is not None
    backup_path = tmp_path / "projections.backup.sqlite3"
    backup_projections_database(runtime.stores.database, backup_path)
    with pytest.raises(SQLiteBackupError, match="alias"):
        restore_backup(
            backup_path,
            backup_path,
            expected_store="projections",
            overwrite=True,
        )

    connection = runtime.stores.database.connect()
    try:
        with pytest.raises(SQLiteBackupError, match="in use"):
            restore_backup(
                backup_path,
                runtime.projections_path,
                expected_store="projections",
                overwrite=True,
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_backup_rejects_hardlink_to_live_database_before_mutation(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    alias = tmp_path / "live-database-hardlink.sqlite3"
    os.link(runtime.canonical_path, alias)
    original = runtime.canonical_path.read_bytes()

    with pytest.raises(SQLiteBackupError, match="alias"):
        backup_canonical_database(
            runtime.repositories.database,
            alias,
            overwrite=True,
        )

    assert runtime.canonical_path.read_bytes() == original
    assert alias.read_bytes() == original


@pytest.mark.asyncio
async def test_restore_rejects_discard_manifest_alias_before_mutation(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    backup_path = tmp_path / "canonical.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, backup_path)
    manifest_path = backup_path.with_name(backup_path.name + ".manifest.json")
    destination = tmp_path / "destination.sqlite3"
    destination.write_bytes(b"destination-sentinel")
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(SQLiteBackupError, match="alias"):
        restore_backup(
            backup_path,
            destination,
            expected_store="canonical",
            overwrite=True,
            discard_projections_path=manifest_path,
        )

    assert destination.read_bytes() == b"destination-sentinel"
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.asyncio
async def test_paired_restore_rejects_cross_destination_hardlinks_atomically(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    canonical_backup = tmp_path / "canonical.backup.sqlite3"
    projection_backup = tmp_path / "projections.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, canonical_backup)
    backup_projections_database(runtime.stores.database, projection_backup)

    target_dir = tmp_path / "targets"
    target_dir.mkdir()
    canonical_target = target_dir / "canonical.sqlite3"
    projection_target = target_dir / "analytics-projections.sqlite3"
    canonical_target.write_bytes(b"paired-target-sentinel")
    os.link(canonical_target, projection_target)

    with pytest.raises(SQLiteBackupError, match="alias"):
        restore_backup_pair(
            canonical_backup,
            canonical_target,
            projections_backup=projection_backup,
            projections_destination=projection_target,
            overwrite=True,
        )

    assert canonical_target.read_bytes() == b"paired-target-sentinel"
    assert projection_target.read_bytes() == b"paired-target-sentinel"


@pytest.mark.asyncio
async def test_paired_restore_honors_overwrite_false_for_each_destination(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    canonical_backup = tmp_path / "canonical.backup.sqlite3"
    projection_backup = tmp_path / "projections.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, canonical_backup)
    backup_projections_database(runtime.stores.database, projection_backup)

    first = tmp_path / "first"
    first.mkdir()
    first_canonical = first / "canonical.sqlite3"
    first_projection = first / "analytics-projections.sqlite3"
    first_canonical.write_bytes(b"canonical-sentinel")
    with pytest.raises(FileExistsError):
        restore_backup_pair(
            canonical_backup,
            first_canonical,
            projections_backup=projection_backup,
            projections_destination=first_projection,
            overwrite=False,
        )
    assert first_canonical.read_bytes() == b"canonical-sentinel"
    assert not first_projection.exists()

    second = tmp_path / "second"
    second.mkdir()
    second_canonical = second / "canonical.sqlite3"
    second_projection = second / "analytics-projections.sqlite3"
    second_projection.write_bytes(b"projection-sentinel")
    with pytest.raises(FileExistsError):
        restore_backup_pair(
            canonical_backup,
            second_canonical,
            projections_backup=projection_backup,
            projections_destination=second_projection,
            overwrite=False,
        )
    assert not second_canonical.exists()
    assert second_projection.read_bytes() == b"projection-sentinel"


@pytest.mark.asyncio
async def test_projection_publication_failure_leaves_restored_authority_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    canonical_backup = tmp_path / "canonical.backup.sqlite3"
    projection_backup = tmp_path / "projections.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, canonical_backup)
    backup_projections_database(runtime.stores.database, projection_backup)
    canonical_target = tmp_path / "restored" / "canonical.sqlite3"
    projection_target = tmp_path / "restored" / "analytics-projections.sqlite3"
    original_publish = backup_module._publish_staged_file
    publications = 0

    def fail_second_publication(temporary: Path, destination: Path) -> None:
        nonlocal publications
        publications += 1
        if publications == 2:
            raise SQLiteBackupError("synthetic_projection_publication_failure")
        original_publish(temporary, destination)

    monkeypatch.setattr(
        backup_module,
        "_publish_staged_file",
        fail_second_publication,
    )
    with pytest.raises(
        SQLiteBackupError, match="synthetic_projection_publication_failure"
    ):
        restore_backup_pair(
            canonical_backup,
            canonical_target,
            projections_backup=projection_backup,
            projections_destination=projection_target,
        )

    assert canonical_target.exists()
    assert not projection_target.exists()
    restored = create_canonical_repositories(
        "sqlite", canonical_path=canonical_target
    )
    assert identity_reader(restored)(runtime.creator_account_id) == (
        identity_reader(runtime.repositories)(runtime.creator_account_id)
    )


@pytest.mark.asyncio
async def test_backup_source_symlink_or_reparse_alias_is_rejected_when_supported(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    backup_path = tmp_path / "canonical.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, backup_path)
    alias = tmp_path / "canonical-link.sqlite3"
    try:
        alias.symlink_to(backup_path)
    except OSError:
        pytest.skip("file symlink creation is unavailable")

    with pytest.raises(SQLiteBackupError, match="unsafe"):
        verify_backup(alias, expected_store="canonical")


@pytest.mark.skipif(os.name != "nt", reason="Windows open-handle semantics")
@pytest.mark.asyncio
async def test_windows_raw_open_handle_refuses_restore(tmp_path: Path) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.stores.database is not None
    backup_path = tmp_path / "projections.backup.sqlite3"
    backup_projections_database(runtime.stores.database, backup_path)
    raw = sqlite3.connect(runtime.projections_path)
    try:
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("BEGIN")
        raw.execute("SELECT COUNT(*) FROM projection_generations").fetchone()
        with pytest.raises(SQLiteBackupError):
            restore_backup(
                backup_path,
                runtime.projections_path,
                expected_store="projections",
                overwrite=True,
            )
    finally:
        raw.rollback()
        raw.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
@pytest.mark.asyncio
async def test_windows_canonical_projection_and_backup_files_are_private(
    tmp_path: Path,
) -> None:
    runtime = await seeded_runtime(tmp_path / "source")
    assert runtime.repositories.database is not None
    assert runtime.stores.database is not None
    canonical_backup = tmp_path / "canonical.backup.sqlite3"
    projection_backup = tmp_path / "projections.backup.sqlite3"
    backup_canonical_database(runtime.repositories.database, canonical_backup)
    backup_projections_database(runtime.stores.database, projection_backup)
    candidates = [
        runtime.canonical_path,
        runtime.projections_path,
        canonical_backup,
        projection_backup,
        canonical_backup.with_name(canonical_backup.name + ".manifest.json"),
        projection_backup.with_name(projection_backup.name + ".manifest.json"),
    ]
    assert all(_windows_acl_is_owner_only(path) for path in candidates)
