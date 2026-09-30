"""Production native-Snow route exercised by the independent noiseprotocol peer."""

import base64
import ast
import asyncio
import hashlib
import inspect
import json
import textwrap
import threading
import time
from typing import get_args
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from noise.connection import Keypair, NoiseConnection
from starlette.websockets import WebSocketDisconnect

from app.api.endpoints import companion_pairing, companion_session, transport_ws
from app.api.security import csrf_token
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.persistence.factory import create_canonical_repositories
from app.security.companion_pairing_proof import session_prologue
from app.security.companion_session_authority import CompanionSessionAuthority
from app.security.companion_session_authority import CompanionSessionError
from app.transport import companion_channel
from app.transport.companion_records import Assembly, CompanionRecordError, document, fragments
from app.transport.manager import InMemoryTransportManager
from app.protocol.payloads import AgentCapability
from test_companion_session_authority import admitted, _sign
from test_companion_pairing_service import local
from test_companion_pairing_proof import contract, grant_references

EXTENSION_ID = "a" * 32
ORIGIN = f"chrome-extension://{EXTENSION_ID}"


@pytest.fixture
def route(admitted, monkeypatch, tmp_path):
    local, authority, snapshot, _ = admitted
    monkeypatch.setattr(companion_pairing, "require_activated_runtime", lambda: None)
    monkeypatch.setattr(companion_pairing.settings, "extension_id", EXTENSION_ID)
    monkeypatch.setattr(companion_pairing.settings, "websocket_bind_host", "127.0.0.1")
    monkeypatch.setattr(
        companion_pairing.settings, "auth_database_path", local.store.database.path
    )
    monkeypatch.setattr(companion_session, "session_authority", lambda: authority)
    monkeypatch.setattr(
        companion_channel,
        "time",
        SimpleNamespace(
            time=lambda: local.clock.now.timestamp(),
            monotonic=time.monotonic,
        ),
    )
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=tmp_path / "canonical.sqlite3",
        projection_path=tmp_path / "projection.sqlite3",
    )
    manager = InMemoryTransportManager(repositories)
    manager.config_authority._authorized_accounts = lambda: frozenset({local.account})
    monkeypatch.setattr(companion_session, "transport_manager", manager)
    monkeypatch.setattr(transport_ws, "transport_manager", manager)
    app = FastAPI()
    app.add_api_websocket_route("/ws/agent", companion_session.companion_session_socket)
    app.include_router(companion_pairing.router)
    monkeypatch.setattr(
        companion_pairing, "get_runtime_policy", lambda request: local.policy
    )
    monkeypatch.setattr(
        companion_pairing, "companion_pairing_service", lambda: local.service
    )
    monkeypatch.setattr(
        companion_pairing.settings, "bridge_origin", "http://127.0.0.1:17871"
    )
    return SimpleNamespace(
        app=app, local=local, authority=authority, snapshot=snapshot, manager=manager
    )


def _client(route):
    return TestClient(
        route.app, base_url="http://127.0.0.1:17871", client=("127.0.0.1", 50000)
    )


def _oracle(route, *, wrong_pin=False):
    label = route.local.contract["vector"]["fixture_labels"]["agent_noise_key"]
    prefix = route.local.contract["profile"]["test_fixture_derivation"]["label_prefix"]
    private = hashlib.sha256((prefix + label).encode()).digest()
    oracle = NoiseConnection.from_name(b"Noise_KK_25519_ChaChaPoly_SHA256")
    oracle.set_as_initiator()
    oracle.set_keypair_from_private_bytes(Keypair.STATIC, private)
    oracle.set_keypair_from_public_bytes(
        Keypair.REMOTE_STATIC,
        b"q" * 32 if wrong_pin else route.snapshot.pin.brain_noise_public_key,
    )
    oracle.set_prologue(session_prologue(route.snapshot.pin.pairing_digest))
    oracle.start_handshake()
    return oracle


