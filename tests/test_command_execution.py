from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.api.endpoints.transport_ws import _handle_agent_message
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.services.command_execution import (
    CommandAlreadyExistsError,
    CommandReissueError,
)
from app.transport.manager import DEV_ACCOUNT_ID, transport_manager


NOW = datetime(2026, 7, 18, 10, 5, tzinfo=timezone.utc)
ACTION = {
    "type": "message.send",
    "conversation_id": "chat-1",
    "text": "Thanks for writing!",
    "media_url": None,
}


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, value: str) -> None:
        self.sent.append(json.loads(value))


@pytest.fixture(autouse=True)
def reset_manager():
    transport_manager.reset()
    yield
    transport_manager.reset()


async def bind_agent(
    websocket: RecordingWebSocket, *, now: datetime = NOW
):
    return await transport_manager.bind_agent(
        websocket,
        principal_id="test-principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision="config-8",
        now=now,
    )


def result_message(lease, command_id, *, status: str = "succeeded"):
    output = {"external_message_id": "platform-message-9"} if status == "succeeded" else None
    error = (
        {
            "code": "platform_error",
            "detail": "Platform rejected the command",
            "retryable": False,
        }
        if status == "failed"
        else None
    )
    document = {
        "type": "command.result",
        "protocol_version": "2",
        "message_id": str(uuid4()),
        "payload": {
            "connection_id": str(lease.connection_id),
            "fencing_token": lease.fencing_token,
            "creator_account_id": lease.creator_account_id,
            "command_id": str(command_id),
            "result_id": str(uuid4()),
            "status": status,
            "completed_at": NOW.isoformat(),
            "output": output,
            "error": error,
        },
    }
    return AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(document))


def test_command_reaches_only_the_current_fenced_writer() -> None:
    async def scenario():
        first_socket = RecordingWebSocket()
        second_socket = RecordingWebSocket()
        first = await bind_agent(first_socket)
        second = await bind_agent(second_socket)

        record = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            now=NOW,
        )

        assert first.status == "disconnected"
        assert first_socket.sent == []
        execute = second_socket.sent[-1]
        assert execute["type"] == "command.execute"
        assert execute["payload"]["connection_id"] == str(second.connection_id)
        assert execute["payload"]["fencing_token"] == second.fencing_token
        assert execute["payload"]["command_id"] == str(record.command_id)
        assert record.state == "issued"

    asyncio.run(scenario())


def test_duplicate_result_is_applied_once_and_acknowledged_every_time() -> None:
    async def scenario():
        socket = RecordingWebSocket()
        lease = await bind_agent(socket)
        command = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            now=NOW,
        )
        result = result_message(lease, command.command_id)

        assert await _handle_agent_message(socket, lease, result)
        assert await _handle_agent_message(socket, lease, result)

        acknowledgements = [
            message for message in socket.sent if message["type"] == "command.result.ack"
        ]
        assert len(acknowledgements) == 2
        assert all(
            message["payload"]["result_id"] == str(result.payload.result_id)
            for message in acknowledgements
        )
        saved = transport_manager.command_record(command.command_id)
        assert saved is not None
        assert saved.state == "succeeded"
        assert saved.result_apply_count == 1
        assert len(saved.receipts) == 2
        assert saved.receipts[1].duplicate is True

    asyncio.run(scenario())


def test_deadline_expiry_is_unknown_and_a_late_result_is_audited_without_reopening() -> None:
    async def scenario():
        socket = RecordingWebSocket()
        lease = await bind_agent(socket)
        command = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(seconds=5),
            now=NOW,
        )

        await transport_manager.expire(NOW + timedelta(seconds=5))
        expired = transport_manager.command_record(command.command_id)
        assert expired is not None
        assert expired.state == "unknown"
        assert expired.failure_reason == "deadline_expired"

        result = result_message(lease, command.command_id)
        assert await _handle_agent_message(socket, lease, result)

        late = transport_manager.command_record(command.command_id)
        assert late is not None
        assert late.state == "unknown"
        assert late.result is not None
        assert late.result.status == "succeeded"
        assert late.result_apply_count == 1
        assert late.receipts[-1].late is True
        assert socket.sent[-1]["type"] == "command.result.ack"

    asyncio.run(scenario())


def test_disconnected_and_stale_agents_take_the_auditable_refused_path() -> None:
    async def scenario():
        disconnected = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            now=NOW,
        )
        assert disconnected.state == "failed"
        assert disconnected.failure_reason == "agent_disconnected"
        assert disconnected.delivery_attempts == 0

        socket = RecordingWebSocket()
        lease = await bind_agent(socket)
        lease.status = "stale"
        stale = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            now=NOW,
        )
        assert stale.state == "failed"
        assert stale.failure_reason == "agent_stale"
        assert stale.delivery_attempts == 0
        assert socket.sent == []

    asyncio.run(scenario())


def test_command_identity_cannot_be_reminted_and_unsafe_reissue_is_blocked() -> None:
    async def scenario():
        socket = RecordingWebSocket()
        await bind_agent(socket)
        command_id = uuid4()
        command = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            command_id=command_id,
            now=NOW,
        )
        with pytest.raises(CommandAlreadyExistsError):
            await transport_manager.issue_command(
                DEV_ACCOUNT_ID,
                action=ACTION,
                deadline=NOW + timedelta(minutes=5),
                command_id=command_id,
                now=NOW,
            )
        assert len([message for message in socket.sent if message["type"] == "command.execute"]) == 1

        command.idempotency_policy = "none"  # type: ignore[assignment]
        transport_manager.commands.repository.save(command)
        with pytest.raises(CommandReissueError):
            await transport_manager.reissue_command(command_id, now=NOW)
        assert len([message for message in socket.sent if message["type"] == "command.execute"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["accepted", "succeeded", "failed"])
def test_every_persisted_command_result_status_is_acknowledged(status: str) -> None:
    async def scenario():
        socket = RecordingWebSocket()
        lease = await bind_agent(socket)
        command = await transport_manager.issue_command(
            DEV_ACCOUNT_ID,
            action=ACTION,
            deadline=NOW + timedelta(minutes=5),
            now=NOW,
        )
        result = result_message(lease, command.command_id, status=status)
        assert await _handle_agent_message(socket, lease, result)
        assert socket.sent[-1]["type"] == "command.result.ack"
        assert socket.sent[-1]["payload"]["command_id"] == str(command.command_id)

    asyncio.run(scenario())
