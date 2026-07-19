from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.persistence.factory import create_canonical_repositories
from app.persistence.history import InvariantViolation, StreamKey
from app.protocol import AGENT_TO_BRAIN_ADAPTER


ACCOUNT_ID = "dev-creator-account"
INSTALLATION_ID = UUID("20000000-0000-4000-8000-000000000001")
STREAM_ID = UUID("30000000-0000-4000-8000-000000000001")
CONNECTION_ID = "10000000-0000-4000-8000-000000000001"


def message(message_type: str, payload: dict):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(
            {
                "type": message_type,
                "protocol_version": "2",
                "message_id": str(uuid4()),
                "payload": payload,
            }
        )
    )


def identity(*, stream_id: UUID = STREAM_ID, snapshot_id: UUID | None = None) -> dict:
    value = {
        "connection_id": CONNECTION_ID,
        "fencing_token": "fence-test",
        "creator_account_id": ACCOUNT_ID,
        "agent_installation_id": str(INSTALLATION_ID),
        "agent_stream_id": str(stream_id),
    }
    if snapshot_id is not None:
        value["snapshot_id"] = str(snapshot_id)
    return value


def chat(chat_id: str = "chat-1") -> dict:
    return {
        "record_kind": "full",
        "chat_id": chat_id,
        "platform_user_id": f"fan-{chat_id}",
        "display_name": chat_id,
        "updated_at": "2026-07-19T10:00:00Z",
    }


def raw_message(message_id: str, chat_id: str = "chat-1", *, sent_at: str = "2026-07-19T10:00:00Z") -> dict:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_platform_user_id": f"fan-{chat_id}",
        "text": message_id,
        "sent_at": sent_at,
        "direction": "inbound",
    }


def begin(snapshot_id: UUID, *, stream_id: UUID = STREAM_ID, chunks: int = 2, chats: int = 1, messages: int = 1, coverage: int = 0):
    return message(
        "ingest.snapshot",
        {
            **identity(stream_id=stream_id, snapshot_id=snapshot_id),
            "frame_kind": "begin",
            "through_seq": 0,
            "chunk_count": chunks,
            "record_counts": {
                "chats": chats,
                "messages": messages,
                "coverage_evidence": coverage,
            },
            "max_frame_bytes": 524288,
        },
    ).payload


def chunk(snapshot_id: UUID, index: int, kind: str, records: list[dict], *, stream_id: UUID = STREAM_ID):
    return message(
        "ingest.snapshot",
        {
            **identity(stream_id=stream_id, snapshot_id=snapshot_id),
            "frame_kind": "chunk",
            "chunk_index": index,
            "entity_kind": kind,
            "records": records,
        },
    ).payload


def commit(snapshot_id: UUID, chunks: int, *, stream_id: UUID = STREAM_ID):
    return message(
        "ingest.snapshot",
        {
            **identity(stream_id=stream_id, snapshot_id=snapshot_id),
            "frame_kind": "commit",
            "chunk_count": chunks,
        },
    ).payload


def delta(
    sequence: int,
    change: dict,
    *,
    stream_id: UUID = STREAM_ID,
    origin: str = "signer",
):
    return message(
        "ingest.delta",
        {
            **identity(stream_id=stream_id),
            "event_id": str(uuid4()),
            "source_seq": sequence,
            "acquisition_origin": origin,
            "change": change,
        },
    ).payload


def commit_base_snapshot(repository, *, stream_id: UUID = STREAM_ID, chats: list[dict] | None = None, messages: list[dict] | None = None):
    chats = [chat()] if chats is None else chats
    messages = [raw_message("message-1")] if messages is None else messages
    snapshot_id = uuid4()
    key = StreamKey(ACCOUNT_ID, INSTALLATION_ID, stream_id)
    assert repository.begin_snapshot(
        key,
        begin(snapshot_id, stream_id=stream_id, chats=len(chats), messages=len(messages)),
    ).status == "accepted"
    assert repository.add_snapshot_chunk(
        key,
        chunk(snapshot_id, 0, "chat", [{"tombstone": False, "chat": item} for item in chats], stream_id=stream_id),
    ).status == "accepted"
    assert repository.add_snapshot_chunk(
        key,
        chunk(snapshot_id, 1, "message", [{"tombstone": False, "message": item} for item in messages], stream_id=stream_id),
    ).status == "accepted"
    result = repository.commit_snapshot(key, commit(snapshot_id, 2, stream_id=stream_id))
    assert result.status == "accepted"
    return key, snapshot_id


def authorize_history(history) -> None:
    authorized = history.update_history_settings(
        ACCOUNT_ID,
        expected_revision=0,
        values={
            "consent_policy_version": "history-consent-v1",
            "consent_revision": "consent-1",
            "authorized_platform_creator_id": "platform-creator-1",
            "desired_state": "running",
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        },
    )
    history.bind_history_config(
        ACCOUNT_ID,
        settings_revision=int(authorized["settings_revision"]),
        config_revision="config-history-1",
    )
    history.mark_history_config_applied(ACCOUNT_ID, "config-history-1")


