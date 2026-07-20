from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.services.agent_configuration import (
    BOOTSTRAP_CAPTURE_POLICY,
    BOOTSTRAP_CONFIG_REVISION,
    AgentConfigurationAuthority,
    InMemoryAgentConfigRepository,
    config_document_digest,
)
from app.transport import DEV_ACCOUNT_ID, DEV_AGENT_AUTH_TICKET, transport_manager


FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v2"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def reset_transport() -> None:
    transport_manager.reset()
    yield
    transport_manager.reset()


def config_params(**overrides) -> dict[str, str]:
    values = {
        "agent_installation_id": str(uuid4()),
        "creator_account_id": DEV_ACCOUNT_ID,
        "supported_config_schema_versions": "2",
    }
    values.update(overrides)
    return values


def config_headers(ticket: str = DEV_AGENT_AUTH_TICKET, **overrides: str) -> dict[str, str]:
    values = {"Authorization": f"Bearer {ticket}"}
    values.update(overrides)
    return values


def agent_handshake(socket, hello: dict | None = None) -> tuple[dict, dict]:
    hello = deepcopy(hello or fixture("agent.hello"))
    socket.send_json(hello)
    session = socket.receive_json()
    assert session["type"] == "agent.session"
    if session["payload"]["resume_action"] == "snapshot_required":
        assert socket.receive_json()["type"] == "sync.required"
    return hello, session


def bind_report(
    report: dict,
    session: dict,
    *,
    revision: str,
    digest: str,
) -> dict:
    report = deepcopy(report)
    report["message_id"] = str(uuid4())
    report["payload"].update(
        connection_id=session["payload"]["connection_id"],
        fencing_token=session["payload"]["fencing_token"],
        creator_account_id=DEV_ACCOUNT_ID,
        config_revision=revision,
        digest=digest,
        outcome="applied",
    )
    return report


