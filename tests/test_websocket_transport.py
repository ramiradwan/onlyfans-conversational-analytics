from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings, settings
from app.main import app
from app.persistence.auth import (
    AuthorizedAccountBinding,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.transport import DEV_ACCOUNT_ID, DEV_AGENT_AUTH_TICKET, transport_manager
from app.transport.manager import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_EXPIRED_CLOSE_CODE,
    LEASE_TIMEOUT_SECONDS,
    REQUIRED_CONFIG_REVISION,
    AuthenticationError,
    InMemoryTransportManager,
    authorized_account_ids,
    utc_now,
)


FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v2"
AUTHORIZATION_GRANT_TYPES = (
    "installation_grant",
    "membership_snapshot",
    "creator_account_binding",
)
INSTALLATION_ID = "transport-installation"
ORGANIZATION_ID = "transport-organization"
INSTALLATION_KEY_ID = "transport-installation-key"
INSTALLATION_KEY_JKT = "transport-installation-key-thumbprint"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def authorize_account_durably(path: Path, account_id: str) -> None:
    """Record one account's durable authorization in a fresh store at `path`.

    Writes what provisioning writes: an activated installation key, the grant
    tuple the authorization rests on, the approved candidate, and the binding.
    Grant validity is anchored to the current instant because eligibility is
    resolved against the wall clock at the moment it is asked for.
    """

    now = utc_now()
    store = SQLiteAuthenticationStore(path)
    store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=now - timedelta(minutes=10),
        )
    )
    store.activate_installation_key(
        InstallationKeyReference(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            installation_key_id=INSTALLATION_KEY_ID,
            installation_key_jkt=INSTALLATION_KEY_JKT,
            public_key_jwk='{"kty":"EC"}',
            created_at=now - timedelta(minutes=10),
            activated_at=now - timedelta(minutes=9),
        )
    )
    reference_ids: list[str] = []
    for grant_type in AUTHORIZATION_GRANT_TYPES:
        reference_id = f"{grant_type}-{account_id}"
        membership = grant_type == "membership_snapshot"
        store.record_verified_grant(
            VerifiedGrantReference(
                reference_id=reference_id,
                grant_identifier=f"{reference_id}-identifier",
                grant_type=grant_type,
                grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
                issuer="https://identity.example",
                subject="external-subject-1",
                installation_id=INSTALLATION_ID,
                creator_account_id=(
                    account_id if grant_type == "creator_account_binding" else None
                ),
                valid_from=now - timedelta(hours=1),
                expires_at=now + timedelta(hours=1),
                verified_at=now - timedelta(minutes=5),
                organization_id=ORGANIZATION_ID,
                installation_key_id=INSTALLATION_KEY_ID,
                installation_key_jkt=INSTALLATION_KEY_JKT,
                membership_id=f"membership-{account_id}" if membership else None,
                allowed_creator_account_ids=(account_id,) if membership else None,
                membership_roles=("owner",) if membership else None,
            )
        )
        reference_ids.append(reference_id)
    association_request_id = f"association-{account_id}"
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id=f"onboarding-{account_id}",
            organization_id=ORGANIZATION_ID,
            creator_account_id=account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=now - timedelta(minutes=3),
        )
    )
    store.approve_provisioning_candidate(
        association_request_id, resolved_at=now - timedelta(minutes=2)
    )
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=account_id,
            installation_id=INSTALLATION_ID,
            platform_creator_id=account_id,
            association_request_id=association_request_id,
            grant_bundle_sha256=hashlib.sha256(
                "\n".join(sorted(reference_ids)).encode()
            ).hexdigest(),
            authorized_at=now - timedelta(minutes=1),
            grant_reference_ids=tuple(reference_ids),
        )
    )


def agent_handshake(socket) -> tuple[dict, dict]:
    hello = fixture("agent.hello")
    socket.send_json(hello)
    session = socket.receive_json()
    assert session["type"] == "agent.session"
    assert session["correlation_id"] == hello["message_id"]
    assert session["payload"]["creator_account_id"] == DEV_ACCOUNT_ID
    assert session["payload"]["fencing_token"]
    if session["payload"]["resume_action"] == "snapshot_required":
        sync_required = socket.receive_json()
        assert sync_required["type"] == "sync.required"
        assert sync_required["payload"]["connection_id"] == session["payload"]["connection_id"]
    return hello, session


