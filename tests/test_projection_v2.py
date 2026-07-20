from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import tracemalloc
from math import ceil
from uuid import UUID, uuid4

import pytest

from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.history import (
    CONVERSATION_BATCH_SIZE,
    PROJECTION_BATCH_SIZE,
    StreamKey,
)
from app.persistence.projection_pipeline import DeterministicProjectionPipeline
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.transport.manager import InMemoryTransportManager


ACCOUNT = "projection-account"
INSTALLATION = UUID("20000000-0000-4000-8000-000000000002")
STREAM = UUID("30000000-0000-4000-8000-000000000002")
CONNECTION = "10000000-0000-4000-8000-000000000002"


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


def identity(
    stream_id: UUID,
    snapshot_id: UUID | None = None,
    *,
    account_id: str = ACCOUNT,
) -> dict:
    result = {
        "connection_id": CONNECTION,
        "fencing_token": "projection-fence",
        "creator_account_id": account_id,
        "agent_installation_id": str(INSTALLATION),
        "agent_stream_id": str(stream_id),
    }
    if snapshot_id is not None:
        result["snapshot_id"] = str(snapshot_id)
    return result


def chat(chat_id: str) -> dict:
    return {
        "record_kind": "full",
        "chat_id": chat_id,
        "platform_user_id": f"fan-{chat_id}",
        "display_name": chat_id,
        "updated_at": "2026-07-19T10:00:00Z",
    }


def raw_message(message_id: str, chat_id: str = "chat-1") -> dict:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_platform_user_id": f"fan-{chat_id}",
        "text": f"Text {message_id}",
        "sent_at": "2026-07-19T10:00:00Z",
        "direction": "inbound",
    }


def stage_snapshot(
    repositories: CanonicalRepositories,
    *,
    chats: list[dict],
    messages: list[dict],
    stream_id: UUID = STREAM,
    account_id: str = ACCOUNT,
) -> tuple[StreamKey, object]:
    history = repositories.history
    key = StreamKey(account_id, INSTALLATION, stream_id)
    snapshot_id = uuid4()
    chat_chunks = ceil(len(chats) / 100)
    message_chunks = ceil(len(messages) / 100)
    chunk_count = chat_chunks + message_chunks
    begin = envelope(
        "ingest.snapshot",
        {
            **identity(stream_id, snapshot_id, account_id=account_id),
            "frame_kind": "begin",
            "through_seq": 0,
            "chunk_count": chunk_count,
            "record_counts": {
                "chats": len(chats),
                "messages": len(messages),
                "coverage_evidence": 0,
            },
            "max_frame_bytes": 524288,
        },
    )
    assert history.begin_snapshot(key, begin).status == "accepted"
    index = 0
    for offset in range(0, len(chats), 100):
        payload = envelope(
            "ingest.snapshot",
            {
                **identity(stream_id, snapshot_id, account_id=account_id),
                "frame_kind": "chunk",
                "chunk_index": index,
                "entity_kind": "chat",
                "records": [
                    {"tombstone": False, "chat": item}
                    for item in chats[offset : offset + 100]
                ],
            },
        )
        assert history.add_snapshot_chunk(key, payload).status == "accepted"
        index += 1
    for offset in range(0, len(messages), 100):
        payload = envelope(
            "ingest.snapshot",
            {
                **identity(stream_id, snapshot_id, account_id=account_id),
                "frame_kind": "chunk",
                "chunk_index": index,
                "entity_kind": "message",
                "records": [
                    {"tombstone": False, "message": item}
                    for item in messages[offset : offset + 100]
                ],
            },
        )
        assert history.add_snapshot_chunk(key, payload).status == "accepted"
        index += 1
    commit = envelope(
        "ingest.snapshot",
        {
            **identity(stream_id, snapshot_id, account_id=account_id),
            "frame_kind": "commit",
            "chunk_count": chunk_count,
        },
    )
    return key, commit


def commit_seed(
    repositories: CanonicalRepositories,
    *,
    chats: list[dict] | None = None,
    messages: list[dict] | None = None,
    stream_id: UUID = STREAM,
    account_id: str = ACCOUNT,
) -> StreamKey:
    key, commit = stage_snapshot(
        repositories,
        chats=[chat("chat-1")] if chats is None else chats,
        messages=[] if messages is None else messages,
        stream_id=stream_id,
        account_id=account_id,
    )
    result = repositories.history.commit_snapshot(key, commit)
    assert result.status == "accepted"
    return key