def test_authenticated_config_fetch_has_real_digest_and_conditional_etag() -> None:
    client = TestClient(app)
    response = client.get(
        "/api/v1/agent/config", params=config_params(), headers=config_headers()
    )
    assert response.status_code == 200
    document = response.json()
    assert document["digest"] == config_document_digest(document)
    assert response.headers["etag"] == document["etag"] == document["config_revision"]

    not_modified = client.get(
        "/api/v1/agent/config",
        params=config_params(),
        headers=config_headers(**{"If-None-Match": f'"{document["etag"]}"'}),
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == document["etag"]


def test_config_fetch_rejects_missing_invalid_and_unauthorized_stub_context() -> None:
    client = TestClient(app)
    assert client.get("/api/v1/agent/config", params=config_params()).status_code == 401
    assert (
        client.get(
            "/api/v1/agent/config",
            params=config_params(),
            headers=config_headers("wrong"),
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/agent/config",
            params=config_params(creator_account_id="another-account"),
            headers=config_headers(),
        ).status_code
        == 401
    )


def test_repository_seals_each_monotonic_revision_against_mutation() -> None:
    repository = InMemoryAgentConfigRepository()
    authority = AgentConfigurationAuthority(repository)
    capture_policy = {
        "observation_interval_seconds": 45,
        "rules": [
            {
                "resource": "presence",
                "url_pattern": "/api2/v2/users/list",
                "enabled": True,
            }
        ],
    }
    published = asyncio.run(
        authority.publish(
            DEV_ACCOUNT_ID,
            capture_policy=capture_policy,
            command_policy={
                "allowed_actions": [],
                "max_text_length": 500,
                "require_idempotency": True,
            },
            issued_at=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        )
    )
    assert published.config_revision == "config-9"

    capture_policy["rules"][0]["enabled"] = False
    published.capture_policy.rules[0].enabled = False
    stored = repository.document(DEV_ACCOUNT_ID, "config-9")
    assert stored is not None
    assert stored.capture_policy.rules[0].enabled is True
    with pytest.raises(ValueError, match="immutable"):
        repository.add_document(stored)


def test_bootstrap_requires_dependency_closed_config_8() -> None:
    repository = InMemoryAgentConfigRepository()
    authority = AgentConfigurationAuthority(repository)

    current = repository.document(DEV_ACCOUNT_ID, BOOTSTRAP_CONFIG_REVISION)
    assert current is not None
    assert authority.required_document(DEV_ACCOUNT_ID).config_revision == "config-8"
    assert current.capture_policy.model_dump(mode="json") == BOOTSTRAP_CAPTURE_POLICY
    assert {
        (rule.resource, rule.url_pattern)
        for rule in current.capture_policy.rules
        if rule.enabled
    } == {
        ("chats", "/api2/v2/chats"),
        ("chats", "/api2/v2/users/*/chats"),
        ("messages", "/api2/v2/chats/*/messages"),
        ("messages", "/ws3"),
    }


def test_publish_rejects_message_capture_without_chat_dependency() -> None:
    authority = AgentConfigurationAuthority(InMemoryAgentConfigRepository())
    with pytest.raises(ValueError, match="requires enabled chat capture"):
        asyncio.run(
            authority.publish(
                DEV_ACCOUNT_ID,
                capture_policy={
                    "observation_interval_seconds": 60,
                    "rules": [
                        {
                            "resource": "messages",
                            "url_pattern": "/api2/v2/chats/*/messages",
                            "enabled": True,
                        }
                    ],
                },
                command_policy={
                    "allowed_actions": [],
                    "max_text_length": 500,
                    "require_idempotency": True,
                },
            )
        )


def test_publish_signals_connected_agent_and_new_session_self_heals_loss() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as agent:
        _, session = agent_handshake(agent)
        assert session["payload"]["required_config_revision"] == "config-8"
        published = asyncio.run(
            transport_manager.publish_config(
                DEV_ACCOUNT_ID,
                capture_policy={
                    "observation_interval_seconds": 60,
                    "rules": [
                        {
                            "resource": "chats",
                            "url_pattern": "/api2/v2/chats",
                            "enabled": True,
                        },
                        {
                            "resource": "messages",
                            "url_pattern": "/api2/v2/chats/*/messages",
                            "enabled": True,
                        }
                    ],
                },
                command_policy={
                    "allowed_actions": ["message.send"],
                    "max_text_length": 800,
                    "require_idempotency": True,
                },
            )
        )
        available = agent.receive_json()
        assert available["type"] == "config.available"
        assert available["payload"]["required_config_revision"] == "config-9"
        assert available["payload"]["digest"] == published.digest

        assert asyncio.run(transport_manager.signal_config_available(DEV_ACCOUNT_ID))
        repeated_signal = agent.receive_json()
        assert repeated_signal["type"] == "config.available"
        assert repeated_signal["payload"] == available["payload"]

    with client.websocket_connect("/ws/agent") as reconnected:
        _, repeated = agent_handshake(reconnected)
        assert repeated["payload"]["required_config_revision"] == "config-9"


def test_config_drift_stays_degraded_for_stale_report_and_clears_on_confirmation() -> None:
    client = TestClient(app)
    initial = transport_manager.required_config_document(DEV_ACCOUNT_ID)
    with client.websocket_connect("/ws/bridge") as bridge:
        bridge.send_json(fixture("bridge.hello"))
        assert bridge.receive_json()["type"] == "bridge.session"
        for _ in range(4):
            bridge.receive_json()

        with client.websocket_connect("/ws/agent") as agent:
            hello = fixture("agent.hello")
            hello["payload"]["applied_config_revision"] = "config-8"
            _, session = agent_handshake(agent, hello)
            connected = bridge.receive_json()
            assert connected["type"] == "agent.state"
            assert connected["payload"]["degraded_reason"] is None

            published = asyncio.run(
                transport_manager.publish_config(
                    DEV_ACCOUNT_ID,
                    capture_policy={
                        "observation_interval_seconds": 90,
                        "rules": [
                            {
                                "resource": "chats",
                                "url_pattern": "/api2/v2/chats",
                                "enabled": True,
                            }
                        ],
                    },
                    command_policy={
                        "allowed_actions": [],
                        "max_text_length": 250,
                        "require_idempotency": True,
                    },
                )
            )
            drift = bridge.receive_json()
            assert drift["payload"]["required_config_revision"] == "config-9"
            assert drift["payload"]["applied_config_revision"] == "config-8"
            assert drift["payload"]["degraded_reason"] is not None
            assert agent.receive_json()["type"] == "config.available"

            stale = bind_report(
                fixture("config.applied"),
                session,
                revision=initial.config_revision,
                digest=initial.digest,
            )
            asyncio.run(
                transport_manager.record_config_applied(
                    transport_manager.active_agents[DEV_ACCOUNT_ID],
                    AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(stale)).payload,
                )
            )
            stale_state = bridge.receive_json()
            assert stale_state["type"] == "agent.state"
            assert stale_state["payload"]["required_config_revision"] == "config-9"
            assert stale_state["payload"]["applied_config_revision"] == "config-8"
            assert stale_state["payload"]["degraded_reason"] is not None
            # Applying config now also refreshes readiness (configuration alignment
            # can change), so a system.state trails each agent.state.
            stale_readiness = bridge.receive_json()
            assert stale_readiness["type"] == "system.state"

            current = bind_report(
                fixture("config.applied"),
                session,
                revision=published.config_revision,
                digest=published.digest,
            )
            asyncio.run(
                transport_manager.record_config_applied(
                    transport_manager.active_agents[DEV_ACCOUNT_ID],
                    AGENT_TO_BRAIN_ADAPTER.validate_json(json.dumps(current)).payload,
                )
            )
            converged = bridge.receive_json()
            assert converged["type"] == "agent.state"
            assert converged["payload"]["required_config_revision"] == "config-9"
            assert converged["payload"]["applied_config_revision"] == "config-9"
            assert converged["payload"]["degraded_reason"] is None
            converged_readiness = bridge.receive_json()
            assert converged_readiness["type"] == "system.state"