def _establish(socket, route):
    oracle = _oracle(route)
    socket.send_bytes(route.snapshot.pin.pairing_id + bytes(oracle.write_message(b"")))
    assert bytes(oracle.read_message(socket.receive_bytes())) == b""
    socket.send_bytes(bytes(oracle.encrypt(b"\x00client-ready")))
    assert bytes(oracle.decrypt(socket.receive_bytes())) == b"\x00server-ready"
    authorization = bytes(oracle.decrypt(socket.receive_bytes()))
    assert authorization[0] == 1
    value = document(authorization[1:])
    assert value["type"] == "session.authorization"
    # Hash comparisons avoid printing retained grant bytes if this fails.
    for kind in ("installation_grant", "creator_account_binding"):
        if (
            hashlib.sha256(value[kind].encode()).digest()
            != hashlib.sha256(route.snapshot.authorization[kind].encode()).digest()
        ):
            pytest.fail("encrypted authorization grant mismatch", pytrace=False)
    return oracle


def _send(socket, oracle, value):
    for part in fragments(value):
        socket.send_bytes(bytes(oracle.encrypt(b"\x01" + part)))


def _receive(socket, oracle):
    assembly = Assembly()
    while True:
        raw = bytes(oracle.decrypt(socket.receive_bytes()))
        assert raw[0] == 1
        value = assembly.feed(raw[1:])
        if value is not None:
            return value


def _rpc(socket, oracle, method, params):
    identifier = str(uuid4())
    _send(
        socket,
        oracle,
        {"type": "rpc.request", "id": identifier, "method": method, "params": params},
    )
    for _ in range(16):
        result = _receive(socket, oracle)
        if result["type"] == "rpc.response":
            break
    else:
        pytest.fail("protected RPC response was not delivered", pytrace=False)
    assert result["type"] == "rpc.response" and result["id"] == identifier
    if "error" in result:
        pytest.fail(f"protected RPC {method} was refused", pytrace=False)
    return result["result"]


def _prove(socket, oracle, route):
    challenge = _rpc(socket, oracle, "agent.challenge", {})
    return _rpc(
        socket,
        oracle,
        "agent.authenticate",
        {
            "challenge_id": challenge["challenge_id"],
            "signature": _sign(route.local, route.snapshot, challenge),
        },
    )


def _refused_rpc(socket, oracle, method, params):
    identifier = str(uuid4())
    _send(
        socket,
        oracle,
        {
            "type": "rpc.request",
            "id": identifier,
            "method": method,
            "params": params,
        },
    )
    response = _receive(socket, oracle)
    # Never print a provider response if a secret were accidentally exported.
    if response != {
        "type": "rpc.response",
        "id": identifier,
        "error": "session_request_refused",
    }:
        pytest.fail("protected RPC refusal was not payload-free", pytrace=False)
    with pytest.raises(WebSocketDisconnect):
        socket.receive_bytes()


@pytest.mark.parametrize(
    "method", ["agent.storage.unseal", "agent.storage.rotate", "agent.config.get"]
)
def test_no_runtime_rpc_before_agent_identity_proof(route, method):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _refused_rpc(socket, oracle, method, {})


def test_storage_reconstruction_requires_fresh_proof_and_returns_current_ticket(route):
    with _client(route) as client:
        with client.websocket_connect(
            "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
        ) as first:
            oracle = _establish(first, route)
            first_result = _prove(first, oracle, route)
            previous = first_result["storage_bootstrap"]
            first_unlock = _rpc(
                first, oracle, "agent.storage.unseal", {"storage_bootstrap": previous}
            )
        with client.websocket_connect(
            "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
        ) as second:
            oracle = _establish(second, route)
            current = _prove(second, oracle, route)
            unlocked = _rpc(
                second, oracle, "agent.storage.unseal", {"storage_bootstrap": previous}
            )
            if (
                unlocked["auth_ticket"] != current["auth_ticket"]
                or unlocked["auth_ticket"] == first_result["auth_ticket"]
            ):
                pytest.fail(
                    "reconstruction did not bind the current session ticket",
                    pytrace=False,
                )
            if (
                hashlib.sha256(unlocked["storage_key_base64"].encode()).digest()
                != hashlib.sha256(first_unlock["storage_key_base64"].encode()).digest()
            ):
                pytest.fail(
                    "same-account storage key changed on reconstruction", pytrace=False
                )