def commit_message_delta(
    repositories: CanonicalRepositories,
    key: StreamKey,
    *,
    sequence: int,
    origin: str,
    message: dict,
) -> None:
    payload = envelope(
        "ingest.delta",
        {
            **identity(key.agent_stream_id, account_id=key.creator_account_id),
            "event_id": str(uuid4()),
            "source_seq": sequence,
            "acquisition_origin": origin,
            "change": {"type": "message.upsert", "message": message},
        },
    )
    assert repositories.history.commit_delta(key, payload).status == "accepted"


def material(repositories: CanonicalRepositories) -> tuple:
    with repositories.projection_database.read() as connection:
        analyses = [
            tuple(row)
            for row in connection.execute(
                """SELECT conversation_id,message_id,source_hash,analysis_status,
                          sentiment,analyzer_id,document_json
                   FROM projection_message_analysis ORDER BY message_id"""
            )
        ]
        nodes = [
            tuple(row)
            for row in connection.execute(
                """SELECT conversation_id,node_id,node_kind,entity_id,document_json
                   FROM projection_lpg_nodes ORDER BY node_id"""
            )
        ]
        edges = [
            tuple(row)
            for row in connection.execute(
                """SELECT conversation_id,edge_id,source_node_id,target_node_id,
                          relationship,document_json
                   FROM projection_lpg_edges ORDER BY edge_id"""
            )
        ]
        analytics = json.loads(
            connection.execute("SELECT document_json FROM projection_analytics").fetchone()[0]
        )
    for metric in analytics.values():
        metric.pop("as_of")
    return analyses, nodes, edges, analytics


def test_passive_and_signer_messages_have_identical_durable_nlp_lpg_and_analytics() -> None:
    projected: list[CanonicalRepositories] = []
    for origin in ("passive", "signer"):
        repositories = create_canonical_repositories("memory")
        key = commit_seed(repositories)
        commit_message_delta(
            repositories,
            key,
            sequence=1,
            origin=origin,
            message=raw_message("message-1"),
        )
        assert repositories.projection.catch_up(ACCOUNT) is not None
        projected.append(repositories)

    assert material(projected[0]) == material(projected[1])
    analyses = material(projected[0])[0]
    assert analyses[0][3:6] == ("unavailable", "unknown", None)
    assert json.loads(analyses[0][6]) == {
        "analyzer_id": None,
        "pipeline_version": "deterministic-local-v1",
        "reason": "model_not_configured",
        "sentiment": "unknown",
        "status": "unavailable",
        "topics": [],
    }
    snapshot = projected[0].projection.snapshot(ACCOUNT)
    assert snapshot["conversations"][0]["latest_message"]["sentiment"] == "unknown"


class TrackingPipeline:
    def __init__(self) -> None:
        self.delegate = DeterministicProjectionPipeline()
        self.pipeline_version = self.delegate.pipeline_version
        self.max_conversations = 0
        self.max_messages = 0
        self.conversation_ids: set[str] = set()

    def project(self, conversations, messages):
        self.max_conversations = max(self.max_conversations, len(conversations))
        self.max_messages = max(self.max_messages, len(messages))
        self.conversation_ids.update(item.conversation_id for item in conversations)
        return self.delegate.project(conversations, messages)


