#!/usr/bin/env python3
"""Execute observational RET-001 companion resurrection scenarios.

This runner intentionally uses existing production persistence/backup paths plus
synthetic fixture data. Direct SQL deletion is confined to this characterization
process and is not a production deletion API or retention implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

# Configure deterministic non-production local encryption before importing the
# persistence modules. Production code continues to require Windows DPAPI.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "OFCA_TEST_DATABASE_MASTER_KEY_HEX",
    "4d7d278b49d90e4d747ac36d4f65661ec089df85a8f6f22851a628c880f7a4e2",
)

from app.persistence import backup as backup_module  # noqa: E402
from app.persistence.backup import (  # noqa: E402
    backup_canonical_database,
    restore_backup,
    verify_backup,
)
from app.persistence.factory import create_canonical_repositories  # noqa: E402
from app.persistence.history import HistoryRepository, StreamKey  # noqa: E402
from app.protocol.payloads import (  # noqa: E402
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)

RESULT_SCHEMA = "ofca-ret-001-resurrection-scenario-result/v1"
RUNNER_ID = "ret-001-companion-observations/v1"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "analytics" / "creator-beta.snapshot.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def seed_canonical_snapshot(history: HistoryRepository) -> tuple[str, dict]:
    document = load_fixture()
    identity = {
        "connection_id": UUID(document["connection_id"]),
        "fencing_token": document["fencing_token"],
        "creator_account_id": document["creator_account_id"],
        "agent_installation_id": UUID(document["agent_installation_id"]),
        "agent_stream_id": UUID(document["agent_stream_id"]),
        "snapshot_id": UUID(document["snapshot_id"]),
    }
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
    commit = IngestSnapshotCommitPayload(
        **identity, frame_kind="commit", chunk_count=2
    )
    assert history.commit_snapshot(key, commit).status == "accepted"
    return identity["creator_account_id"], messages[0]


def canonical_message(database, account_id: str, message_id: str) -> dict | None:
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT message_id, chat_id, text, sent_at, direction
              FROM account_messages
             WHERE creator_account_id=? AND message_id=?
            """,
            (account_id, message_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "message_id": str(row[0]),
        "chat_id": str(row[1]),
        "text_sha256": sha256_text(str(row[2])),
        "sent_at": str(row[3]),
        "direction": str(row[4]),
    }


def projection_message(database, account_id: str, message_id: str) -> dict | None:
    with database.read() as connection:
        row = connection.execute(
            """
            SELECT message_id, conversation_id, text, sent_at, direction, projection_slot
              FROM projection_messages
             WHERE creator_account_id=? AND message_id=?
             ORDER BY projection_slot DESC
             LIMIT 1
            """,
            (account_id, message_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "message_id": str(row[0]),
        "conversation_id": str(row[1]),
        "text_sha256": sha256_text(str(row[2])),
        "sent_at": str(row[3]),
        "direction": str(row[4]),
        "projection_slot": int(row[5]),
    }


def delete_canonical_message(database, account_id: str, message_id: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM account_messages WHERE creator_account_id=? AND message_id=?",
            (account_id, message_id),
        )


def delete_projection_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def observation(requirement_id: str, evidence_id: str, facts: dict) -> dict:
    return {
        "relationship_requirement_id": requirement_id,
        "outcome": "OBSERVED",
        "evidence_ids": [evidence_id],
        "facts": facts,
    }


def result_document(
    scenario_id: str,
    product_revision: str,
    result_status: str,
    observations: list[dict],
    *,
    notes: list[str],
) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "scenario_id": scenario_id,
        "product_revision": product_revision,
        "executed_at": utc_now(),
        "result_status": result_status,
        "observations": observations,
        "execution_context": {
            "runner": RUNNER_ID,
            "fixture": str(FIXTURE_PATH.relative_to(ROOT)).replace("\\", "/"),
            "synthetic_fixture": True,
            "production_lifecycle_change_authorized": False,
            "test_only_direct_sql_deletion": True,
        },
        "notes": notes,
    }


def run_projection_rebuild(root: Path, product_revision: str) -> dict:
    scenario_root = root / "res-003"
    scenario_root.mkdir(parents=True, exist_ok=True)
    canonical_path = scenario_root / "canonical.sqlite3"
    projection_path = scenario_root / "projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    account_id, fixture_message = seed_canonical_snapshot(repositories.history)
    assert repositories.projection.catch_up(account_id) is not None
    before = projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert before is not None

    delete_projection_files(projection_path)
    assert not projection_path.exists()

    recovered = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    recovered.projection.catch_up(account_id)
    after = projection_message(
        recovered.projection_database, account_id, fixture_message["message_id"]
    )
    assert after is not None
    assert after["message_id"] == before["message_id"]
    assert after["text_sha256"] == before["text_sha256"]

    return result_document(
        "RES-003-PROJECTION-REBUILD",
        product_revision,
        "OBSERVED_REENTRY",
        [
            observation(
                "RES-003-R1",
                "EXEC-RES-003-PROJECTION-REBUILD",
                {
                    "deleted_store": "projections.sqlite3",
                    "canonical_message_survived": canonical_message(
                        recovered.database, account_id, fixture_message["message_id"]
                    )
                    is not None,
                    "projection_message_before": before,
                    "projection_message_after_rebuild": after,
                    "recreated_same_text_sha256": after["text_sha256"]
                    == before["text_sha256"],
                },
            )
        ],
        notes=[
            "The disposable Bridge projection file was removed only inside this synthetic characterization run.",
            "No production deletion or retention control was added or invoked.",
        ],
    )