def test_storage_bootstrap_for_another_account_is_refused(route):
    from app.security.extension_storage import seal_extension_storage_bootstrap

    sealed = seal_extension_storage_bootstrap(
        extension_id=EXTENSION_ID,
        creator_account_id="another-account",
        credential_kind="pairing",
        auth_ticket="untrusted-fixture-ticket",
    )
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _prove(socket, oracle, route)
        _refused_rpc(
            socket, oracle, "agent.storage.unseal", {"storage_bootstrap": sealed}
        )


def test_production_route_native_noise_identity_proof_and_storage_rpc(route):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        challenge = _rpc(socket, oracle, "agent.challenge", {})
        authenticated = _rpc(
            socket,
            oracle,
            "agent.authenticate",
            {
                "challenge_id": challenge["challenge_id"],
                "signature": _sign(route.local, route.snapshot, challenge),
            },
        )
        unsealed = _rpc(
            socket,
            oracle,
            "agent.storage.unseal",
            {"storage_bootstrap": authenticated["storage_bootstrap"]},
        )
        assert unsealed["creator_account_id"] == route.local.account
        assert len(base64.b64decode(unsealed["storage_key_base64"])) == 32
        _send(
            socket,
            oracle,
            {
                "type": "agent.hello",
                "protocol_version": "2",
                "message_id": str(uuid4()),
                "payload": {
                    "auth_ticket": authenticated["auth_ticket"],
                    "agent_installation_id": route.snapshot.pin.agent_installation_id,
                    "requested_creator_account_id": route.local.account,
                    "capabilities": ["capture.chats"],
                    "extension_version": "2.0.1",
                    "agent_stream_id": str(uuid4()),
                    "last_acknowledged_source_seq": 0,
                    "applied_config_revision": None,
                },
            },
        )
        session = _receive(socket, oracle)
        assert session["type"] == "agent.session"
        config = _rpc(
            socket,
            oracle,
            "agent.config.get",
            {
                "operation": "agent.config.get",
                "protocol_version": "2",
                "auth_ticket": session["payload"]["config_auth_ticket"],
                "agent_installation_id": route.snapshot.pin.agent_installation_id,
                "creator_account_id": route.local.account,
                "current_etag": None,
                "current_config_revision": None,
                "supported_config_schema_versions": ["2"],
            },
        )
        assert config["status"] == 200
        conditional = _rpc(
            socket,
            oracle,
            "agent.config.get",
            {
                "operation": "agent.config.get",
                "protocol_version": "2",
                "auth_ticket": session["payload"]["config_auth_ticket"],
                "agent_installation_id": route.snapshot.pin.agent_installation_id,
                "creator_account_id": route.local.account,
                "current_etag": f'W/"{config["etag"]}"',
                "current_config_revision": config["document"]["config_revision"],
                "supported_config_schema_versions": ["2"],
            },
        )
        assert conditional == {"status": 304, "etag": config["etag"], "document": None}
        rotation = _rpc(
            socket,
            oracle,
            "agent.storage.rotate",
            {
                "protocol_version": "2",
                "creator_account_id": route.local.account,
                "agent_installation_id": route.snapshot.pin.agent_installation_id,
                "reconnect_auth_ticket": session["payload"]["reconnect_auth_ticket"],
                "config_auth_ticket": session["payload"]["config_auth_ticket"],
                "storage_bootstrap": authenticated["storage_bootstrap"],
            },
        )
        assert rotation["schema"] == "ofca-extension-storage-rotation/v1"
        assert isinstance(rotation["storage_bootstrap"], str)