def test_incremental_projection_touches_only_changed_conversation_and_recovery_is_idempotent() -> None:
    repositories = create_canonical_repositories("memory")
    key = commit_seed(
        repositories,
        chats=[chat("chat-1"), chat("chat-2")],
        messages=[raw_message("message-1", "chat-1"), raw_message("message-2", "chat-2")],
    )
    first = repositories.projection.catch_up(ACCOUNT)
    assert first is not None
    first_generation = repositories.projection.active_generation(ACCOUNT)
    assert first_generation is not None
    tracker = TrackingPipeline()
    repositories.projection.pipeline = tracker
    commit_message_delta(
        repositories,
        key,
        sequence=1,
        origin="passive",
        message=raw_message("message-3", "chat-1"),
    )
    second = repositories.projection.catch_up(ACCOUNT)
    assert second is not None
    second_generation = repositories.projection.active_generation(ACCOUNT)
    assert second_generation is not None
    assert second_generation["generation_id"] != first_generation["generation_id"]
    assert tracker.conversation_ids == {"chat-1"}
    with repositories.projection_database.read() as connection:
        account = connection.execute(
            """SELECT generation_id,projected_revision,read_revision,generated_at
               FROM projection_accounts
              WHERE creator_account_id=? AND generation_id=?""",
            (ACCOUNT, second_generation["generation_id"]),
        ).fetchone()
        log = connection.execute(
            """SELECT change_kind,touched_conversations_json
               FROM projection_change_log WHERE creator_account_id=?
               ORDER BY read_revision DESC LIMIT 1""",
            (ACCOUNT,),
        ).fetchone()
        log_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM projection_change_log WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()[0]
        )
    assert log[0] == "incremental"
    assert json.loads(log[1]) == {"conversation_ids": ["chat-1"], "count": 1}

    # Simulate a crash after the projection file committed but before canonical
    # activation/broadcast. Restart recovery must activate the same generation.
    with repositories.database.transaction() as connection:
        connection.execute(
            "UPDATE account_heads SET view_revision=view_revision-1 WHERE creator_account_id=?",
            (ACCOUNT,),
        )
        connection.execute(
            """UPDATE projection_work SET completed_at=NULL
               WHERE creator_account_id=? AND canonical_revision=?""",
            (ACCOUNT, int(account[1])),
        )
        connection.execute(
            """UPDATE projection_activation_intents SET state='pending',
                      generation_id=NULL,activated_view_revision=NULL,
                      projection_committed_at=NULL,activated_at=NULL
               WHERE creator_account_id=? AND target_canonical_revision=?""",
            (ACCOUNT, int(account[1])),
        )

    # The newer slot is durable but not canonically active. Every external read
    # continues to serve the complete prior generation during the crash gap.
    assert repositories.projection.active_generation(ACCOUNT) == first_generation
    assert repositories.projection.conversation_exists(ACCOUNT, "chat-1")
    prior_items, _, prior_page = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert [item["message_id"] for item in prior_items] == ["message-1"]
    assert prior_page["generation_id"] == first_generation["generation_id"]
    prior = repositories.projection.snapshot(ACCOUNT)
    assert prior["view_revision"] == first_generation["read_revision"]
    assert prior["projection"]["status"] == "pending"
    assert prior["analytics"]["total_messages"]["value"] == 2

    recovered = repositories.projection.catch_up(ACCOUNT)
    assert recovered is not None
    with repositories.projection_database.read() as connection:
        recovered_account = connection.execute(
            """SELECT generation_id,projected_revision,read_revision
               FROM projection_accounts
              WHERE creator_account_id=? AND generation_id=?""",
            (ACCOUNT, second_generation["generation_id"]),
        ).fetchone()
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM projection_change_log WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()[0]
        ) == log_count
    assert tuple(recovered_account) == tuple(account[:3])
    assert recovered["view_revision"] == int(account[2])
    items, _, visible_generation = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert [item["message_id"] for item in items] == ["message-1", "message-3"]
    assert visible_generation["generation_id"] == str(account[0])