def test_real_json_chunk_timestamps_are_accepted_and_normalized_to_utc() -> None:
    payload = chunk(
        uuid4(),
        0,
        "message",
        [{"tombstone": False, "message": raw_message("message-1", sent_at="2026-07-19T12:00:00+02:00")}],
    )
    assert payload.records[0]["message"]["sent_at"] == "2026-07-19T10:00:00Z"
    with pytest.raises(Exception):
        chunk(
            uuid4(),
            0,
            "message",
            [{"tombstone": False, "message": raw_message("message-2", sent_at="2026-07-19T10:00:00")}],
        )


def test_snapshot_progress_survives_restart_and_conflicting_commit_replay_rejects(tmp_path: Path) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projection_path = tmp_path / "projections.sqlite3"
    first = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path, projection_path=projection_path
    )
    snapshot_id = uuid4()
    key = StreamKey(ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
    assert first.history.begin_snapshot(key, begin(snapshot_id)).status == "accepted"
    assert first.history.add_snapshot_chunk(
        key,
        chunk(snapshot_id, 0, "chat", [{"tombstone": False, "chat": chat()}]),
    ).next_expected_chunk_index == 1

    restarted = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path, projection_path=projection_path
    )
    assert restarted.history.pending_snapshot(key) == (snapshot_id, 1)
    assert restarted.history.add_snapshot_chunk(
        key,
        chunk(snapshot_id, 1, "message", [{"tombstone": False, "message": raw_message("message-1")}]),
    ).status == "accepted"
    accepted = restarted.history.commit_snapshot(key, commit(snapshot_id, 2))
    assert accepted.snapshot_committed is True
    assert restarted.history.commit_snapshot(key, commit(snapshot_id, 2)).status == "duplicate"
    conflict = restarted.history.commit_snapshot(key, commit(snapshot_id, 9))
    assert conflict.status == "rejected"
    assert conflict.code == "chunk_conflict"


def test_snapshot_commit_rejects_checkpoint_change_after_begin() -> None:
    repositories = create_canonical_repositories("memory")
    key, _ = commit_base_snapshot(repositories.history)
    snapshot_id = uuid4()
    assert repositories.history.begin_snapshot(key, begin(snapshot_id)).status == "accepted"
    repositories.history.add_snapshot_chunk(
        key, chunk(snapshot_id, 0, "chat", [{"tombstone": False, "chat": chat()}])
    )
    repositories.history.add_snapshot_chunk(
        key,
        chunk(
            snapshot_id,
            1,
            "message",
            [{"tombstone": False, "message": raw_message("message-1")}],
        ),
    )
    assert repositories.history.commit_delta(
        key, delta(1, {"type": "chat.upsert", "chat": chat()})
    ).status == "accepted"
    rejected = repositories.history.commit_snapshot(key, commit(snapshot_id, 2))
    assert (rejected.status, rejected.code, rejected.committed_source_seq) == (
        "rejected",
        "invariant_failed",
        1,
    )


def test_snapshot_chunk_kinds_must_be_monotonic() -> None:
    repositories = create_canonical_repositories("memory")
    key = StreamKey(ACCOUNT_ID, INSTALLATION_ID, STREAM_ID)
    snapshot_id = uuid4()
    repositories.history.begin_snapshot(key, begin(snapshot_id))
    assert repositories.history.add_snapshot_chunk(
        key,
        chunk(
            snapshot_id,
            0,
            "message",
            [{"tombstone": False, "message": raw_message("message-1")}],
        ),
    ).status == "accepted"
    rejected = repositories.history.add_snapshot_chunk(
        key, chunk(snapshot_id, 1, "chat", [{"tombstone": False, "chat": chat()}])
    )
    assert (rejected.status, rejected.code) == ("rejected", "invariant_failed")


def test_message_tombstone_parent_conflict_rolls_back_checkpoint() -> None:
    repositories = create_canonical_repositories("memory")
    key, _ = commit_base_snapshot(
        repositories.history,
        chats=[chat("chat-1"), chat("chat-2")],
        messages=[raw_message("message-1", "chat-1")],
    )
    with pytest.raises(InvariantViolation):
        repositories.history.commit_delta(
            key,
            delta(
                1,
                {"type": "message.delete", "message_id": "message-1", "chat_id": "chat-2"},
            ),
        )
    assert repositories.history.checkpoint(key) == 0