def bridge_handshake(socket) -> tuple[dict, dict, list[dict]]:
    hello = fixture("bridge.hello")
    socket.send_json(hello)
    session = socket.receive_json()
    initial = [socket.receive_json() for _ in range(4)]
    assert session["type"] == "bridge.session"
    assert [message["type"] for message in initial] == [
        "state.snapshot",
        "presence.state",
        "agent.state",
        "system.state",
    ]
    return hello, session, initial


@pytest.fixture(autouse=True)
def reset_transport_manager():
    transport_manager.reset()
    yield
    transport_manager.reset()


def bind_agent_payload(message: dict, session: dict, hello: dict) -> dict:
    payload = message["payload"]
    payload["connection_id"] = session["payload"]["connection_id"]
    payload["fencing_token"] = session["payload"]["fencing_token"]
    payload["creator_account_id"] = DEV_ACCOUNT_ID
    if "agent_installation_id" in payload:
        payload["agent_installation_id"] = hello["payload"]["agent_installation_id"]
    if "agent_stream_id" in payload:
        payload["agent_stream_id"] = hello["payload"]["agent_stream_id"]
    return message


def commit_fixture_snapshot(socket, session: dict, hello: dict) -> tuple[dict, dict]:
    begin = bind_agent_payload(fixture("ingest.snapshot"), session, hello)
    begin["payload"].update(
        chunk_count=2,
        record_counts={"chats": 1, "messages": 1, "coverage_evidence": 0},
    )
    socket.send_json(begin)
    assert socket.receive_json()["type"] == "ingest.ack"
    identity = {
        name: begin["payload"][name]
        for name in (
            "connection_id",
            "fencing_token",
            "creator_account_id",
            "agent_installation_id",
            "agent_stream_id",
            "snapshot_id",
        )
    }
    frames = [
        {
            "type": "ingest.snapshot",
            "protocol_version": "2",
            "message_id": str(uuid4()),
            "payload": {
                **identity,
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
                            "display_name": "Fan One",
                            "updated_at": "2026-07-19T10:00:00Z",
                        },
                    }
                ],
            },
        },
        {
            "type": "ingest.snapshot",
            "protocol_version": "2",
            "message_id": str(uuid4()),
            "payload": {
                **identity,
                "frame_kind": "chunk",
                "chunk_index": 1,
                "entity_kind": "message",
                "records": [
                    {
                        "tombstone": False,
                        "message": {
                            "message_id": "message-1",
                            "chat_id": "chat-1",
                            "sender_platform_user_id": "fan-1",
                            "text": "Hello",
                            "sent_at": "2026-07-19T10:00:00Z",
                            "direction": "inbound",
                        },
                    }
                ],
            },
        },
    ]
    for frame in frames:
        socket.send_json(frame)
        assert socket.receive_json()["type"] == "ingest.ack"
    commit = {
        "type": "ingest.snapshot",
        "protocol_version": "2",
        "message_id": str(uuid4()),
        "payload": {**identity, "frame_kind": "commit", "chunk_count": 2},
    }
    socket.send_json(commit)
    ack = socket.receive_json()
    assert ack["type"] == "ingest.ack"
    return commit, ack


def test_agent_and_bridge_complete_role_specific_handshakes() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as agent:
        _, agent_session = agent_handshake(agent)
        assert agent_session["payload"]["resume_action"] == "snapshot_required"
        assert agent_session["payload"]["lease"] == {
            "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "lease_timeout_seconds": LEASE_TIMEOUT_SECONDS,
        }

    with client.websocket_connect("/ws/bridge") as bridge:
        _, bridge_session, initial = bridge_handshake(bridge)
        assert (
            bridge_session["payload"]["bridge_session_id"]
            == fixture("bridge.hello")["payload"]["bridge_session_id"]
        )
        assert initial[0]["payload"]["conversations"] == []
        assert initial[1]["payload"]["freshness"] == "unknown"
        assert initial[2]["payload"]["status"] == "disconnected"
        assert initial[3]["payload"]["readiness"] == "unavailable"