def test_bridge_lists_safe_pin_projection_and_revokes_live_noise_session(route):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        _establish(socket, route)
        response = client.get("/api/v1/companion/pins")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        pins = response.json()["pins"]
        assert len(pins) == 1
        assert set(pins[0]) == {
            "pairing_id",
            "creator_account_id",
            "generation",
            "version",
            "state",
            "expires_at",
            "comparison_code",
            "agent_identity_thumbprint",
        }
        assert pins[0]["state"] == "admitted"
        identifier = pins[0]["pairing_id"]
        headers = {
            "Origin": "http://127.0.0.1:17871",
            "X-CSRF-Token": csrf_token(route.local.policy),
        }
        refused = client.post(
            f"/api/v1/companion/pins/{identifier}/revoke",
            json={"version": pins[0]["version"]},
        )
        assert refused.status_code == 403
        assert len(route.local.service.pins(route.local.policy)) == 1
        revoked = client.post(
            f"/api/v1/companion/pins/{identifier}/revoke",
            json={"version": pins[0]["version"]},
            headers=headers,
        )
        assert revoked.status_code == 200
        assert revoked.json()["state"] == "revoked"
        assert client.get("/api/v1/companion/pins").json() == {"pins": []}
        with pytest.raises(CompanionSessionError):
            route.authority.prepare(route.snapshot.pin.pairing_id)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()
        assert closed.value.code == 1008


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/agent/config", "get"),
        ("/api/v1/agent/storage/unseal", "post"),
        ("/api/v1/agent/storage/rotate", "post"),
    ],
)
def test_application_has_no_plaintext_agent_http_routes(path, method):
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1:17871") as client:
        response = getattr(client, method)(path)
        assert response.status_code == 404


@pytest.mark.parametrize(
    "mode", ["unknown-pin", "wrong-noise-pin", "early-data", "plaintext"]
)
def test_production_route_refuses_untrusted_handshakes_without_application_response(
    route, mode
):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _oracle(route, wrong_pin=mode == "wrong-noise-pin")
        if mode == "plaintext":
            socket.send_text('{"type":"agent.hello"}')
        else:
            identifier = (
                b"z" * 32 if mode == "unknown-pin" else route.snapshot.pin.pairing_id
            )
            socket.send_bytes(
                identifier
                + bytes(
                    oracle.write_message(b"unexpected" if mode == "early-data" else b"")
                )
            )
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_bytes()
        assert refused.value.code == 1008


@pytest.mark.parametrize(
    "mode", ["tamper", "oversize", "plaintext-after-handshake", "replay"]
)
def test_established_route_refuses_modified_or_invalid_frames(route, mode):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        frame = bytes(
            oracle.encrypt(
                b"\x01"
                + next(
                    fragments(
                        {
                            "type": "rpc.request",
                            "id": str(uuid4()),
                            "method": "agent.challenge",
                            "params": {},
                        }
                    )
                )
            )
        )
        if mode == "tamper":
            socket.send_bytes(frame[:-1] + bytes([frame[-1] ^ 1]))
        elif mode == "oversize":
            socket.send_bytes(b"x" * 4_097)
        elif mode == "plaintext-after-handshake":
            socket.send_text('{"type":"rpc.request"}')
        else:
            socket.send_bytes(frame)
            assert _receive(socket, oracle)["type"] == "rpc.response"
            socket.send_bytes(frame)
        with pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_bytes()
        assert refused.value.code == 1008


@pytest.mark.parametrize("expiry", [False, True], ids=["revocation", "expiry"])
def test_idle_protected_socket_closes_when_authority_ends(route, expiry):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        _establish(socket, route)
        if expiry:
            route.local.clock.now = route.snapshot.expires_at
        else:
            route.local.store.revoke(
                RevocationKey(
                    RevocationScopeType.AGENT_PAIRING,
                    base64.urlsafe_b64encode(route.snapshot.pin.pairing_id)
                    .rstrip(b"=")
                    .decode(),
                )
            )
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()


def test_plain_origin_is_refused_before_noise(route):
    with _client(route) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/agent", headers={"Origin": "http://127.0.0.1:17871"}
            ):
                pytest.fail("untrusted web origin admitted")