def test_closed_coverage_generation_replays_idempotently_in_repair_snapshot() -> None:
    repositories = create_canonical_repositories("memory")
    history = repositories.history
    key, _ = commit_base_snapshot(history)
    authorized = history.update_history_settings(
        ACCOUNT_ID,
        expected_revision=0,
        values={
            "consent_policy_version": "history-consent-v1",
            "consent_revision": "consent-1",
            "authorized_platform_creator_id": "platform-creator-1",
            "desired_state": "running",
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        },
    )
    history.bind_history_config(
        ACCOUNT_ID,
        settings_revision=int(authorized["settings_revision"]),
        config_revision="config-coverage-1",
    )
    history.mark_history_config_applied(ACCOUNT_ID, "config-coverage-1")
    generation_id = str(uuid4())
    evidence = [
        {
            "type": "generation.started",
            "generation_id": generation_id,
            "as_of": "2026-07-19T10:00:00Z",
            "authorization_revision": "consent-1",
        },
        {"type": "inventory.member", "generation_id": generation_id, "conversation_id": "chat-1"},
        {"type": "inventory.ended", "generation_id": generation_id, "observed_at": "2026-07-19T10:01:00Z"},
        {
            "type": "conversation.history_started",
            "generation_id": generation_id,
            "conversation_id": "chat-1",
            "earliest_observed_at": "2026-01-01T00:00:00Z",
            "observed_at": "2026-07-19T10:02:00Z",
        },
        {
            "type": "conversation.head_reconciled",
            "generation_id": generation_id,
            "conversation_id": "chat-1",
            "reconciled_through": "2026-07-19T10:00:00Z",
        },
        {"type": "generation.closed", "generation_id": generation_id, "closed_at": "2026-07-19T10:03:00Z"},
    ]
    for sequence, item in enumerate(evidence, 1):
        result = history.commit_delta(
            key, delta(sequence, {"type": "coverage.observed", "evidence": item})
        )
        assert result.status == "accepted"
    revision_before = history.account_revision(ACCOUNT_ID)[0]
    assert history.coverage(ACCOUNT_ID)["status"] == "complete"

    repair_stream = uuid4()
    repair_key = StreamKey(ACCOUNT_ID, INSTALLATION_ID, repair_stream)
    snapshot_id = uuid4()
    assert history.begin_snapshot(
        repair_key,
        begin(snapshot_id, stream_id=repair_stream, chunks=2, chats=1, messages=0, coverage=len(evidence)),
    ).status == "accepted"
    history.add_snapshot_chunk(
        repair_key,
        chunk(snapshot_id, 0, "chat", [{"tombstone": False, "chat": chat()}], stream_id=repair_stream),
    )
    history.add_snapshot_chunk(
        repair_key,
        chunk(snapshot_id, 1, "coverage_evidence", evidence, stream_id=repair_stream),
    )
    replay = history.commit_snapshot(repair_key, commit(snapshot_id, 2, stream_id=repair_stream))
    assert replay.status == "accepted"
    assert replay.canonical_revision is None
    assert history.account_revision(ACCOUNT_ID)[0] == revision_before
    coverage = history.coverage(ACCOUNT_ID)
    assert set(coverage) == {
        "status", "phase", "generation_id", "as_of", "discovered_conversations",
        "complete_conversations", "complete_as_of", "reason",
    }


def test_generation_start_requires_current_applied_consent_and_config() -> None:
    repositories = create_canonical_repositories("memory")
    key, _ = commit_base_snapshot(repositories.history)
    generation_id = str(uuid4())
    with pytest.raises(InvariantViolation, match="current applied consent/config"):
        repositories.history.commit_delta(
            key,
            delta(
                1,
                {
                    "type": "coverage.observed",
                    "evidence": {
                        "type": "generation.started",
                        "generation_id": generation_id,
                        "as_of": "2026-07-19T10:00:00Z",
                        "authorization_revision": "consent-1",
                    },
                },
            ),
        )
    assert repositories.history.checkpoint(key) == 0
    assert repositories.history.coverage(ACCOUNT_ID)["status"] == "unknown"