def run_backup_restore(root: Path, product_revision: str) -> dict:
    scenario_root = root / "res-005"
    scenario_root.mkdir(parents=True, exist_ok=True)
    canonical_path = scenario_root / "canonical.sqlite3"
    projection_path = scenario_root / "projections.sqlite3"
    backup_path = scenario_root / "canonical.backup.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    account_id, fixture_message = seed_canonical_snapshot(repositories.history)
    before = canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    assert before is not None

    manifest = backup_canonical_database(repositories.database, backup_path)
    assert verify_backup(backup_path, expected_store="canonical") == manifest
    backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()

    delete_canonical_message(repositories.database, account_id, fixture_message["message_id"])
    assert canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    ) is None

    staging_observation = {"temporary_seen_before_publish": False}
    original_publish = backup_module._publish_staged_file

    def observing_publish(temporary, destination):
        staging_observation["temporary_seen_before_publish"] = Path(temporary).exists()
        staging_observation["temporary_name_suffix"] = ".restore.tmp"
        return original_publish(temporary, destination)

    backup_module._publish_staged_file = observing_publish
    try:
        restore_backup(
            backup_path,
            canonical_path,
            expected_store="canonical",
            overwrite=True,
            discard_projections_path=projection_path,
        )
    finally:
        backup_module._publish_staged_file = original_publish

    restored = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    after = canonical_message(restored.database, account_id, fixture_message["message_id"])
    assert after is not None
    assert after["message_id"] == before["message_id"]
    assert after["text_sha256"] == before["text_sha256"]
    assert staging_observation["temporary_seen_before_publish"] is True

    return result_document(
        "RES-005-ORDINARY-BACKUP-RESTORE",
        product_revision,
        "OBSERVED_REENTRY",
        [
            observation(
                "RES-005-R1",
                "EXEC-RES-005-BACKUP-COPY",
                {
                    "verified_backup_sha256": backup_sha256,
                    "backup_store_name": manifest.store_name,
                    "message_before_test_only_delete": before,
                    "message_restored_from_backup": after,
                    "restored_same_text_sha256": after["text_sha256"]
                    == before["text_sha256"],
                },
            ),
            observation(
                "RES-005-R2",
                "EXEC-RES-005-RESTORE-STAGING",
                {
                    **staging_observation,
                    "restore_completed": True,
                    "message_present_after_restore": True,
                },
            ),
        ],
        notes=[
            "The source message row was deleted directly in the isolated test database solely to characterize existing restore behavior.",
            "The direct SQL mutation is not a product deletion API and does not select a retention design.",
        ],
    )


def run_derived_survival(root: Path, product_revision: str) -> dict:
    scenario_root = root / "res-009"
    scenario_root.mkdir(parents=True, exist_ok=True)
    canonical_path = scenario_root / "canonical.sqlite3"
    projection_path = scenario_root / "projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=canonical_path,
        projection_path=projection_path,
    )
    account_id, fixture_message = seed_canonical_snapshot(repositories.history)
    assert repositories.projection.catch_up(account_id) is not None
    projection_before = projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    canonical_before = canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    assert projection_before is not None
    assert canonical_before is not None

    delete_canonical_message(repositories.database, account_id, fixture_message["message_id"])
    canonical_after = canonical_message(
        repositories.database, account_id, fixture_message["message_id"]
    )
    projection_after = projection_message(
        repositories.projection_database, account_id, fixture_message["message_id"]
    )
    assert canonical_after is None
    assert projection_after is not None
    assert projection_after["text_sha256"] == projection_before["text_sha256"]

    document = result_document(
        "RES-009-DERIVED-STORE-REBUILD-AFTER-SOURCE-DELETE",
        product_revision,
        "NO_REENTRY_OBSERVED",
        [
            observation(
                "RES-009-R1",
                "EXEC-RES-009-DERIVED-SURVIVAL",
                {
                    "canonical_message_before": canonical_before,
                    "canonical_message_present_after_test_only_delete": False,
                    "projection_message_before": projection_before,
                    "projection_message_after_source_delete": projection_after,
                    "projection_retained_same_text_sha256": projection_after["text_sha256"]
                    == projection_before["text_sha256"],
                },
            )
        ],
        notes=[
            "This scenario observes surviving derived Bridge projection information after an isolated test-only canonical row deletion.",
            "It does not claim that the projection can reverse-restore the canonical source.",
        ],
    )
    document["characterization_outcome"] = "OBSERVED_EQUIVALENT_INFORMATION_SURVIVAL"
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.product_revision) != 40 or any(
        character not in "0123456789abcdef" for character in args.product_revision
    ):
        raise SystemExit("--product-revision must be a lowercase 40-hex commit SHA")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="ret-001-companion-observations-") as temporary:
        root = Path(temporary)
        results = [
            run_projection_rebuild(root, args.product_revision),
            run_backup_restore(root, args.product_revision),
            run_derived_survival(root, args.product_revision),
        ]

    for result in results:
        destination = args.output_dir / f"{result['scenario_id'].lower()}.json"
        destination.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    summary = {
        "schema": "ofca-ret-001-companion-observation-run/v1",
        "product_revision": args.product_revision,
        "runner": RUNNER_ID,
        "executed_scenario_ids": sorted(result["scenario_id"] for result in results),
        "result_statuses": {
            result["scenario_id"]: result["result_status"] for result in results
        },
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
