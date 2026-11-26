from contextlib import ExitStack
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import security
from app.api.endpoints import saved_data, transport_ws, webauthn
from app.api.security import csrf_token, webauthn_session_token
from app.core.config import settings
from app.persistence.auth import (
    AuthenticationStateError, AuthorizedAccountBinding, BridgeSessionIssue, SQLiteAuthenticationStore,
    TicketBinding, TicketIssue, TicketPurpose,
)
from app.security.account_passkey_authority import AccountPasskeyAuthority, InitialLocalAccess
from app.security.runtime_policy import AuthContext
from app.security.saved_data import SavedDataSessions
from app.security.webauthn import WebAuthnAuthorityPort
from test_account_passkey_authority import Hello, VerifiedRecords
from test_continuation_selection import add_candidate, enroll
from test_transport_auth_v2 import manager
from test_websocket_transport import fixture as protocol_fixture
from test_webauthn_routes import ACCOUNT_ID, INSTANT, INSTALLATION_ID, ORIGIN, _seeded_store


pytestmark = pytest.mark.ci_tier("integration")


def test_lock_ends_open_sessions_and_tickets_and_preserves_the_installed_credential(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    store = _seeded_store(path, INSTANT)
    enroll(store, installation=INSTALLATION_ID)
    grants = ("installation_grant-http", "membership_snapshot-http", "creator_account_binding-http")
    association = add_candidate(store, creator=ACCOUNT_ID, approved=True, installation_id=INSTALLATION_ID,
                                organization_id="organization-http")
    store.record_authorized_account_binding(AuthorizedAccountBinding(
        creator_account_id=ACCOUNT_ID, installation_id=INSTALLATION_ID, platform_creator_id=ACCOUNT_ID,
        association_request_id=association, grant_bundle_sha256="a" * 64, authorized_at=INSTANT,
        grant_reference_ids=grants,
    ))
    key = ec.generate_private_key(ec.SECP256R1())
    records = VerifiedRecords(key)
    broker = Hello(key)
    authority = AccountPasskeyAuthority(store, records, records, environment="qa", broker=broker)
    owner = authority.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None)
    generation = str(uuid4())
    authority.stage_initial(InitialLocalAccess(generation, owner, records.initial))
    deadline = INSTANT + timedelta(minutes=5)
    issue = BridgeSessionIssue(owner.credential_id, owner.principal_id, ACCOUNT_ID, "creator", deadline, grants)
    first, second = store.issue_bridge_session(issue), store.issue_bridge_session(issue)
    ticket = store.issue_ticket(TicketIssue(
        TicketPurpose.BRIDGE_WEBSOCKET, owner.principal_id, "creator", ACCOUNT_ID, deadline,
        parent_session_id=second.session_id, expected_bridge_session_id=second.session_id,
    ))
    archives = SavedDataSessions()
    token, csrf = archives.issue(owner, 300)
    assert archives.accepts(owner, token, csrf)
    assert store.desktop_session_is_open(first.session_id) and store.desktop_session_is_open(second.session_id)

    app = FastAPI()
    app.state.account_passkey_authority = authority
    app.include_router(webauthn.router)
    app.include_router(transport_ws.router)
    app.dependency_overrides[webauthn.require_activated_runtime] = lambda: None
    monkeypatch.setattr(security, "SQLiteAuthenticationStore", lambda _path: store)
    monkeypatch.setattr(saved_data, "_sessions", archives)
    transport = manager()
    monkeypatch.setattr(transport_ws, "transport_manager", transport)
    with TestClient(app, base_url=ORIGIN) as client, ExitStack() as windows:
        sockets = []
        for session in (first, second):
            cookie = webauthn_session_token(session.policy.identity, session.session_value)
            headers = {"Cookie": f"{settings.bridge_session_cookie_name}={cookie}"}
            socket = windows.enter_context(client.websocket_connect("/ws/bridge", headers=headers))
            hello = protocol_fixture("bridge.hello")
            hello["payload"].update(
                bridge_session_id=str(uuid4()),
                requested_creator_account_id=ACCOUNT_ID,
                auth_ticket=transport.issue_bridge_ticket(
                    principal_id=owner.principal_id, role="creator", creator_account_id=ACCOUNT_ID,
                    parent_session_id=session.session_id,
                ),
            )
            socket.send_json(hello)
            assert socket.receive_json()["type"] == "bridge.session"
            assert [socket.receive_json()["type"] for _ in range(5)] == [
                "state.snapshot", "presence.state", "agent.state", "system.state", "companion.state",
            ]
            sockets.append(socket)
        response = client.post(
            "/api/v1/webauthn/lock",
            headers={**headers, "Origin": ORIGIN, "X-CSRF-Token": csrf_token(second.policy)},
        )
        assert response.status_code == 200 and response.json() == {"locked": True}
        for socket in sockets:
            event = socket.receive_json()
            assert event["type"] == "protocol.error" and event["payload"]["code"] == "unauthorized"
            assert event["payload"]["fatal"] is True
            with pytest.raises(WebSocketDisconnect) as disconnected:
                socket.receive_json()
            assert disconnected.value.code == 1008
        assert not transport.bridges
    assert not archives.is_open(token)
    assert store.read_bridge_session(first.session_value) is None
    assert store.read_bridge_session(second.session_value) is None
    assert store.consume_ticket(ticket.value, TicketBinding(
        TicketPurpose.BRIDGE_WEBSOCKET, ACCOUNT_ID, "creator", expected_bridge_session_id=second.session_id,
    )) is None
    with pytest.raises(AuthenticationStateError, match="locked"):
        store.issue_bridge_session(issue)
    with pytest.raises(AuthenticationStateError, match="locked"):
        store.build_runtime_policy_from_grants(AuthContext(owner.principal_id, ACCOUNT_ID, "agent"), grants)
    reopened = SQLiteAuthenticationStore(path, clock=store._now)
    with reopened.database.transaction() as db:
        assert reopened.desktop_lock_state(db) == (1, True)
        assert authority.active(db)[0].owner == owner
        with pytest.raises(AuthenticationStateError, match="changed"):
            reopened.unlock_desktop_after_assertion(db, 0)
    port = WebAuthnAuthorityPort(store, clock=store._now)
    policy = store.build_runtime_policy()
    def current_authority(connection=None):
        return port.session_authority(policy, connection=connection).authority

    sign_in = dict(authorize=lambda db: None, current_authority=current_authority, lifetime=300)
    assert authority.sign_in(current_authority(), initial_generation=generation, **sign_in) is None
    assert not broker.challenges
    saved_token, saved_csrf = authority.unlock(archives, authorize=lambda db: None, lifetime=300)
    assert archives.accepts(owner, saved_token, saved_csrf)
    with reopened.database.read() as db:
        assert reopened.desktop_lock_state(db) == (1, True)
    fresh = authority.sign_in(current_authority(), **sign_in)
    assert len(broker.challenges) == 2
    assert reopened.desktop_session_is_open(fresh.session_id)
    assert not reopened.desktop_session_is_open(second.session_id)