def test_new_nonmember_chat_invalidates_complete_coverage_but_retains_complete_as_of() -> None:
    repositories = create_canonical_repositories("memory")
    history = repositories.history
    key, _ = commit_base_snapshot(history)
    authorize_history(history)
    generation_id = str(uuid4())
    evidence = [
        {
            "type": "generation.started",
            "generation_id": generation_id,
            "as_of": "2026-07-19T10:00:00Z",
            "authorization_revision": "consent-1",
        },
        {
            "type": "inventory.member",
            "generation_id": generation_id,
            "conversation_id": "chat-1",
        },
        {
            "type": "inventory.ended",
            "generation_id": generation_id,
            "observed_at": "2026-07-19T10:01:00Z",
        },
        {
            "type": "conversation.history_started",
            "generation_id": generation_id,
            "conversation_id": "chat-1",
            "earliest_observed_at": "2026-01-01T00:00:00Z",
            "observed_at": "2026-07-19T10:02:00Z",
        },
        {
            "type": "conversation.head_reconciled",
            "generation_id": generation_id,
            "conversation_id": "chat-1",
            "reconciled_through": "2026-07-19T10:00:00Z",
        },
        {
            "type": "generation.closed",
            "generation_id": generation_id,
            "closed_at": "2026-07-19T10:03:00Z",
        },
    ]
    for sequence, item in enumerate(evidence, 1):
        history.commit_delta(
            key, delta(sequence, {"type": "coverage.observed", "evidence": item})
        )
    complete = history.coverage(ACCOUNT_ID)
    assert complete["status"] == "complete"
    assert complete["complete_as_of"] == "2026-07-19T10:00:00Z"

    result = history.commit_delta(
        key,
        delta(
            len(evidence) + 1,
            {"type": "chat.upsert", "chat": chat("chat-new")},
            origin="passive",
        ),
    )
    assert result.status == "accepted"
    invalidated = history.coverage(ACCOUNT_ID)
    assert invalidated["status"] == "partial"
    assert invalidated["phase"] == "repairing"
    assert invalidated["reason"] == "new_conversation_discovered"
    assert invalidated["complete_as_of"] == complete["complete_as_of"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("chat_id", "chat-2"),
        ("sender_platform_user_id", "fan-other"),
        ("text", "changed"),
        ("sent_at", "2026-07-19T10:00:01Z"),
        ("direction", "outbound"),
    ],
)
def test_message_material_is_immutable_for_every_field(field: str, replacement: str) -> None:
    repositories = create_canonical_repositories("memory")
    key, _ = commit_base_snapshot(
        repositories.history,
        chats=[chat("chat-1"), chat("chat-2")],
        messages=[raw_message("message-1", "chat-1")],
    )
    conflicting = raw_message("message-1", "chat-1")
    conflicting[field] = replacement
    with pytest.raises(InvariantViolation, match="immutable message identifier"):
        repositories.history.commit_delta(
            key,
            delta(
                1,
                {"type": "message.upsert", "message": conflicting},
            ),
        )
    assert repositories.history.checkpoint(key) == 0


def test_delta_entity_conflict_is_durable_and_idempotent() -> None:
    repositories = create_canonical_repositories("memory")
    key, _ = commit_base_snapshot(repositories.history)
    conflicting = raw_message("message-1")
    conflicting["text"] = "conflicting"
    for _ in range(2):
        with pytest.raises(InvariantViolation, match="immutable message identifier"):
            repositories.history.commit_delta(
                key,
                delta(1, {"type": "message.upsert", "message": conflicting}),
            )
    with repositories.database.read() as connection:
        rows = connection.execute(
            """SELECT entity_kind,entity_id,existing_hash,incoming_hash,source_seq,reason
               FROM entity_conflicts WHERE creator_account_id=?""",
            (ACCOUNT_ID,),
        ).fetchall()
    assert len(rows) == 1
    assert (rows[0][0], rows[0][1], rows[0][4]) == ("message", "message-1", 1)
    assert rows[0][2] != rows[0][3]
    assert "immutable message identifier" in rows[0][5]
    assert repositories.history.checkpoint(key) == 0


def test_snapshot_entity_conflict_is_durable_and_idempotent() -> None:
    repositories = create_canonical_repositories("memory")
    history = repositories.history
    commit_base_snapshot(history)
    repair_stream = uuid4()
    repair_key = StreamKey(ACCOUNT_ID, INSTALLATION_ID, repair_stream)
    snapshot_id = uuid4()
    conflicting = raw_message("message-1")
    conflicting["text"] = "conflicting"
    history.begin_snapshot(
        repair_key,
        begin(snapshot_id, stream_id=repair_stream, chats=1, messages=1),
    )
    history.add_snapshot_chunk(
        repair_key,
        chunk(
            snapshot_id,
            0,
            "chat",
            [{"tombstone": False, "chat": chat()}],
            stream_id=repair_stream,
        ),
    )
    history.add_snapshot_chunk(
        repair_key,
        chunk(
            snapshot_id,
            1,
            "message",
            [{"tombstone": False, "message": conflicting}],
            stream_id=repair_stream,
        ),
    )
    for _ in range(2):
        with pytest.raises(InvariantViolation, match="immutable message identifier"):
            history.commit_snapshot(
                repair_key, commit(snapshot_id, 2, stream_id=repair_stream)
            )
    with repositories.database.read() as connection:
        rows = connection.execute(
            """SELECT entity_kind,entity_id,source_seq FROM entity_conflicts
               WHERE creator_account_id=?""",
            (ACCOUNT_ID,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [("message", "message-1", 0)]
    assert history.checkpoint(repair_key) is None
