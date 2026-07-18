from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints import transport_ws
from app.api.endpoints.transport_ws import _handle_agent_message
from app.main import app
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.services.agent_configuration import (
    AgentConfigurationAuthority,
    build_config_document,
    config_document_digest,
)
from app.services.command_execution import (
    CommandAlreadyExistsError,
    CommandDeliveryTarget,
    CommandService,
)
from app.transport.manager import DEV_AUTH_TICKET, InMemoryTransportManager
from app.transport.ingestion import IngestionService, StreamKey


FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v1"
ACCOUNT_ID = "dev-creator-account"
NOW = datetime(2026, 7, 18, 10, 5, tzinfo=timezone.utc)
ACTION = {
    "type": "message.send",
    "conversation_id": "chat-1",
    "text": "Thanks for writing!",
    "media_url": None,
}
CAPTURE_POLICY = {
    "observation_interval_seconds": 60,
    "rules": [
        {
            "resource": "messages",
            "url_pattern": "/api2/v2/chats/*/messages",
            "enabled": True,
        }
    ],
}


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, value: str) -> None:
        self.sent.append(json.loads(value))


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def payload(name: str):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(fixture(name))
    ).payload


def stream_key() -> StreamKey:
    hello = payload("agent.hello")
    return StreamKey(ACCOUNT_ID, hello.agent_installation_id, hello.agent_stream_id)


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repositories(request, tmp_path: Path) -> CanonicalRepositories:
    if request.param == "memory":
        return create_canonical_repositories("memory")
    return create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )


@pytest.mark.asyncio
async def test_ingestion_contiguity_dedup_atomic_replacement_and_revision_contract(
    repositories: CanonicalRepositories,
) -> None:
    service = IngestionService(repositories.ingestion)
    key = stream_key()

    snapshot = await service.ingest_snapshot(key, payload("ingest.snapshot"))
    delta = await service.ingest_delta(key, payload("ingest.delta"))
    duplicate = await service.ingest_delta(key, payload("ingest.delta"))

    assert (snapshot.status, snapshot.committed_source_seq) == ("accepted", 10)
    assert (delta.status, delta.committed_source_seq) == ("accepted", 11)
    assert (duplicate.status, duplicate.committed_source_seq) == ("duplicate", 11)
    assert duplicate.state_delta is None
    assert [snapshot.state_delta["view_revision"], delta.state_delta["view_revision"]] == [
        1,
        2,
    ]

    gap_document = fixture("ingest.delta")
    gap_document["payload"].update(source_seq=13, event_id=str(uuid4()))
    gap = await service.ingest_delta(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(gap_document)).payload
    )
    assert (gap.status, gap.code, gap.retryable) == ("gap", "sequence_gap", True)

    conflicting = fixture("ingest.delta")
    conflicting["payload"]["event_id"] = str(uuid4())
    conflict = await service.ingest_delta(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(conflicting)).payload
    )
    assert (conflict.status, conflict.code) == ("rejected", "invariant_failed")

    before = service.state_snapshot_payload(ACCOUNT_ID)
    invalid_document = fixture("ingest.snapshot")
    invalid_document["payload"].update(snapshot_id=str(uuid4()), through_seq=12)
    invalid_document["payload"]["chats"].append(
        deepcopy(invalid_document["payload"]["chats"][0])
    )
    invalid = await service.ingest_snapshot(
        key, AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(invalid_document)).payload
    )
    after = service.state_snapshot_payload(ACCOUNT_ID)

    assert invalid.status == "rejected"
    assert service.checkpoint(key) == 11
    assert after["view_revision"] == before["view_revision"] == 2
    assert after["conversations"] == before["conversations"]

    persisted_stream = repositories.ingestion.stream(key)
    skipped_revision = repositories.ingestion.account_read_model(ACCOUNT_ID)
    assert persisted_stream is not None
    skipped_revision.view_revision += 2
    assert not repositories.ingestion.commit_snapshot(
        key,
        expected_checkpoint=11,
        stream=persisted_stream,
        account=skipped_revision,
    )
    assert repositories.ingestion.account_read_model(ACCOUNT_ID).view_revision == 2