def test_bridge_binds_an_account_that_holds_no_agent_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installation authorizing no account still completes a Bridge bind.

    Zero authorized accounts is a state an installation activates in, so the
    four initial frames must all be produced. `agent.state` describes the
    absent configuration with a null required revision instead of failing the
    socket after `bridge.session` already reported success.
    """
    monkeypatch.setattr(
        transport_manager.config_authority, "_authorized_accounts", frozenset
    )
    monkeypatch.setattr(
        transport_manager.config_authority, "bootstrap_account_id", None
    )
    transport_manager.config_authority.repository.reset()

    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as bridge:
        _, _, initial = bridge_handshake(bridge)

    agent_state = initial[2]["payload"]
    assert agent_state["required_config_revision"] is None
    assert agent_state["applied_config_revision"] is None
    assert agent_state["status"] == "disconnected"
    assert (
        agent_state["degraded_reason"]
        == "No Agent configuration is required for this account"
    )


def test_bridge_bind_publishes_configuration_for_an_authorized_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization recorded after start still reaches the bound socket."""
    monkeypatch.setattr(
        transport_manager.config_authority,
        "_authorized_accounts",
        lambda: frozenset({DEV_ACCOUNT_ID}),
    )
    monkeypatch.setattr(
        transport_manager.config_authority, "bootstrap_account_id", None
    )
    transport_manager.config_authority.repository.reset()

    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as bridge:
        _, _, initial = bridge_handshake(bridge)

    assert initial[2]["payload"]["required_config_revision"] == REQUIRED_CONFIG_REVISION


