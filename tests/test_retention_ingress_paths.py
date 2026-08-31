from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.history import StreamKey
from app.persistence.retention import CreatorVaultRetention
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.transport.manager import (
    DEV_ACCOUNT_ID,
    REQUIRED_CONFIG_REVISION,
    InMemoryTransportManager,
)


ACCOUNT = DEV_ACCOUNT_ID
INSTALLATION = UUID("20000000-0000-4000-8000-000000000071")
REINSTALLATION = UUID("20000000-0000-4000-8000-000000000072")
STREAM = UUID("30000000-0000-4000-8000-000000000071")
REPAIR_STREAM = UUID("30000000-0000-4000-8000-000000000072")
REINSTALL_STREAM = UUID("30000000-0000-4000-8000-000000000073")


def envelope(message_type: str, payload: dict):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(
            {
                "type": message_type,
                "protocol_version": "2",
                "message_id": str(uuid4()),
                "payload": payload,
            }
        )
    ).payload


def chat() -> dict:
    return {
        "record_kind": "full",
        "chat_id": "chat-1",
        "platform_user_id": "participant-1",
        "display_name": "Participant",
        "updated_at": "2026-08-30T10:00:00Z",
    }


def message() -> dict:
    return {
        "message_id": "message-1",
        "chat_id": "chat-1",
        "sender_platform_user_id": "participant-1",
        "text": "stale private text",
        "sent_at": "2026-08-30T10:01:00Z",
        "direction": "inbound",
    }


def identity(key: StreamKey, *, snapshot_id: UUID | None = None) -> dict:
    value = {
        "connection_id": "10000000-0000-4000-8000-000000000071",
        "fencing_token": "retention-fence",
        "creator_account_id": key.creator_account_id,
        "agent_installation_id": str(key.agent_installation_id),
        "agent_stream_id": str(key.agent_stream_id),
    }
    if snapshot_id is not None:
        value["snapshot_id"] = str(snapshot_id)
    return value


def snapshot_begin(key: StreamKey, snapshot_id: UUID, *, through_seq: int = 0):
    return envelope(
        "ingest.snapshot",
        {
            **identity(key, snapshot_id=snapshot_id),
            "frame_kind": "begin",
            "through_seq": through_seq,
            "chunk_count": 2,
            "record_counts": {
                "chats": 1,
                "messages": 1,
                "coverage_evidence": 0,
            },
            "max_frame_bytes": 524288,
        },
    )


def snapshot_chunk(key: StreamKey, snapshot_id: UUID, index: int, kind: str):
    records = (
        [{"tombstone": False, "chat": chat()}]
        if kind == "chat"
        else [{"tombstone": False, "message": message()}]
    )
    return envelope(
        "ingest.snapshot",
        {
            **identity(key, snapshot_id=snapshot_id),
            "frame_kind": "chunk",
            "chunk_index": index,
            "entity_kind": kind,
            "records": records,
        },
    )


def snapshot_commit(key: StreamKey, snapshot_id: UUID):
    return envelope(
        "ingest.snapshot",
        {
            **identity(key, snapshot_id=snapshot_id),
            "frame_kind": "commit",
            "chunk_count": 2,
        },
    )


def commit_snapshot(
    repositories: CanonicalRepositories,
    key: StreamKey,
    *,
    inspect_barriered_staging: bool = False,
) -> None:
    snapshot_id = uuid4()
    history = repositories.history
    assert history.begin_snapshot(key, snapshot_begin(key, snapshot_id)).status == "accepted"
    assert history.add_snapshot_chunk(
        key, snapshot_chunk(key, snapshot_id, 0, "chat")
    ).status == "accepted"
    assert history.add_snapshot_chunk(
        key, snapshot_chunk(key, snapshot_id, 1, "message")
    ).status == "accepted"
    if inspect_barriered_staging:
        with repositories.database.read() as connection:
            staged = connection.execute(
                """SELECT COUNT(*) FROM snapshot_message_records
                   WHERE creator_account_id=? AND agent_installation_id=?
                     AND agent_stream_id=? AND snapshot_id=?""",
                (*key.sql(), str(snapshot_id)),
            ).fetchone()
        assert int(staged[0]) == 0
    assert history.commit_snapshot(key, snapshot_commit(key, snapshot_id)).status == "accepted"