@pytest.mark.asyncio
async def test_revocation_after_confirmation_prevents_grant_record(route):
    oracle = _oracle(route)
    received = []
    closed = []
    first = route.snapshot.pin.pairing_id + bytes(oracle.write_message(b""))

    class Socket:
        client = SimpleNamespace(host="127.0.0.1")
        headers = {"host": "127.0.0.1:17871", "origin": ORIGIN}
        query_params = {}

        async def accept(self):
            pass

        async def receive_bytes(self):
            if not received:
                return first
            return bytes(oracle.encrypt(b"\x00client-ready"))

        async def send_bytes(self, frame):
            if not received:
                assert bytes(oracle.read_message(frame)) == b""
            elif len(received) == 1:
                assert bytes(oracle.decrypt(frame)) == b"\x00server-ready"
                route.local.store.revoke(
                    RevocationKey(
                        RevocationScopeType.AGENT_PAIRING,
                        base64.urlsafe_b64encode(route.snapshot.pin.pairing_id)
                        .rstrip(b"=")
                        .decode(),
                    )
                )
            received.append(True)

        async def close(self, code, reason):
            closed.append(code)

    await companion_session.companion_session_socket(Socket())
    assert len(received) == 2  # Only handshake and fixed confirmation; no grant record.
    assert closed == [1008]


@pytest.mark.asyncio
async def test_cancelled_initializer_closes_late_native_noise_result(route):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    values = []

    def delayed():
        entered.set()
        if not release.wait(3):
            raise RuntimeError("test initializer release timed out")
        value = companion_session.create_noise_responder(route.snapshot.pin)
        values.append(value)
        returned.set()
        return value

    task = asyncio.create_task(companion_session._owned(delayed))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(returned.wait, 1)
    for _ in range(100):
        if values[0].closed():
            break
        await asyncio.sleep(0.005)
    assert values[0].closed()


@pytest.mark.asyncio
async def test_cancelled_initializer_closes_late_authority_handle(route):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Handle:
        def close(self):
            closed.set()

    def delayed():
        entered.set()
        if not release.wait(3):
            raise RuntimeError("test initializer release timed out")
        return Handle()

    task = asyncio.create_task(companion_session._owned(delayed))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(closed.wait, 1)


@pytest.mark.asyncio
async def test_revocation_between_document_fragments_prevents_next_encryption(route):
    authority = route.authority.authorize(route.snapshot)
    encrypted, sent = [], []

    class Cipher:
        def encrypt_transport(self, part):
            encrypted.append(len(part))
            return b"opaque-fixture-ciphertext"

    class Socket:
        async def send_bytes(self, value):
            sent.append(value)
            route.local.store.revoke(
                RevocationKey(
                    RevocationScopeType.CREATOR_ACCOUNT,
                    route.local.account,
                )
            )

    channel = companion_channel.CompanionChannel(
        Socket(),
        Cipher(),
        authority,
        route.snapshot.expires_at,
    )
    with pytest.raises(CompanionSessionError):
        await channel.send({"data": "x" * 8_000})
    assert len(encrypted) == len(sent) == 1
    assert channel.pending_sends == 0


def _protected_socket(route):
    authority = route.authority.authorize(route.snapshot)
    return SimpleNamespace(authorization_guard=authority.operation)


async def _bind_protected_lease(route, socket):
    return await route.manager.bind_agent(
        socket,
        principal_id=f"agent:{route.snapshot.pin.agent_installation_id}",
        creator_account_id=route.local.account,
        agent_installation_id=UUID(route.snapshot.pin.agent_installation_id),
        agent_stream_id=uuid4(),
        applied_config_revision=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["heartbeat", "presence"])