def test_projection_slots_alternate_and_replay_lagging_work() -> None:
    repositories = create_canonical_repositories("memory")

    def slot_for(generation_id: str) -> int:
        with repositories.projection_database.read() as connection:
            row = connection.execute(
                """SELECT projection_slot FROM projection_accounts
                    WHERE creator_account_id=? AND generation_id=?""",
                (ACCOUNT, generation_id),
            ).fetchone()
        assert row is not None
        return int(row[0])

    key = commit_seed(
        repositories,
        chats=[chat("chat-1"), chat("chat-2")],
        messages=[raw_message("message-1", "chat-1"), raw_message("message-2", "chat-2")],
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None
    first = repositories.projection.active_generation(ACCOUNT)
    assert first is not None
    first_slot = slot_for(first["generation_id"])

    commit_message_delta(
        repositories,
        key,
        sequence=1,
        origin="passive",
        message=raw_message("message-3", "chat-1"),
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None
    second = repositories.projection.active_generation(ACCOUNT)
    assert second is not None
    second_slot = slot_for(second["generation_id"])

    commit_message_delta(
        repositories,
        key,
        sequence=2,
        origin="signer",
        message=raw_message("message-4", "chat-2"),
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None
    third = repositories.projection.active_generation(ACCOUNT)
    assert third is not None
    third_slot = slot_for(third["generation_id"])

    with repositories.projection_database.read() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM projection_accounts WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()[0]
        ) == 2
        counts = {
            int(row[0]): int(row[1])
            for row in connection.execute(
                """SELECT projection_slot,COUNT(*) FROM projection_messages
                    WHERE creator_account_id=? GROUP BY projection_slot""",
                (ACCOUNT,),
            )
        }
    assert first_slot == third_slot
    assert second_slot != third_slot
    assert counts[third_slot] == 4
    assert counts[second_slot] == 3

    chat_one, _, _ = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    chat_two, _, _ = repositories.projection.message_rows(
        ACCOUNT, "chat-2", before=None, limit=10
    )
    assert {item["message_id"] for item in chat_one} == {"message-1", "message-3"}
    assert {item["message_id"] for item in chat_two} == {"message-2", "message-4"}


def test_projection_slots_are_account_partitioned() -> None:
    other_account = "projection-account-2"
    repositories = create_canonical_repositories("memory")
    first_key = commit_seed(
        repositories,
        messages=[raw_message("shared-message")],
    )
    commit_seed(
        repositories,
        messages=[raw_message("shared-message")],
        stream_id=UUID("30000000-0000-4000-8000-000000000099"),
        account_id=other_account,
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None
    assert repositories.projection.catch_up(other_account) is not None

    commit_message_delta(
        repositories,
        first_key,
        sequence=1,
        origin="passive",
        message=raw_message("account-one-only"),
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None

    first_items, _, _ = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    second_items, _, _ = repositories.projection.message_rows(
        other_account, "chat-1", before=None, limit=10
    )
    assert {item["message_id"] for item in first_items} == {
        "shared-message",
        "account-one-only",
    }
    assert [item["message_id"] for item in second_items] == ["shared-message"]
    with repositories.projection_database.read() as connection:
        slot_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """SELECT creator_account_id,COUNT(*) FROM projection_accounts
                    GROUP BY creator_account_id"""
            )
        }
    assert slot_counts == {ACCOUNT: 2, other_account: 1}


def test_reseed_keeps_prior_slot_readable_until_recovery_activation() -> None:
    repositories = create_canonical_repositories("memory")
    commit_seed(repositories, messages=[raw_message("message-1")])
    assert repositories.projection.catch_up(ACCOUNT) is not None
    prior_generation = repositories.projection.active_generation(ACCOUNT)
    assert prior_generation is not None

    commit_seed(
        repositories,
        messages=[raw_message("message-1"), raw_message("message-2")],
        stream_id=UUID("30000000-0000-4000-8000-000000000088"),
    )
    assert repositories.projection.catch_up(ACCOUNT) is not None
    reseed_generation = repositories.projection.active_generation(ACCOUNT)
    assert reseed_generation is not None
    assert reseed_generation["generation_id"] != prior_generation["generation_id"]

    with repositories.database.transaction() as connection:
        connection.execute(
            "UPDATE account_heads SET view_revision=? WHERE creator_account_id=?",
            (prior_generation["read_revision"], ACCOUNT),
        )
        connection.execute(
            """UPDATE projection_work SET completed_at=NULL
                WHERE creator_account_id=? AND canonical_revision=?""",
            (ACCOUNT, reseed_generation["projected_revision"]),
        )
        connection.execute(
            """UPDATE projection_activation_intents SET state='pending',
                      generation_id=NULL,activated_view_revision=NULL,
                      projection_committed_at=NULL,activated_at=NULL
                WHERE creator_account_id=? AND target_canonical_revision=?""",
            (ACCOUNT, reseed_generation["projected_revision"]),
        )

    assert repositories.projection.active_generation(ACCOUNT) == prior_generation
    prior_items, _, prior_page = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert [item["message_id"] for item in prior_items] == ["message-1"]
    assert prior_page["generation_id"] == prior_generation["generation_id"]

    assert repositories.projection.catch_up(ACCOUNT) is not None
    assert repositories.projection.active_generation(ACCOUNT) == reseed_generation
    current_items, _, current_page = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert {item["message_id"] for item in current_items} == {"message-1", "message-2"}
    assert current_page["generation_id"] == reseed_generation["generation_id"]
    with repositories.projection_database.read() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM projection_accounts WHERE creator_account_id=?",
                (ACCOUNT,),
            ).fetchone()[0]
        ) == 2


