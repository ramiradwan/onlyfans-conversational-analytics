"""Protocol-v2 durable ingestion regressions.

The retired monolithic v1 in-memory service is intentionally not exercised;
these tests use the authoritative HistoryRepository contract.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from app.persistence.factory import create_canonical_repositories
from app.persistence.history import StreamKey
from app.protocol import AGENT_TO_BRAIN_ADAPTER


ACCOUNT = "dev-creator-account"
INSTALLATION = UUID("20000000-0000-4000-8000-000000000001")
STREAM = UUID("30000000-0000-4000-8000-000000000001")


def payload(message_type: str, document: dict):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(
            {
                "type": message_type,
                "protocol_version": "2",
                "message_id": str(uuid4()),
                "payload": document,
            }
        )
    ).payload


def identity(snapshot_id: UUID) -> dict:
    return {
        "connection_id": str(uuid4()),
        "fencing_token": "fence-test",
        "creator_account_id": ACCOUNT,
        "agent_installation_id": str(INSTALLATION),
        "agent_stream_id": str(STREAM),
        "snapshot_id": str(snapshot_id),
    }


def commit_seed(repository) -> StreamKey:
    key = StreamKey(ACCOUNT, INSTALLATION, STREAM)
    snapshot_id = uuid4()
    begin = payload(
        "ingest.snapshot",
        {
            **identity(snapshot_id),
            "frame_kind": "begin",
            "through_seq": 10,
            "chunk_count": 1,
            "record_counts": {"chats": 1, "messages": 0, "coverage_evidence": 0},
            "max_frame_bytes": 524288,
        },
    )
    chunk = payload(
        "ingest.snapshot",
        {
            **identity(snapshot_id),
            "frame_kind": "chunk",
            "chunk_index": 0,
            "entity_kind": "chat",
            "records": [
                {
                    "tombstone": False,
                    "chat": {
                        "record_kind": "full",
                        "chat_id": "chat-1",
                        "platform_user_id": "fan-1",
                        "display_name": "Fan",
                        "updated_at": "2026-07-19T10:00:00Z",
                    },
                }
            ],
        },
    )
    commit = payload(
        "ingest.snapshot",
        {**identity(snapshot_id), "frame_kind": "commit", "chunk_count": 1},
    )
    assert repository.begin_snapshot(key, begin).status == "accepted"
    assert repository.add_snapshot_chunk(key, chunk).status == "accepted"
    assert repository.commit_snapshot(key, commit).status == "accepted"
    return key


def delta(sequence: int, event_id: UUID, text: str = "Hello"):
    return payload(
        "ingest.delta",
        {
            "connection_id": str(uuid4()),
            "fencing_token": "fence-test",
            "creator_account_id": ACCOUNT,
            "agent_installation_id": str(INSTALLATION),
            "agent_stream_id": str(STREAM),
            "event_id": str(event_id),
            "source_seq": sequence,
            "acquisition_origin": "passive",
            "change": {
                "type": "message.upsert",
                "message": {
                    "message_id": "message-1",
                    "chat_id": "chat-1",
                    "sender_platform_user_id": "fan-1",
                    "text": text,
                    "sent_at": "2026-07-19T10:01:00Z",
                    "direction": "inbound",
                },
            },
        },
    )


def test_duplicate_event_ack_and_gap_leave_checkpoint_unchanged() -> None:
    repositories = create_canonical_repositories("memory")
    key = commit_seed(repositories.history)
    event_id = uuid4()
    assert repositories.history.commit_delta(key, delta(11, event_id)).status == "accepted"
    duplicate = repositories.history.commit_delta(key, delta(11, event_id))
    assert (duplicate.status, duplicate.committed_source_seq) == ("duplicate", 11)
    gap = repositories.history.commit_delta(key, delta(13, uuid4(), "Later"))
    assert (gap.status, gap.code, gap.retryable) == ("gap", "sequence_gap", True)
    assert repositories.history.checkpoint(key) == 11


def test_same_message_identifier_with_different_material_always_rejects() -> None:
    repositories = create_canonical_repositories("memory")
    key = commit_seed(repositories.history)
    assert repositories.history.commit_delta(key, delta(11, uuid4())).status == "accepted"
    try:
        repositories.history.commit_delta(key, delta(12, uuid4(), "Edited"))
    except ValueError as error:
        assert "immutable message identifier" in str(error)
    else:
        raise AssertionError("conflicting immutable message material was accepted")
    assert repositories.history.checkpoint(key) == 11