async def test_current_frame_does_not_allow_post_revocation_state_write(
    route, operation, monkeypatch
):
    lease = await _bind_protected_lease(route, _protected_socket(route))
    prior_heartbeat = lease.last_heartbeat_at
    entered = []
    monkeypatch.setattr(
        route.manager.config_authority,
        "record_echo",
        lambda *args: entered.append(True),
    )
    route.local.store.revoke(
        RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, route.local.account)
    )
    with pytest.raises(CompanionSessionError):
        if operation == "heartbeat":
            await route.manager.heartbeat(lease, None)
        else:
            await route.manager.observe_presence(
                lease,
                observation_id=1,
                observed_at=route.local.clock.now,
                online_platform_user_ids=["fixture-user"],
                now=route.local.clock.now,
            )
    assert not entered
    assert lease.last_heartbeat_at == prior_heartbeat
    assert route.local.account not in route.manager.presence


@pytest.mark.asyncio
async def test_waiting_lease_admission_rechecks_authority_after_command_lock(route):
    lock = route.manager._agent_command_lock
    await lock.acquire()
    task = asyncio.create_task(_bind_protected_lease(route, _protected_socket(route)))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        route.local.store.revoke(
            RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, route.local.account)
        )
    finally:
        lock.release()
    with pytest.raises(CompanionSessionError):
        await task
    assert not route.manager.active_agents
    assert not route.manager.agent_connections


FUTURE_CAPABILITY = "future.compatibility.v1"
FUTURE_METHOD = "future.compatibility.get"


@pytest.fixture
def forward_logs(caplog):
    caplog.set_level("DEBUG")
    yield
    assert FUTURE_CAPABILITY not in caplog.text
    assert FUTURE_METHOD not in caplog.text


def _forward_hello(socket, oracle, route, capabilities, **extra):
    authenticated = _prove(socket, oracle, route)
    _send(socket, oracle, {
        "type": "agent.hello",
        "protocol_version": "2",
        "message_id": str(uuid4()),
        "payload": {
            "auth_ticket": authenticated["auth_ticket"],
            "agent_installation_id": route.snapshot.pin.agent_installation_id,
            "requested_creator_account_id": route.local.account,
            "capabilities": capabilities,
            "extension_version": "2.0.5",
            "agent_stream_id": str(uuid4()),
            "last_acknowledged_source_seq": 0,
            "applied_config_revision": None,
            **extra,
        },
    })
    return _receive(socket, oracle)


@pytest.mark.parametrize("catchup", [False, True])
def test_unknown_capability_admitted_without_negotiation(
    route, monkeypatch, forward_logs, catchup,
):
    negotiated = []
    bind = route.manager.bind_agent

    async def record_bind(*args, **kwargs):
        negotiated.append(tuple(kwargs["capabilities"]))
        return await bind(*args, **kwargs)

    monkeypatch.setattr(route.manager, "bind_agent", record_bind)
    known = [value for value in get_args(AgentCapability)
             if catchup or value != "history.catchup.v1"]
    known.append("capture.chats")
    with _client(route) as client:
        for capabilities in (known, [*known, FUTURE_CAPABILITY]):
            with client.websocket_connect(
                "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
            ) as socket:
                oracle = _establish(socket, route)
                session = _forward_hello(socket, oracle, route, capabilities)
                assert session["type"] == "agent.session"
                assert FUTURE_CAPABILITY not in json.dumps(session)
                sync = _receive(socket, oracle)
                assert sync["type"] == "sync.required"
                assert FUTURE_CAPABILITY not in json.dumps(sync)
                assert route.manager.catchup.supported(route.local.account) is catchup
    assert negotiated == [tuple(known), tuple(known)]


async def _rejected_forward_hello(route, capabilities, **extra):
    responses, closed = [], []

    class Socket:
        async def accept(self):
            pass

        async def receive_text(self):
            return json.dumps({
                "type": "agent.hello", "protocol_version": "2", "message_id": str(uuid4()),
                "payload": {
                    "auth_ticket": "invalid-ticket",
                    "agent_installation_id": route.snapshot.pin.agent_installation_id,
                    "requested_creator_account_id": route.local.account,
                    "capabilities": capabilities, "extension_version": "2.0.5",
                    "agent_stream_id": str(uuid4()), "last_acknowledged_source_seq": 0,
                    "applied_config_revision": None,
                    **extra,
                },
            })

        async def send_text(self, raw):
            responses.append(json.loads(raw))

        async def close(self, code, reason):
            closed.append(code)

    await transport_ws._agent_socket(Socket())
    assert len(responses) == 1
    return responses[0], closed