def test_manager_start_resumes_durable_projection_work_without_new_ingest() -> None:
    repositories = create_canonical_repositories("memory")
    commit_seed(repositories, messages=[raw_message("message-1")])
    assert repositories.projection.pending_accounts() == [ACCOUNT]
    manager = InMemoryTransportManager(repositories)

    async def exercise() -> None:
        await manager.start()
        task = manager._projection_tasks.get(ACCOUNT)
        assert task is not None
        await task
        assert repositories.projection.pending_accounts() == []
        await manager.stop()

    asyncio.run(exercise())
    snapshot = repositories.projection.snapshot(ACCOUNT)
    assert snapshot["projection"]["status"] == "current"
    assert [item["conversation_id"] for item in snapshot["conversations"]] == ["chat-1"]


def test_manager_start_finishes_a_committed_projection_activation_intent() -> None:
    repositories = create_canonical_repositories("memory")
    commit_seed(repositories, messages=[raw_message("message-1")])
    initial = repositories.projection.catch_up(ACCOUNT)
    assert initial is not None
    generation = repositories.projection.active_generation(ACCOUNT)
    assert generation is not None
    with repositories.database.transaction() as connection:
        connection.execute(
            "UPDATE account_heads SET view_revision=view_revision-1 WHERE creator_account_id=?",
            (ACCOUNT,),
        )
        connection.execute(
            """UPDATE projection_activation_intents SET state='pending',
                      generation_id=NULL,activated_view_revision=NULL,
                      projection_committed_at=NULL,activated_at=NULL
               WHERE creator_account_id=? AND target_canonical_revision=?""",
            (ACCOUNT, generation["projected_revision"]),
        )
    assert repositories.projection.pending_accounts() == [ACCOUNT]
    manager = InMemoryTransportManager(repositories)

    async def exercise() -> None:
        await manager.start()
        task = manager._projection_tasks.get(ACCOUNT)
        assert task is not None
        await task
        await manager.stop()

    asyncio.run(exercise())
    assert repositories.projection.pending_accounts() == []
    assert repositories.projection.active_generation(ACCOUNT) == generation
    assert repositories.projection.snapshot(ACCOUNT)["view_revision"] == initial["view_revision"]


class BlockingPipeline:
    def __init__(self) -> None:
        self.delegate = DeterministicProjectionPipeline()
        self.pipeline_version = self.delegate.pipeline_version
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = False

    def project(self, conversations, messages):
        if messages and not self.blocked:
            self.blocked = True
            self.entered.set()
            if not self.release.wait(10):
                raise TimeoutError("projection test release was not signalled")
        return self.delegate.project(conversations, messages)


def test_background_projection_keeps_previous_generation_readable() -> None:
    repositories = create_canonical_repositories("memory")
    key = commit_seed(repositories, messages=[raw_message("message-1")])
    initial = repositories.projection.catch_up(ACCOUNT)
    assert initial is not None
    commit_message_delta(
        repositories,
        key,
        sequence=1,
        origin="passive",
        message=raw_message("message-2"),
    )
    blocking = BlockingPipeline()
    repositories.projection.pipeline = blocking
    manager = InMemoryTransportManager(repositories)

    async def exercise() -> None:
        task = manager.schedule_projection(ACCOUNT)
        assert await asyncio.to_thread(blocking.entered.wait, 3)
        await asyncio.sleep(0)
        old_items, _, old_generation = repositories.projection.message_rows(
            ACCOUNT, "chat-1", before=None, limit=10
        )
        assert [item["message_id"] for item in old_items] == ["message-1"]
        assert old_generation["read_revision"] == initial["view_revision"]
        blocking.release.set()
        await task

    try:
        asyncio.run(exercise())
    finally:
        blocking.release.set()
    new_items, _, new_generation = repositories.projection.message_rows(
        ACCOUNT, "chat-1", before=None, limit=10
    )
    assert [item["message_id"] for item in new_items] == ["message-1", "message-2"]
    assert new_generation["read_revision"] == initial["view_revision"] + 1