def delta(
    key: StreamKey,
    *,
    sequence: int,
    origin: str,
    event_id: UUID | None = None,
    connection_id: str = "10000000-0000-4000-8000-000000000071",
    fencing_token: str = "retention-fence",
):
    return envelope(
        "ingest.delta",
        {
            **identity(key),
            "connection_id": connection_id,
            "fencing_token": fencing_token,
            "event_id": str(event_id or uuid4()),
            "source_seq": sequence,
            "acquisition_origin": origin,
            "change": {"type": "message.upsert", "message": message()},
        },
    )


def delete_observed_message(repositories: CanonicalRepositories) -> None:
    CreatorVaultRetention(repositories.database).delete_message(ACCOUNT, "message-1")
    with repositories.database.read() as connection:
        assert connection.execute(
            """SELECT 1 FROM deletion_barriers
               WHERE creator_account_id=? AND scope_kind='message' AND scope_key='message-1'""",
            (ACCOUNT,),
        ).fetchone() is not None


def assert_source_absent(
    repositories: CanonicalRepositories,
    *,
    expect_redacted_event: bool,
) -> None:
    with repositories.database.read() as connection:
        assert connection.execute(
            """SELECT 1 FROM account_messages
               WHERE creator_account_id=? AND message_id='message-1'""",
            (ACCOUNT,),
        ).fetchone() is None
        assert connection.execute(
            """SELECT 1 FROM archive_membership
               WHERE creator_account_id=? AND message_id='message-1'""",
            (ACCOUNT,),
        ).fetchone() is None
        assert connection.execute(
            """SELECT 1 FROM stream_message_membership
               WHERE creator_account_id=? AND message_id='message-1'""",
            (ACCOUNT,),
        ).fetchone() is None
        rows = connection.execute(
            """SELECT event_json FROM raw_ingest_events
               WHERE creator_account_id=? ORDER BY source_seq""",
            (ACCOUNT,),
        ).fetchall()
    if expect_redacted_event:
        assert rows
        for row in rows:
            assert row[0] == '{"redacted_by_deletion_barrier":true}'
    else:
        assert rows == []


def test_passive_delta_after_delete_is_redacted_and_non_resurrecting() -> None:
    repositories = create_canonical_repositories("memory")
    key = StreamKey(ACCOUNT, INSTALLATION, STREAM)
    commit_snapshot(repositories, key)
    delete_observed_message(repositories)

    replay = repositories.history.commit_delta(
        key, delta(key, sequence=1, origin="passive")
    )

    assert replay.status == "accepted"
    assert repositories.history.checkpoint(key) == 1
    assert_source_absent(repositories, expect_redacted_event=True)


def test_history_signer_delta_after_delete_is_redacted_and_non_resurrecting() -> None:
    repositories = create_canonical_repositories("memory")
    key = StreamKey(ACCOUNT, INSTALLATION, STREAM)
    commit_snapshot(repositories, key)
    delete_observed_message(repositories)

    replay = repositories.history.commit_delta(
        key, delta(key, sequence=1, origin="signer")
    )

    assert replay.status == "accepted"
    assert repositories.history.checkpoint(key) == 1
    assert_source_absent(repositories, expect_redacted_event=True)


def test_snapshot_repair_drops_barriered_message_before_staging_or_commit() -> None:
    repositories = create_canonical_repositories("memory")
    key = StreamKey(ACCOUNT, INSTALLATION, STREAM)
    commit_snapshot(repositories, key)
    delete_observed_message(repositories)

    repair_key = StreamKey(ACCOUNT, INSTALLATION, REPAIR_STREAM)
    commit_snapshot(repositories, repair_key, inspect_barriered_staging=True)

    assert repositories.history.checkpoint(repair_key) == 0
    assert_source_absent(repositories, expect_redacted_event=False)


class NullWebSocket:
    async def send_text(self, _text: str) -> None:
        return None

    async def close(self, code: int, reason: str) -> None:
        _ = (code, reason)


def bound_identity(lease) -> tuple[str, str]:
    return str(lease.connection_id), lease.fencing_token