@pytest.mark.parametrize("capabilities", [
    ["capture.chats", "Future.compatibility.v1"],
    ["capture.chats", "future compatibility.v1"],
    ["capture.chats", "f." + "a" * 63],
    ["capture.chats", 42],
    ["capture.chats"] * 33,
    [FUTURE_CAPABILITY],
    ["capture.chats", "future..v1"],
    ["capture.chats", ".future.v1"],
    ["capture.chats", "future.v1."],
    ["capture.chats", "future.v1\n"],
    ["capture.chats", "future"],
    ["capture.chats", "future.v_1"],
    [],
], ids=["uppercase", "space", "long", "non-string", "too-many", "unknown-only",
        "empty-word", "leading-dot", "trailing-dot", "newline", "undotted", "underscore", "empty"])
@pytest.mark.asyncio
async def test_malformed_capability_rejects_hello(route, forward_logs, capabilities, caplog):
    response, closed = await _rejected_forward_hello(route, capabilities)
    assert response["type"] == "protocol.error"
    assert response["payload"]["code"] == "validation_failed"
    assert response["payload"]["fatal"] is True
    for token in capabilities:
        if isinstance(token, str) and token not in get_args(AgentCapability):
            assert token not in json.dumps(response)
            assert token not in caplog.text
    assert closed == [1002]
    assert not route.manager.agent_connections


@pytest.mark.asyncio
async def test_hello_fields_remain_closed(route, forward_logs):
    response, closed = await _rejected_forward_hello(route, ["capture.chats"], future=True)
    assert response["type"] == "protocol.error"
    assert response["payload"]["code"] == "validation_failed"
    assert closed == [1002]


def test_capability_token_and_count_boundaries(route, forward_logs):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        response = _forward_hello(socket, oracle, route, ["capture.chats"] * 31 + ["f." + "a" * 62])
        assert response["type"] == "agent.session"


def _forward_request(socket, oracle, method, identifier=None, **extra):
    identifier = identifier or str(uuid4())
    _send(socket, oracle, {
        "type": "rpc.request", "id": identifier, "method": method, "params": {}, **extra,
    })
    return identifier


def test_unknown_method_keeps_same_channel(route, forward_logs):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        session = _forward_hello(socket, oracle, route, ["capture.chats"])
        assert session["type"] == "agent.session"
        assert _receive(socket, oracle)["type"] == "sync.required"
        identifier = _forward_request(socket, oracle, FUTURE_METHOD)
        assert _receive(socket, oracle) == {
            "type": "rpc.response", "id": identifier, "error": "unknown_method",
        }
        result = _rpc(socket, oracle, "agent.config.get", {
            "operation": "agent.config.get", "protocol_version": "2",
            "auth_ticket": session["payload"]["config_auth_ticket"],
            "agent_installation_id": route.snapshot.pin.agent_installation_id,
            "creator_account_id": route.local.account,
            "current_etag": None, "current_config_revision": None,
            "supported_config_schema_versions": ["2"],
        })
        assert result["status"] == 200


def test_surface_report_reaches_session_dispatch(route):
    report = {
        "schema": "ofca-browser-surface/v1",
        "capture": "active",
        "site_access": "granted",
        "history_permission": "granted",
        "legal_review_required": False,
    }
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _prove(socket, oracle, route)
        assert _rpc(socket, oracle, "agent.surface.report", report) == {}
        surface = route.manager.browser_surface(route.local.account)
        assert surface is not None
        assert {key: surface[key] for key in report if key != "schema"} == {
            key: value for key, value in report.items() if key != "schema"
        }
    assert route.manager.browser_surface(route.local.account) is None