class _RecordingBridge:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    async def send_text(self, text: str) -> None:
        self._sent.append(json.loads(text))

    async def close(self, code: int, reason: str) -> None:  # pragma: no cover - unused
        pass


def test_projection_catch_up_rebroadcasts_recovered_system_state() -> None:
    """A caught-up read model must re-emit system.state, not just state.snapshot.

    Regression for the live-canary finding: after a commit the Bridge rendered the
    freshly delivered conversations while the readiness chip stayed frozen on the
    stale bind-time "unavailable"/"degraded" value, because system.state was only
    ever sent once at bind and never refreshed when the projection recovered.
    """
    repositories = create_canonical_repositories("memory")
    key = commit_seed(repositories, messages=[raw_message("message-1")])
    assert repositories.projection.catch_up(ACCOUNT) is not None
    manager = InMemoryTransportManager(repositories)
    sent: list[dict] = []

    async def exercise() -> None:
        await manager.bind_bridge(
            _RecordingBridge(sent),
            principal_id="principal",
            creator_account_id=ACCOUNT,
            bridge_session_id=uuid4(),
        )
        # New canonical data lands; the durable read-model projection now lags.
        commit_message_delta(
            repositories,
            key,
            sequence=1,
            origin="passive",
            message=raw_message("message-2"),
        )
        sent.clear()
        # The commit handler serializes durable projection work per account.
        await manager.schedule_projection(ACCOUNT)

    asyncio.run(exercise())

    types = [message["type"] for message in sent]
    assert "state.snapshot" in types
    assert "system.state" in types
    system = next(message for message in sent if message["type"] == "system.state")
    detail = system["payload"]["detail"] or ""
    assert "projection=pending" not in detail
    assert "projection=unavailable" not in detail
    assert system["payload"]["readiness"] in {"ready", "degraded"}


def run_scale_qualification(message_count: int) -> tuple[int, int, float]:
    repositories = create_canonical_repositories("memory")
    key, commit = stage_snapshot(
        repositories,
        chats=[chat("chat-1")],
        messages=[raw_message(f"message-{index:06d}") for index in range(message_count)],
    )
    tracker = TrackingPipeline()
    repositories.projection.pipeline = tracker
    tracemalloc.start()
    started = time.monotonic()
    result = repositories.history.commit_snapshot(key, commit)
    assert result.status == "accepted"
    assert repositories.projection.catch_up(ACCOUNT) is not None
    elapsed = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert tracker.max_messages <= PROJECTION_BATCH_SIZE
    assert tracker.max_conversations <= CONVERSATION_BATCH_SIZE
    with repositories.projection_database.read() as connection:
        assert int(
            connection.execute("SELECT COUNT(*) FROM projection_message_analysis").fetchone()[0]
        ) == message_count
        assert int(
            connection.execute("SELECT COUNT(*) FROM projection_lpg_edges").fetchone()[0]
        ) == message_count
    return tracker.max_messages, peak, elapsed


def test_ten_thousand_message_brain_path_is_bounded() -> None:
    max_batch, peak, _ = run_scale_qualification(10_000)
    assert max_batch == PROJECTION_BATCH_SIZE
    assert peak < 32 * 1024 * 1024


@pytest.mark.skipif(
    os.environ.get("RUN_BETA_QUALIFICATION") != "1",
    reason="set RUN_BETA_QUALIFICATION=1 for the 100,000-message Beta gate",
)
def test_beta_hundred_thousand_message_brain_qualification() -> None:
    max_batch, peak, elapsed = run_scale_qualification(100_000)
    assert max_batch == PROJECTION_BATCH_SIZE
    assert peak < 64 * 1024 * 1024
    assert elapsed < 120