async def bind(
    manager: InMemoryTransportManager,
    *,
    installation_id: UUID,
    stream_id: UUID,
):
    return await manager.bind_agent(
        NullWebSocket(),
        principal_id="retention-test",
        creator_account_id=ACCOUNT,
        agent_installation_id=installation_id,
        agent_stream_id=stream_id,
        applied_config_revision=REQUIRED_CONFIG_REVISION,
    )


def test_reconnect_replay_uses_new_fence_but_cannot_resurrect_deleted_message() -> None:
    repositories = create_canonical_repositories("memory")
    manager = InMemoryTransportManager(repositories)

    async def exercise() -> None:
        first = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        key = manager.stream_key(first)
        commit_snapshot(repositories, key)
        delete_observed_message(repositories)

        second = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        assert manager.is_current_fence(first) is False
        assert manager.is_current_fence(second) is True
        connection_id, fencing_token = bound_identity(second)
        replay = manager.ingest_delta(
            second,
            delta(
                manager.stream_key(second),
                sequence=1,
                origin="passive",
                connection_id=connection_id,
                fencing_token=fencing_token,
            ),
        )
        assert replay.status == "accepted"

    asyncio.run(exercise())
    assert_source_absent(repositories, expect_redacted_event=True)


def test_projection_reseed_after_delete_cannot_rebuild_deleted_source() -> None:
    repositories = create_canonical_repositories("memory")
    key = StreamKey(ACCOUNT, INSTALLATION, STREAM)
    commit_snapshot(repositories, key)
    assert repositories.projection.catch_up(ACCOUNT) is not None
    before, _, _ = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert [item["message_id"] for item in before] == ["message-1"]
    revision_before = repositories.history.account_revision(ACCOUNT)[0]

    delete_observed_message(repositories)

    revision_after = repositories.history.account_revision(ACCOUNT)[0]
    assert revision_after == revision_before + 1
    assert repositories.projection.pending_accounts() == [ACCOUNT]
    assert repositories.projection.catch_up(ACCOUNT) is not None
    after, _, _ = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert after == []


def test_outbox_retry_after_reconnect_remains_non_resurrecting_and_idempotent() -> None:
    repositories = create_canonical_repositories("memory")
    manager = InMemoryTransportManager(repositories)
    event_id = UUID("50000000-0000-4000-8000-000000000071")

    async def exercise() -> None:
        first = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        commit_snapshot(repositories, manager.stream_key(first))
        delete_observed_message(repositories)

        retry_session = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        connection_id, fencing_token = bound_identity(retry_session)
        first_retry = manager.ingest_delta(
            retry_session,
            delta(
                manager.stream_key(retry_session),
                sequence=1,
                origin="passive",
                event_id=event_id,
                connection_id=connection_id,
                fencing_token=fencing_token,
            ),
        )
        assert first_retry.status == "accepted"

        reconnected = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        connection_id, fencing_token = bound_identity(reconnected)
        duplicate_retry = manager.ingest_delta(
            reconnected,
            delta(
                manager.stream_key(reconnected),
                sequence=1,
                origin="passive",
                event_id=event_id,
                connection_id=connection_id,
                fencing_token=fencing_token,
            ),
        )
        assert duplicate_retry.status == "duplicate"

    asyncio.run(exercise())
    assert_source_absent(repositories, expect_redacted_event=True)


def test_fresh_installation_snapshot_cannot_resurrect_deleted_message() -> None:
    repositories = create_canonical_repositories("memory")
    manager = InMemoryTransportManager(repositories)

    async def exercise() -> None:
        first = await bind(manager, installation_id=INSTALLATION, stream_id=STREAM)
        commit_snapshot(repositories, manager.stream_key(first))
        delete_observed_message(repositories)

        reinstalled = await bind(
            manager,
            installation_id=REINSTALLATION,
            stream_id=REINSTALL_STREAM,
        )
        assert reinstalled.agent_installation_id != first.agent_installation_id
        commit_snapshot(
            repositories,
            manager.stream_key(reinstalled),
            inspect_barriered_staging=True,
        )

    asyncio.run(exercise())
    assert_source_absent(repositories, expect_redacted_event=False)