def test_served_methods_cover_dispatch_handlers():
    handled = set()
    for handler in (companion_session.SessionRPC.call, companion_session._serve):
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            selector = node.left
            is_method = isinstance(selector, ast.Name) and selector.id == "method"
            is_request_method = (
                isinstance(selector, ast.Subscript)
                and isinstance(selector.value, ast.Name)
                and selector.value.id == "value"
                and isinstance(selector.slice, ast.Constant)
                and selector.slice.value == "method"
            )
            if not (is_method or is_request_method):
                continue
            if isinstance(node.ops[0], ast.Eq):
                literals = [node.comparators[0]]
            elif isinstance(node.ops[0], ast.In):
                literals = node.comparators[0].elts
            else:
                continue
            for literal in literals:
                assert isinstance(literal, ast.Constant)
                assert isinstance(literal.value, str)
                handled.add(literal.value)
    assert handled
    assert companion_session.SessionRPC.METHODS == handled


@pytest.mark.parametrize("failure", ["known", "timeout", "non-string"])
def test_rpc_failures_still_refuse_and_close(route, monkeypatch, forward_logs, failure):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _prove(socket, oracle, route)
        if failure == "timeout":
            timeout = asyncio.timeout
            deadlines = []
            expired = []

            def expire_dispatch(delay):
                if not deadlines and asyncio.current_task().get_coro().__name__ == "dispatch":
                    deadlines.append(delay)
                    deadline = timeout(0)
                    expired.append(deadline)
                    return deadline
                return timeout(delay)

            monkeypatch.setattr(companion_session.asyncio, "timeout", expire_dispatch)
        method = 42 if failure == "non-string" else "agent.config.get"
        identifier = _forward_request(socket, oracle, method)
        assert _receive(socket, oracle) == {
            "type": "rpc.response", "id": identifier, "error": "session_request_refused",
        }
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_bytes()
        assert closed.value.code == 1008
        if failure == "timeout":
            assert deadlines == [10]
            assert expired[0].expired()


def test_unknown_method_id_cannot_be_repeated(route, forward_logs):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        identifier = _forward_request(socket, oracle, FUTURE_METHOD)
        assert _receive(socket, oracle) == {
            "type": "rpc.response", "id": identifier, "error": "unknown_method",
        }
        _forward_request(socket, oracle, "agent.challenge", identifier)
        with pytest.raises(WebSocketDisconnect) as closed:
            _receive(socket, oracle)
        assert closed.value.code == 1008


@pytest.mark.parametrize("extra", [{"unexpected": True}, {"id": "invalid"}])
def test_unknown_method_retains_request_validation(route, forward_logs, extra):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _forward_request(socket, oracle, FUTURE_METHOD, **extra)
        with pytest.raises(WebSocketDisconnect) as closed:
            _receive(socket, oracle)
        assert closed.value.code == 1008


@pytest.mark.asyncio
async def test_unknown_methods_count_toward_session_limit(monkeypatch, forward_logs):
    inbound = asyncio.Queue()
    replies = asyncio.Queue()
    closed = []

    async def idle(*args, **kwargs):
        await asyncio.Future()

    async def close(code):
        closed.append(code)

    monkeypatch.setattr(transport_ws, "_agent_socket", idle)
    channel = SimpleNamespace(
        authority=SimpleNamespace(current_policy=lambda: None),
        websocket=SimpleNamespace(client=None),
        receive=inbound.get, send=replies.put, close=close, watch_authority=idle,
    )
    task = asyncio.create_task(companion_session._serve(channel, SimpleNamespace()))
    try:
        for _ in range(1_024):
            identifier = str(uuid4())
            await inbound.put({
                "type": "rpc.request", "id": identifier,
                "method": FUTURE_METHOD, "params": {},
            })
            assert await asyncio.wait_for(replies.get(), 2) == {
                "type": "rpc.response", "id": identifier, "error": "unknown_method",
            }
        await inbound.put({
            "type": "rpc.request", "id": str(uuid4()),
            "method": "agent.challenge", "params": {},
        })
        with pytest.raises(CompanionRecordError):
            await asyncio.wait_for(task, 2)
        assert closed == [1008]
        assert replies.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
