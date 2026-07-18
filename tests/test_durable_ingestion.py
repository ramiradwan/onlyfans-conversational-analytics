from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.transport import DEV_ACCOUNT_ID, transport_manager
from app.transport.ingestion import InMemoryIngestionRepository, IngestionService, StreamKey

FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v1"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def payload(name: str):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(fixture(name))).payload


def stream_key() -> StreamKey:
    hello = payload("agent.hello")
    return StreamKey(DEV_ACCOUNT_ID, hello.agent_installation_id, hello.agent_stream_id)


@pytest.fixture(autouse=True)
def reset_transport():
    transport_manager.reset()
    yield
    transport_manager.reset()


@pytest.mark.asyncio
async def test_duplicate_event_is_acked_without_double_commit_and_contiguous_advances() -> None:
    service = IngestionService(InMemoryIngestionRepository())
    key = stream_key()
    snapshot = await service.ingest_snapshot(key, payload("ingest.snapshot"))
    assert snapshot.committed_source_seq == 10

    delta_payload = payload("ingest.delta")
    committed = await service.ingest_delta(key, delta_payload)
    duplicate = await service.ingest_delta(key, delta_payload)

    assert (committed.status, committed.committed_source_seq) == ("accepted", 11)
    assert (duplicate.status, duplicate.committed_source_seq) == ("duplicate", 11)
    assert duplicate.state_delta is None
    read_model = service.state_snapshot_payload(DEV_ACCOUNT_ID)
    assert read_model["view_revision"] == 2
    assert [message["message_id"] for message in read_model["conversations"][0]["messages"]] == [
        "message-1",
        "message-2",
    ]


@pytest.mark.asyncio
async def test_gap_and_nonretryable_item_leave_checkpoint_unchanged() -> None:
    service = IngestionService(InMemoryIngestionRepository())
    key = stream_key()
    await service.ingest_snapshot(key, payload("ingest.snapshot"))

    gap_document = fixture("ingest.delta")
    gap_document["payload"].update(source_seq=12, event_id=str(uuid4()))
    gap = await service.ingest_delta(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(gap_document)).payload
    )
    assert (gap.status, gap.code, gap.retryable) == ("gap", "sequence_gap", True)
    assert service.checkpoint(key) == 10

    poison_document = fixture("ingest.delta")
    poison_document["payload"]["event_id"] = str(uuid4())
    poison_document["payload"]["change"]["message"]["chat_id"] = "missing-chat"
    poison = await service.ingest_delta(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(poison_document)).payload
    )
    assert (poison.status, poison.code, poison.retryable) == (
        "rejected",
        "invariant_failed",
        False,
    )
    assert service.checkpoint(key) == 10


@pytest.mark.asyncio
async def test_snapshot_replacement_failure_is_atomic() -> None:
    service = IngestionService(InMemoryIngestionRepository())
    key = stream_key()
    await service.ingest_snapshot(key, payload("ingest.snapshot"))
    before = service.state_snapshot_payload(DEV_ACCOUNT_ID)

    invalid_document = fixture("ingest.snapshot")
    invalid_document["payload"].update(snapshot_id=str(uuid4()), through_seq=11)
    invalid_document["payload"]["chats"].append(
        dict(invalid_document["payload"]["chats"][0])
    )
    invalid = await service.ingest_snapshot(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(invalid_document)).payload
    )

    assert invalid.status == "rejected"
    assert service.checkpoint(key) == 10
    after = service.state_snapshot_payload(DEV_ACCOUNT_ID)
    assert after["view_revision"] == before["view_revision"]
    assert after["conversations"] == before["conversations"]


def test_sequence_gap_uses_ingest_rejected_and_does_not_advance_checkpoint() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as agent:
        hello = fixture("agent.hello")
        agent.send_json(hello)
        session = agent.receive_json()
        assert agent.receive_json()["type"] == "sync.required"

        snapshot = fixture("ingest.snapshot")
        snapshot["payload"].update(
            connection_id=session["payload"]["connection_id"],
            fencing_token=session["payload"]["fencing_token"],
            creator_account_id=DEV_ACCOUNT_ID,
            agent_installation_id=hello["payload"]["agent_installation_id"],
            agent_stream_id=hello["payload"]["agent_stream_id"],
        )
        agent.send_json(snapshot)
        assert agent.receive_json()["payload"]["committed_source_seq"] == 10

        gap = fixture("ingest.delta")
        gap["payload"].update(
            connection_id=session["payload"]["connection_id"],
            fencing_token=session["payload"]["fencing_token"],
            creator_account_id=DEV_ACCOUNT_ID,
            agent_installation_id=hello["payload"]["agent_installation_id"],
            agent_stream_id=hello["payload"]["agent_stream_id"],
            event_id=str(uuid4()),
            source_seq=12,
        )
        agent.send_json(gap)
        rejected = agent.receive_json()

        assert rejected["type"] == "ingest.rejected"
        assert rejected["payload"]["code"] == "sequence_gap"
        assert rejected["payload"]["retryable"] is True
        lease = transport_manager.active_agents[DEV_ACCOUNT_ID]
        assert transport_manager.checkpoint_for(lease) == 10


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


@pytest.mark.asyncio
async def test_state_deltas_broadcast_contiguously_and_checkpoint_resumes() -> None:
    key = stream_key()
    bridge_socket = FakeWebSocket()
    await transport_manager.bind_bridge(
        bridge_socket,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        bridge_session_id=uuid4(),
    )
    agent_socket = FakeWebSocket()
    lease = await transport_manager.bind_agent(
        agent_socket,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=key.agent_installation_id,
        agent_stream_id=key.agent_stream_id,
        applied_config_revision="config-7",
    )
    bridge_socket.sent.clear()

    snapshot = await transport_manager.ingestion.ingest_snapshot(key, payload("ingest.snapshot"))
    assert snapshot.state_delta is not None
    await transport_manager.broadcast_state_delta(DEV_ACCOUNT_ID, snapshot.state_delta)
    await asyncio.sleep(0.15)

    delta = await transport_manager.ingestion.ingest_delta(key, payload("ingest.delta"))
    assert delta.state_delta is not None
    await transport_manager.broadcast_state_delta(DEV_ACCOUNT_ID, delta.state_delta)
    await asyncio.sleep(0.15)

    state_deltas = [message for message in bridge_socket.sent if message["type"] == "state.delta"]
    assert [message["payload"]["view_revision"] for message in state_deltas] == [1, 2]
    assert state_deltas[-1]["payload"]["changes"][0]["conversation"]["messages"][-1][
        "message_id"
    ] == "message-2"

    await transport_manager.disconnect_agent(lease.connection_id)
    resumed = await transport_manager.bind_agent(
        FakeWebSocket(),
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=key.agent_installation_id,
        agent_stream_id=key.agent_stream_id,
        applied_config_revision="config-7",
    )
    assert resumed.connection_id != lease.connection_id
    assert resumed.fencing_token != lease.fencing_token
    assert transport_manager.checkpoint_for(resumed) == 11