@pytest.mark.asyncio
async def test_configuration_immutability_monotonic_digest_etag_and_drift_contract(
    repositories: CanonicalRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = AgentConfigurationAuthority(repositories.configuration)
    installation_id = uuid4()
    initial = authority.bind_installation(ACCOUNT_ID, installation_id, "config-7")
    assert initial.required_config_revision == initial.applied_config_revision == "config-7"

    published = await authority.publish(
        ACCOUNT_ID,
        capture_policy=CAPTURE_POLICY,
        command_policy={
            "allowed_actions": ["message.send"],
            "max_text_length": 500,
            "require_idempotency": True,
        },
        issued_at=NOW,
    )
    assert published.config_revision == published.etag == "config-8"
    assert published.digest == config_document_digest(published)
    assert authority.required_document(ACCOUNT_ID).etag == published.etag
    manager = InMemoryTransportManager(repositories)
    monkeypatch.setattr(transport_ws, "transport_manager", manager)
    parameters = {
        "auth_ticket": DEV_AUTH_TICKET,
        "agent_installation_id": str(installation_id),
        "creator_account_id": ACCOUNT_ID,
        "supported_config_schema_versions": "1",
    }
    response = TestClient(app).get("/api/v1/agent/config", params=parameters)
    assert response.status_code == 200
    assert response.json()["digest"] == published.digest
    not_modified = TestClient(app).get(
        "/api/v1/agent/config",
        params=parameters,
        headers={"If-None-Match": f'W/"{published.etag}"'},
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == published.etag

    drift = authority.installation(ACCOUNT_ID, installation_id)
    assert drift.required_config_revision == "config-8"
    assert drift.applied_config_revision == "config-7"
    converged = authority.record_report(
        ACCOUNT_ID,
        installation_id,
        config_revision="config-8",
        digest=published.digest,
        outcome="applied",
        capability_details=[],
    )
    assert converged.required_config_revision == converged.applied_config_revision
    assert converged.last_failure is None

    with pytest.raises(ValueError, match="immutable"):
        repositories.configuration.add_document(published)

    stale = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision="config-6",
        issued_at=NOW,
        capture_policy=CAPTURE_POLICY,
        command_policy={
            "allowed_actions": [],
            "max_text_length": 100,
            "require_idempotency": True,
        },
    )
    with pytest.raises(ValueError, match="monotonically"):
        repositories.configuration.add_document(stale)

    bad_digest = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision="config-9",
        issued_at=NOW,
        capture_policy=CAPTURE_POLICY,
        command_policy={
            "allowed_actions": [],
            "max_text_length": 100,
            "require_idempotency": True,
        },
    ).model_copy(update={"digest": "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="digest"):
        repositories.configuration.add_document(bad_digest)


@pytest.mark.asyncio
async def test_command_identity_deadline_result_dedup_and_receipt_contract(
    repositories: CanonicalRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CommandService(repositories.commands)
    command_id = uuid4()
    target = CommandDeliveryTarget(uuid4(), "fence-1", ACCOUNT_ID)

    async def sender(_: dict) -> None:
        return None

    command = await service.issue(
        creator_account_id=ACCOUNT_ID,
        action=ACTION,
        deadline=NOW + timedelta(minutes=5),
        target=target,
        sender=sender,
        command_id=command_id,
        now=NOW,
    )
    assert command.state == "issued"
    with pytest.raises(CommandAlreadyExistsError):
        await service.issue(
            creator_account_id=ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            target=target,
            sender=sender,
            command_id=command_id,
            now=NOW,
        )

    result_id = uuid4()
    result = {
        "creator_account_id": ACCOUNT_ID,
        "command_id": command_id,
        "result_id": result_id,
        "status": "succeeded",
        "completed_at": NOW + timedelta(seconds=2),
        "output": {"external_message_id": "message-9"},
        "error": None,
    }
    first_receipt = service.record_result(result, received_at=NOW + timedelta(seconds=3))
    second_receipt = service.record_result(result, received_at=NOW + timedelta(seconds=4))
    stored = service.get(command_id)
    assert first_receipt.duplicate is False
    assert second_receipt.duplicate is True
    assert stored is not None
    assert stored.state == "succeeded"
    assert stored.result_apply_count == 1
    assert [receipt.duplicate for receipt in stored.receipts] == [False, True]

    late_id = uuid4()
    late_command = await service.issue(
        creator_account_id=ACCOUNT_ID,
        action=ACTION,
        deadline=NOW + timedelta(seconds=5),
        target=target,
        sender=sender,
        command_id=late_id,
        now=NOW,
    )
    assert service.expire(NOW + timedelta(seconds=5))[0].state == "unknown"
    late_receipt = service.record_result(
        {
            "creator_account_id": ACCOUNT_ID,
            "command_id": late_command.command_id,
            "result_id": uuid4(),
            "status": "succeeded",
            "completed_at": NOW + timedelta(seconds=6),
            "output": {"external_message_id": "message-10"},
            "error": None,
        },
        received_at=NOW + timedelta(seconds=7),
    )
    late_stored = service.get(late_id)
    assert late_receipt.late is True
    assert late_stored is not None and late_stored.state == "unknown"
    assert late_stored.receipts[-1].late is True

    manager = InMemoryTransportManager(repositories)
    socket = RecordingWebSocket()
    stream = stream_key()
    lease = await manager.bind_agent(
        socket,
        principal_id="principal",
        creator_account_id=ACCOUNT_ID,
        agent_installation_id=stream.agent_installation_id,
        agent_stream_id=stream.agent_stream_id,
        applied_config_revision="config-7",
        now=NOW,
    )
    monkeypatch.setattr(transport_ws, "transport_manager", manager)
    message = AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(
            {
                "type": "command.result",
                "protocol_version": "1",
                "message_id": str(uuid4()),
                "payload": {
                    "connection_id": str(lease.connection_id),
                    "fencing_token": lease.fencing_token,
                    "creator_account_id": ACCOUNT_ID,
                    "command_id": str(command_id),
                    "result_id": str(result_id),
                    "status": "succeeded",
                    "completed_at": (NOW + timedelta(seconds=2)).isoformat(),
                    "output": {"external_message_id": "message-9"},
                    "error": None,
                },
            }
        )
    )
    assert await _handle_agent_message(socket, lease, message)
    assert socket.sent[-1]["type"] == "command.result.ack"
    assert socket.sent[-1]["payload"]["result_id"] == str(result_id)