def test_a_durable_authorization_reaches_configuration_through_the_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manager hands the configuration authority its account resolver.

    Nothing here reaches into the authority: the account is authorized only in
    the durable record, and the development bootstrap account is withdrawn, so
    the sole route from that record to a required revision is the resolver the
    manager supplies at construction. `agent.state` therefore carries a
    concrete revision instead of the null that describes an account holding no
    configuration -- a description well formed enough to be reached whether or
    not the resolver is wired, which is why the authorized case states it.
    """

    auth_database_path = tmp_path / "auth.sqlite3"
    authorize_account_durably(auth_database_path, DEV_ACCOUNT_ID)
    monkeypatch.setattr(settings, "auth_database_path", auth_database_path)
    monkeypatch.setattr(
        transport_manager.config_authority, "bootstrap_account_id", None
    )
    transport_manager.config_authority.repository.reset()

    # The durable record resolves on its own, so a null revision below can only
    # be the authority never consulting it.
    assert authorized_account_ids() == frozenset({DEV_ACCOUNT_ID})

    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as bridge:
        _, _, initial = bridge_handshake(bridge)

    agent_state = initial[2]["payload"]
    assert agent_state["required_config_revision"] == REQUIRED_CONFIG_REVISION
    assert (
        transport_manager.required_config_document(DEV_ACCOUNT_ID).config_revision
        == REQUIRED_CONFIG_REVISION
    )


def test_agent_drop_reconnects_with_new_connection_and_fence() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as first:
        _, first_session = agent_handshake(first)

    with client.websocket_connect("/ws/agent") as second:
        _, second_session = agent_handshake(second)

    assert (
        first_session["payload"]["connection_id"]
        != second_session["payload"]["connection_id"]
    )
    assert (
        first_session["payload"]["fencing_token"]
        != second_session["payload"]["fencing_token"]
    )


def test_valid_fixture_exchange_routes_ack_and_presence_end_to_end() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as bridge:
        bridge_handshake(bridge)
        with client.websocket_connect("/ws/agent") as agent:
            hello, session = agent_handshake(agent)
            connected = bridge.receive_json()
            assert connected["type"] == "agent.state"
            assert connected["payload"]["status"] == "connected"

            snapshot, snapshot_ack = commit_fixture_snapshot(agent, session, hello)
            assert snapshot_ack["correlation_id"] == snapshot["message_id"]
            assert snapshot_ack["payload"]["snapshot_id"] == snapshot["payload"]["snapshot_id"]

            delta = bind_agent_payload(fixture("ingest.delta"), session, hello)
            agent.send_json(delta)
            delta_ack = agent.receive_json()
            assert delta_ack["type"] == "ingest.ack"
            assert delta_ack["payload"]["committed_source_seq"] == 11

            observed = bind_agent_payload(fixture("presence.observed"), session, hello)
            observed["payload"]["observed_at"] = utc_now().isoformat()
            agent.send_json(observed)
            presence = bridge.receive_json()
            while presence["type"] != "presence.state":
                assert presence["type"] in {"state.snapshot", "state.delta"}
                presence = bridge.receive_json()
            assert presence["type"] == "presence.state"
            assert presence["payload"]["freshness"] == "current"
            assert presence["payload"]["online_platform_user_ids"] == ["fan-1", "fan-2"]


def test_invalid_ingest_fixture_is_rejected_without_crashing_connection() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as agent:
        hello, session = agent_handshake(agent)
        invalid = json.loads(
            (FIXTURES / "invalid" / "missing-identity.ingest.delta.json").read_text(
                encoding="utf-8"
            )
        )
        agent.send_json(invalid)
        rejected = agent.receive_json()
        assert rejected["type"] == "ingest.rejected"
        assert rejected["payload"]["code"] == "invalid_payload"
        assert rejected["payload"]["retryable"] is False

        heartbeat = bind_agent_payload(fixture("agent.heartbeat"), session, hello)
        agent.send_json(heartbeat)
        lease = transport_manager.active_agents[DEV_ACCOUNT_ID]
        assert lease.applied_config_revision == "config-8"


@pytest.mark.parametrize(
    ("hello_mutation", "expected_code"),
    [
        (lambda hello: hello["payload"].update(auth_ticket="wrong"), "unauthorized"),
        (lambda hello: hello.update(protocol_version="99"), "unsupported_version"),
        (lambda hello: hello["payload"].pop("agent_installation_id"), "validation_failed"),
    ],
)
def test_invalid_agent_hellos_receive_fatal_error_and_close(hello_mutation, expected_code) -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as socket:
        hello = fixture("agent.hello")
        hello_mutation(hello)
        socket.send_json(hello)
        error = socket.receive_json()
        assert error["type"] == "protocol.error"
        assert error["payload"]["code"] == expected_code
        assert error["payload"]["fatal"] is True
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_wrong_role_pre_handshake_and_bridge_invalid_fixture_follow_error_matrix() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as socket:
        socket.send_json(fixture("bridge.hello"))
        error = socket.receive_json()
        assert error["payload"]["code"] == "wrong_role"
        assert error["payload"]["fatal"] is True

    with client.websocket_connect("/ws/bridge") as socket:
        invalid = json.loads(
            (FIXTURES / "invalid" / "unknown-extra.bridge.hello.json").read_text(
                encoding="utf-8"
            )
        )
        socket.send_json(invalid)
        error = socket.receive_json()
        assert error["payload"]["code"] == "validation_failed"
        assert error["payload"]["fatal"] is True

    with client.websocket_connect("/ws/agent") as socket:
        heartbeat = fixture("agent.heartbeat")
        socket.send_json(heartbeat)
        error = socket.receive_json()
        assert error["payload"]["code"] == "pre_handshake"


def test_new_agent_connection_fences_old_writer() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as first:
        first_hello, first_session = agent_handshake(first)
        with client.websocket_connect("/ws/agent") as second:
            agent_handshake(second)
            stale_snapshot = bind_agent_payload(
                fixture("ingest.snapshot"), first_session, first_hello
            )
            first.send_json(stale_snapshot)
            rejected = first.receive_json()
            assert rejected["type"] == "ingest.rejected"
            assert rejected["payload"]["code"] == "stale_fence"


def test_lease_and_presence_expiry_derive_stale_disconnected_and_unknown() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/agent") as agent:
        hello, session = agent_handshake(agent)
        lease = transport_manager.active_agents[DEV_ACCOUNT_ID]
        observed = bind_agent_payload(fixture("presence.observed"), session, hello)
        observed["payload"]["online_platform_user_ids"] = []
        observed["payload"]["observed_at"] = utc_now().isoformat()
        agent.send_json(observed)
        agent.send_json(observed)
        assert agent.receive_json()["type"] == "protocol.error"

        record = transport_manager.presence[DEV_ACCOUNT_ID]
        assert transport_manager.presence_state_payload(DEV_ACCOUNT_ID)["freshness"] == "current"
        assert transport_manager.presence_state_payload(DEV_ACCOUNT_ID)["online_platform_user_ids"] == []
        asyncio.run(transport_manager.expire(lease.last_heartbeat_at + timedelta(seconds=60)))
        assert transport_manager.agent_state_payload(DEV_ACCOUNT_ID)["status"] == "stale"
        asyncio.run(transport_manager.expire(lease.last_heartbeat_at + timedelta(seconds=120)))
        assert transport_manager.agent_state_payload(DEV_ACCOUNT_ID)["status"] == "disconnected"
        assert DEV_ACCOUNT_ID not in transport_manager.active_agents
        assert lease.connection_id not in transport_manager.agent_connections

        asyncio.run(transport_manager.expire(record.expires_at))
        presence = transport_manager.presence_state_payload(DEV_ACCOUNT_ID)
        assert presence["freshness"] == "unknown"
        assert presence["online_platform_user_ids"] == []
        assert presence["last_observation"] is not None


class RecordingWebSocket:
    def __init__(self, name: str, events: list[tuple]) -> None:
        self.name = name
        self.events = events

    async def send_text(self, text: str) -> None:
        document = json.loads(text)
        self.events.append((self.name, "send", document))

    async def close(self, code: int, reason: str) -> None:
        self.events.append((self.name, "close", code, reason))


@pytest.mark.asyncio
async def test_hard_lease_expiry_closes_then_broadcasts_disconnected() -> None:
    manager = InMemoryTransportManager()
    events: list[tuple] = []
    bridge = RecordingWebSocket("bridge", events)
    await manager.bind_bridge(
        bridge,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        bridge_session_id=uuid4(),
    )
    agent = RecordingWebSocket("agent", events)
    lease = await manager.bind_agent(
        agent,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=REQUIRED_CONFIG_REVISION,
    )

    events.clear()
    await manager.expire(
        lease.last_heartbeat_at + timedelta(seconds=lease.lease_timeout_seconds)
    )
    assert events[-1][2]["type"] == "agent.state"
    assert events[-1][2]["payload"]["status"] == "stale"

    events.clear()
    await manager.expire(
        lease.last_heartbeat_at + timedelta(seconds=lease.lease_timeout_seconds * 2)
    )
    assert DEV_ACCOUNT_ID not in manager.active_agents
    assert lease.connection_id not in manager.agent_connections
    assert events[0] == (
        "agent",
        "close",
        LEASE_EXPIRED_CLOSE_CODE,
        "Agent heartbeat lease expired",
    )
    assert events[1][0:2] == ("bridge", "send")
    assert events[1][2]["type"] == "agent.state"
    assert events[1][2]["payload"]["status"] == "disconnected"


@pytest.mark.asyncio
async def test_hard_expiry_does_not_retire_a_lease_refreshed_while_waiting() -> None:
    manager = InMemoryTransportManager()
    events: list[tuple] = []
    agent = RecordingWebSocket("agent", events)
    lease = await manager.bind_agent(
        agent,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=REQUIRED_CONFIG_REVISION,
    )
    expiry_time = lease.last_heartbeat_at + timedelta(
        seconds=lease.lease_timeout_seconds * 2
    )

    await manager._agent_command_lock.acquire()
    expiry = asyncio.create_task(manager.expire(expiry_time))
    await asyncio.sleep(0)
    await manager.heartbeat(
        lease,
        REQUIRED_CONFIG_REVISION,
        now=expiry_time - timedelta(seconds=1),
    )
    manager._agent_command_lock.release()
    await expiry

    assert manager.active_agents[DEV_ACCOUNT_ID] is lease
    assert manager.agent_connections[lease.connection_id] is lease
    assert lease.status == "connected"
    assert not any(event[1] == "close" for event in events)


@pytest.mark.asyncio
async def test_expiry_sweeper_does_not_resurrect_a_disconnected_socket() -> None:
    manager = InMemoryTransportManager()
    events: list[tuple] = []
    agent = RecordingWebSocket("agent", events)
    lease = await manager.bind_agent(
        agent,
        principal_id="principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=REQUIRED_CONFIG_REVISION,
    )

    await manager.disconnect_agent(lease.connection_id)
    await manager.expire(lease.last_heartbeat_at + timedelta(seconds=1))

    assert manager.active_agents[DEV_ACCOUNT_ID] is lease
    assert lease.status == "disconnected"
    assert manager.agent_state_payload(DEV_ACCOUNT_ID)["status"] == "disconnected"


def test_agent_lease_timing_defaults_are_configurable() -> None:
    assert Settings.model_fields["agent_heartbeat_interval_seconds"].default == 20
    assert Settings.model_fields["agent_lease_timeout_seconds"].default == 60
    custom = Settings(
        agent_heartbeat_interval_seconds=7,
        agent_lease_timeout_seconds=21,
    )
    assert custom.agent_heartbeat_interval_seconds == 7
    assert custom.agent_lease_timeout_seconds == 21


@pytest.mark.parametrize(
    ("heartbeat_interval", "lease_timeout"),
    [(21, 21), (22, 21)],
)
def test_agent_lease_timing_rejects_heartbeat_at_or_after_timeout(
    heartbeat_interval: int,
    lease_timeout: int,
) -> None:
    with pytest.raises(
        ValidationError,
        match="Agent heartbeat interval must be less than lease timeout",
    ):
        Settings(
            agent_heartbeat_interval_seconds=heartbeat_interval,
            agent_lease_timeout_seconds=lease_timeout,
        )


def test_bridge_resync_returns_correlated_snapshot() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws/bridge") as bridge:
        hello, session, _ = bridge_handshake(bridge)
        resync = fixture("state.resync")
        resync["payload"].update(
            connection_id=session["payload"]["connection_id"],
            bridge_session_id=hello["payload"]["bridge_session_id"],
            creator_account_id=DEV_ACCOUNT_ID,
        )
        bridge.send_json(resync)
        snapshot = bridge.receive_json()
        assert snapshot["type"] == "state.snapshot"
        assert snapshot["correlation_id"] == resync["message_id"]


def test_agent_config_transport_validates_stub_auth_and_etag() -> None:
    client = TestClient(app)
    params = {
        "agent_installation_id": str(uuid4()),
        "creator_account_id": DEV_ACCOUNT_ID,
        "supported_config_schema_versions": "2",
    }
    auth = {"Authorization": f"Bearer {DEV_AGENT_AUTH_TICKET}"}
    response = client.get("/api/v1/agent/config", params=params, headers=auth)
    assert response.status_code == 200
    assert response.json()["operation"] == "agent.config.document"
    assert response.headers["etag"] == REQUIRED_CONFIG_REVISION

    not_modified = client.get(
        "/api/v1/agent/config",
        params={**params, "current_etag": REQUIRED_CONFIG_REVISION},
        headers=auth,
    )
    assert not_modified.status_code == 304

    unauthorized = client.get(
        "/api/v1/agent/config", params=params, headers={"Authorization": "Bearer wrong"}
    )
    assert unauthorized.status_code == 401


def test_development_stub_fails_closed_for_production_or_non_local_exposure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(RuntimeError):
        transport_manager.validate_auth_configuration()
    with pytest.raises(AuthenticationError):
        transport_manager.authenticate(
            DEV_AGENT_AUTH_TICKET, DEV_ACCOUNT_ID, role="agent"
        )

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "websocket_bind_host", "0.0.0.0")
    with pytest.raises(RuntimeError):
        transport_manager.validate_auth_configuration()
