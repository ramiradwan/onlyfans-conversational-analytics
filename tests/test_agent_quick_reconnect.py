from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.persistence.factory import create_canonical_repositories
from app.transport.manager import (
    DEV_ACCOUNT_ID,
    LEASE_EXPIRED_CLOSE_CODE,
    REQUIRED_CONFIG_REVISION,
    InMemoryTransportManager,
    utc_now,
)


class RecordingWebSocket:
    def __init__(self, name: str, events: list[tuple]) -> None:
        self.name = name
        self.events = events

    async def send_text(self, text: str) -> None:
        self.events.append((self.name, "send", json.loads(text)))

    async def close(self, code: int, reason: str) -> None:
        self.events.append((self.name, "close", code, reason))


@pytest.mark.asyncio
async def test_quick_reconnect_supersedes_without_expiring_new_session() -> None:
    manager = InMemoryTransportManager(create_canonical_repositories("memory"))
    events: list[tuple] = []
    started = utc_now()
    first = await manager.bind_agent(
        RecordingWebSocket("first", events),
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=REQUIRED_CONFIG_REVISION,
        now=started,
    )
    second = await manager.bind_agent(
        RecordingWebSocket("second", events),
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=REQUIRED_CONFIG_REVISION,
        now=started + timedelta(seconds=15),
    )
    assert first.status == "disconnected"
    assert manager.active_agents[DEV_ACCOUNT_ID] is second
    await manager.expire(started + timedelta(seconds=first.lease_timeout_seconds))
    assert manager.active_agents[DEV_ACCOUNT_ID] is second
    assert not any(event[:3] == ("second", "close", LEASE_EXPIRED_CLOSE_CODE) for event in events)

    await manager.issue_command(
        DEV_ACCOUNT_ID,
        action={"type": "message.send", "conversation_id": "chat-1", "text": "Hello", "media_url": None},
        deadline=started + timedelta(minutes=5),
        now=started + timedelta(seconds=16),
    )
    commands = [event for event in events if event[1] == "send" and event[2]["type"] == "command.execute"]
    assert len(commands) == 1
    assert commands[0][0] == "second"
    assert commands[0][2]["payload"]["connection_id"] == str(second.connection_id)
