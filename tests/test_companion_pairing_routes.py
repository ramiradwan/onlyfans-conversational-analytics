"""Pairing adapters enforce origins, bounded input and disconnect fences."""

from __future__ import annotations

import base64
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.endpoints import companion_pairing as routes
from app.api.security import csrf_token
from app.security.companion_pairing import CompanionPairingError
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
from app.security import companion_pairing_proof as proof
from test_companion_pairing_service import (
    local,
    contract,
    grant_references,
    _agent_confirmation,
)

ORIGIN = "http://bridge.localhost:17871"
EXTENSION = "a" * 32
PAIRING_BYTES = b"p" * 32
PAIRING_ID = base64.urlsafe_b64encode(PAIRING_BYTES).rstrip(b"=").decode("ascii")
POLICY = RuntimePolicy(
    AuthContext("operator", "account", "creator", session_id="durable-session"),
    AuthorizationEpoch(1),
)
REQUEST = {
    "type": "pair.request",
    "agent_installation_id": "agent",
    "agent_identity_jwk": {},
    "agent_noise_key": "key",
    "agent_nonce": "nonce",
}
CONFIRM = {"type": "pair.confirm", "pairing_id": PAIRING_ID, "agent_proof": "proof"}


class FakeService:
    def __init__(self):
        self.state = None
        self.calls = []
        self.claims = 0
        self.aborted = threading.Event()
        self.offered = threading.Event()
        self.agent_confirmed = threading.Event()
        self.offer_entered = threading.Event()
        self.offer_release = None
        self.claim_entered = threading.Event()
        self.claim_release = None
        self.late_completed = threading.Event()

    def public(self):
        return {
            "pairing_id": PAIRING_ID,
            "creator_account_id": "account",
            "generation": 1,
            "version": 1,
            "state": "open",
            "expires_at": "2026-09-12T12:00:00+00:00",
            "comparison_code": None,
            "agent_identity_thumbprint": None,
        }

    def open(self, policy, creator_account_id):
        self.calls.append(("open", policy, creator_account_id))
        return self.public()

    def status(self, policy, pairing_id):
        self.calls.append(("status", policy, pairing_id))
        return self.public()

    def confirm(self, policy, pairing_id, version):
        self.calls.append(("confirm", policy, pairing_id, version))
        return self.public()

    def cancel(self, policy, pairing_id, version, decline=False):
        self.calls.append(("cancel", policy, pairing_id, version, decline))
        return self.public()

    def claim(self):
        self.claim_entered.set()
        if self.claim_release is not None:
            self.claim_release.wait(2)
        self.claims += 1
        if self.claims > 1:
            self.state = "cancelled"
            raise CompanionPairingError()
        return SimpleNamespace(pairing_id=PAIRING_BYTES)

    def offer(self, window, request):
        self.offer_entered.set()
        if self.offer_release is not None:
            self.offer_release.wait(2)
        if self.aborted.is_set():
            self.late_completed.set()
            raise CompanionPairingError()
        self.offered.set()
        return {
            "type": "pair.offer",
            "pairing_id": PAIRING_ID,
            "generation": 1,
            "creator_account_id": "account",
            "brain_noise_key": "key",
            "brain_nonce": "nonce",
            "installation_jwk": {},
            "installation_grant": "grant",
            "creator_account_binding": "binding",
            "brain_proof": "proof",
        }

    def confirm_agent(self, pairing_id, document):
        if document != CONFIRM:
            raise CompanionPairingError("pairing_message_invalid")
        self.agent_confirmed.set()

    def outcome(self, pairing_id):
        return self.state

    def abort(self, pairing_id):
        assert pairing_id == PAIRING_BYTES
        self.aborted.set()
        self.state = "cancelled"


@pytest.fixture
def application(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(routes.settings, "bridge_origin", ORIGIN)
    monkeypatch.setattr(routes.settings, "extension_id", EXTENSION)
    monkeypatch.setattr(routes.settings, "websocket_bind_host", "127.0.0.1")
    monkeypatch.setattr(routes, "require_activated_runtime", lambda: None)
    monkeypatch.setattr(routes, "get_runtime_policy", lambda request: POLICY)
    monkeypatch.setattr(routes, "companion_pairing_service", lambda: service)
    monkeypatch.setattr(routes, "OUTCOME_POLL_SECONDS", 0.005)
    app = FastAPI()
    app.include_router(routes.router)
    return app, service


def http_headers():
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(POLICY)}


def socket_headers(**extras):
    return {
        "Host": "127.0.0.1:17871",
        "Origin": f"chrome-extension://{EXTENSION}",
        **extras,
    }


def client_for(application, **kwargs):
    return TestClient(
        application, base_url=ORIGIN, client=("127.0.0.1", 50000), **kwargs
    )


def test_http_routes_use_current_policy_csrf_versions_and_no_store(application):
    app, service = application
    with client_for(app) as client:
        result = client.post(
            "/api/v1/companion/pairings",
            json={"creator_account_id": "account"},
            headers=http_headers(),
        )
        assert result.status_code == 201
        for suffix in ("confirm", "cancel", "decline"):
            response = client.post(
                f"/api/v1/companion/pairings/{PAIRING_ID}/{suffix}",
                json={"version": 3},
                headers=http_headers(),
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
        response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert service.calls == [
        ("open", POLICY, "account"),
        ("confirm", POLICY, PAIRING_ID, 3),
        ("cancel", POLICY, PAIRING_ID, 3, False),
        ("cancel", POLICY, PAIRING_ID, 3, True),
        ("status", POLICY, PAIRING_ID),
    ]


@pytest.mark.parametrize(
    "case",
    ["no-origin", "other-origin", "other-host", "missing-csrf", "bad-csrf", "query"],
)
def test_http_mutations_require_exact_origin_and_csrf(application, case):
    app, service = application
    headers = http_headers()
    path = "/api/v1/companion/pairings"
    if case == "no-origin":
        headers.pop("Origin")
    elif case == "other-origin":
        headers["Origin"] = "https://hostile.example"
    elif case == "other-host":
        headers["Host"] = "hostile.example"
    elif case == "missing-csrf":
        headers.pop("X-CSRF-Token")
    elif case == "bad-csrf":
        headers["X-CSRF-Token"] = "secret-sentinel"
    else:
        path += "?auth_ticket=secret-sentinel"
    with client_for(app) as client:
        response = client.post(
            path, json={"creator_account_id": "account"}, headers=headers
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "pairing_account_refused"}
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == []


@pytest.mark.parametrize(
    "body",
    [
        b'{"creator_account_id":"account","creator_account_id":"secret-sentinel"}',
        b'{"creator_account_id":"account","extra":"secret-sentinel"}',
        b'{"creator_account_id":true}',
        b'{"creator_account_id":NaN}',
        b"[]",
        b'{"creator_account_id":"\xff"}',
        b" " * 2049,
    ],
    ids=["duplicate", "extra", "type", "non-json", "array", "invalid-utf8", "oversize"],
)
def test_http_body_is_strict_bounded_and_payload_free(application, body):
    app, service = application
    with client_for(app) as client:
        response = client.post(
            "/api/v1/companion/pairings", content=body, headers=http_headers()
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "pairing_message_invalid"}
    assert service.calls == []


@pytest.mark.parametrize("version", [True, -1, 2**53, 1.0, "1"])
def test_http_versions_do_not_coerce_untrusted_values(application, version):
    app, service = application
    with client_for(app) as client:
        response = client.post(
            f"/api/v1/companion/pairings/{PAIRING_ID}/confirm",
            json={"version": version},
            headers=http_headers(),
        )
    assert response.status_code == 400
    assert service.calls == []


def test_http_authentication_activation_and_internal_failures_are_fixed(
    application, monkeypatch
):
    app, service = application
    with client_for(app) as client:
        monkeypatch.setattr(
            routes,
            "get_runtime_policy",
            lambda request: RuntimePolicy(None, AuthorizationEpoch(1)),
        )
        response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
        assert response.status_code == 401
        assert response.json() == {"detail": "pairing_account_refused"}
        monkeypatch.setattr(routes, "get_runtime_policy", lambda request: POLICY)

        def unavailable():
            raise HTTPException(503, "private-activation-sentinel")

        monkeypatch.setattr(routes, "require_activated_runtime", unavailable)
        response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
        assert response.status_code == 503
        assert response.json() == {"detail": "pairing_storage_refused"}
        monkeypatch.setattr(routes, "require_activated_runtime", lambda: None)

        def failed(*args):
            raise RuntimeError("private-database-sentinel")

        monkeypatch.setattr(service, "status", failed)
        response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
        assert response.status_code == 503
        assert response.json() == {"detail": "pairing_storage_refused"}


def test_http_status_does_not_export_new_service_fields(application, monkeypatch):
    app, service = application
    monkeypatch.setattr(
        service,
        "status",
        lambda *args: {**service.public(), "compact_jws": "secret-sentinel"},
    )
    with client_for(app) as client:
        response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
    assert response.status_code == 503
    assert response.json() == {"detail": "pairing_storage_refused"}


@pytest.mark.parametrize(
    "case",
    [
        "no-origin",
        "other-extension",
        "null-origin",
        "other-host",
        "bridge-host",
        "localhost-alias",
        "query",
        "authorization",
        "cookie",
        "subprotocol",
        "remote-client",
        "nonloopback-bind",
        "unpackaged-id",
    ],
)
def test_websocket_refuses_untrusted_origins_and_ambient_credentials(
    application, monkeypatch, case
):
    app, service = application
    headers = socket_headers()
    path = "/ws/agent/pairing"
    client_address = ("127.0.0.1", 50000)
    if case == "no-origin":
        headers.pop("Origin")
    elif case == "other-extension":
        headers["Origin"] = "chrome-extension://" + "b" * 32
    elif case == "null-origin":
        headers["Origin"] = "null"
    elif case == "other-host":
        headers["Host"] = "hostile.example:17871"
    elif case == "bridge-host":
        headers["Host"] = "bridge.localhost:17871"
    elif case == "localhost-alias":
        headers["Host"] = "localhost:17871"
    elif case == "query":
        path += "?auth_ticket=secret-sentinel"
    elif case == "authorization":
        headers["Authorization"] = "Bearer secret-sentinel"
    elif case == "cookie":
        headers["Cookie"] = "auth_ticket=secret-sentinel"
    elif case == "subprotocol":
        headers["Sec-WebSocket-Protocol"] = "secret-sentinel"
    elif case == "remote-client":
        client_address = ("192.0.2.1", 50000)
    elif case == "nonloopback-bind":
        monkeypatch.setattr(routes.settings, "websocket_bind_host", "0.0.0.0")
    else:
        monkeypatch.setattr(routes.settings, "extension_id", "development")
    with TestClient(app, base_url=ORIGIN, client=client_address) as client:
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect(path, headers=headers):
                pytest.fail("untrusted pairing socket was accepted")
    assert caught.value.code == 1008
    assert caught.value.reason == "pairing_account_refused"
    assert service.claims == 0


@pytest.mark.parametrize(
    "case", ["oversize", "multibyte-oversize", "duplicate", "binary", "wrong-type"]
)
def test_websocket_frame_limits_and_closed_first_type(application, case):
    app, service = application
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            if case == "oversize":
                websocket.send_text(" " * 36865)
            elif case == "multibyte-oversize":
                websocket.send_text("é" * 18433)
            elif case == "duplicate":
                websocket.send_text('{"type":"pair.request","type":"pair.request"}')
            elif case == "binary":
                websocket.send_bytes(b"secret-sentinel")
            else:
                websocket.send_json(
                    {"type": "agent.hello", "auth_ticket": "secret-sentinel"}
                )
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_json()
    assert caught.value.code == 1008
    assert caught.value.reason == "pairing_message_invalid"
    assert service.claims == 0


def test_websocket_first_frame_deadline_does_not_claim_a_window(
    application, monkeypatch
):
    app, service = application
    monkeypatch.setattr(routes, "STEP_TIMEOUT_SECONDS", 0.03)
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_json()
    assert caught.value.code == 1008
    assert service.claims == 0


def test_websocket_confirmation_deadline_aborts_window(application, monkeypatch):
    app, service = application
    monkeypatch.setattr(routes, "STEP_TIMEOUT_SECONDS", 0.05)
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            websocket.send_json(REQUEST)
            assert websocket.receive_json()["type"] == "pair.offer"
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()
        assert service.aborted.wait(1)


def test_websocket_result_wait_preserves_confirmed_pairing(application):
    app, service = application
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            websocket.send_json(REQUEST)
            assert websocket.receive_json()["type"] == "pair.offer"
            websocket.send_json(CONFIRM)
            assert service.agent_confirmed.wait(1)
            service.state = "confirmed"
            assert websocket.receive_json() == {
                "type": "pair.result",
                "pairing_id": PAIRING_ID,
                "outcome": "confirmed",
            }
    assert not service.aborted.is_set()


@pytest.mark.parametrize(
    "extra_frame", [False, True], ids=["disconnect", "extra-frame"]
)
def test_waiting_for_operator_aborts_on_disconnect_or_unexpected_frame(
    application, extra_frame
):
    app, service = application
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            websocket.send_json(REQUEST)
            websocket.receive_json()
            websocket.send_json(CONFIRM)
            assert service.agent_confirmed.wait(1)
            if extra_frame:
                websocket.send_json(CONFIRM)
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_json()
            else:
                websocket.close()
        assert service.aborted.wait(1)


def test_second_connection_cancels_window_and_first_socket(application):
    app, service = application
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as first:
            first.send_json(REQUEST)
            first.receive_json()
            with client.websocket_connect(
                "/ws/agent/pairing", headers=socket_headers()
            ) as second:
                second.send_json(REQUEST)
                with pytest.raises(WebSocketDisconnect):
                    second.receive_json()
            result = first.receive_json()
            assert result["type"] == "pair.result"
            assert result["outcome"] == "cancelled"
        assert service.state == "cancelled"
        assert service.claims == 2


@pytest.mark.parametrize("phase", ["claim", "offer"])
def test_timeout_fences_late_thread_completion(application, monkeypatch, phase):
    app, service = application
    release = threading.Event()
    setattr(service, f"{phase}_release", release)
    monkeypatch.setattr(routes, "STEP_TIMEOUT_SECONDS", 0.04)
    try:
        with client_for(app) as client:
            with client.websocket_connect(
                "/ws/agent/pairing", headers=socket_headers()
            ) as websocket:
                websocket.send_json(REQUEST)
                assert getattr(service, f"{phase}_entered").wait(1)
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_json()
                if phase == "offer":
                    assert service.aborted.wait(1)
                release.set()
                assert service.aborted.wait(1)
                if phase == "offer":
                    assert service.late_completed.wait(1)
                assert not service.offered.is_set()
    finally:
        release.set()


def test_disconnect_aborts_while_signing_thread_is_still_working(application):
    app, service = application
    service.offer_release = threading.Event()
    try:
        with client_for(app) as client:
            with client.websocket_connect(
                "/ws/agent/pairing", headers=socket_headers()
            ) as websocket:
                websocket.send_json(REQUEST)
                assert service.offer_entered.wait(1)
                websocket.close()
            assert service.aborted.wait(1)
            service.offer_release.set()
            assert service.late_completed.wait(1)
            assert not service.offered.is_set()
    finally:
        service.offer_release.set()


@pytest.mark.parametrize("operation", ["open", "confirm", "status"])
def test_http_deadline_covers_service_work_and_fences_late_mutation(
    application, monkeypatch, operation
):
    app, service = application
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()

    def slow(*args):
        entered.set()
        release.wait(2)
        finished.set()
        return service.public()

    monkeypatch.setattr(service, operation, slow)
    monkeypatch.setattr(routes, "STEP_TIMEOUT_SECONDS", 0.04)
    try:
        with client_for(app) as client:
            if operation == "open":
                response = client.post(
                    "/api/v1/companion/pairings",
                    json={"creator_account_id": "account"},
                    headers=http_headers(),
                )
            elif operation == "confirm":
                response = client.post(
                    f"/api/v1/companion/pairings/{PAIRING_ID}/confirm",
                    json={"version": 1},
                    headers=http_headers(),
                )
            else:
                response = client.get(f"/api/v1/companion/pairings/{PAIRING_ID}")
            assert entered.is_set()
            assert response.status_code == 408
            assert response.json() == {"detail": "pairing_state_refused"}
            if operation == "confirm":
                assert service.aborted.wait(1)
            release.set()
            assert finished.wait(1)
            if operation != "status":
                assert service.aborted.wait(1)
            else:
                assert not service.aborted.is_set()
    finally:
        release.set()


async def test_http_disconnect_fences_late_open_worker(monkeypatch):
    service = FakeService()
    release = threading.Event()
    entered = threading.Event()
    disconnected = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    def slow():
        entered.set()
        release.wait(2)
        return service.public()

    task = asyncio.create_task(
        routes._http_call(
            SimpleNamespace(receive=receive),
            slow,
            service=service,
            operation="open",
            pairing_id=None,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        disconnected.set()
        with pytest.raises(routes._Refusal):
            await task
        release.set()
        assert await asyncio.to_thread(service.aborted.wait, 1)
    finally:
        release.set()


def test_whole_window_deadline_bounds_operator_wait(application, monkeypatch):
    app, service = application
    monkeypatch.setattr(routes, "WINDOW_TIMEOUT_SECONDS", 0.08)
    with client_for(app) as client:
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            websocket.send_json(REQUEST)
            websocket.receive_json()
            websocket.send_json(CONFIRM)
            assert service.agent_confirmed.wait(1)
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()
        assert service.aborted.wait(1)


def test_real_routes_bind_signed_pairing_and_admit_only_after_bridge_confirmation(
    local, application, monkeypatch
):
    app, _ = application
    monkeypatch.setattr(routes, "companion_pairing_service", lambda: local.service)
    monkeypatch.setattr(routes, "get_runtime_policy", lambda request: local.policy)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(local.policy)}
    verified = threading.Event()
    confirm_agent = local.service.confirm_agent

    def confirmed(*args):
        confirm_agent(*args)
        verified.set()

    monkeypatch.setattr(local.service, "confirm_agent", confirmed)
    with client_for(app) as client:
        opened = client.post(
            "/api/v1/companion/pairings",
            json={"creator_account_id": local.account},
            headers=headers,
        )
        assert opened.status_code == 201
        public_id = opened.json()["pairing_id"]
        raw_id = base64.urlsafe_b64decode(public_id + "=")
        with client.websocket_connect(
            "/ws/agent/pairing", headers=socket_headers()
        ) as websocket:
            websocket.send_json(local.request)
            offer = websocket.receive_json()
            assert offer["type"] == "pair.offer"
            window = local.service.persistence.window(raw_id)
            assert proof.verify_pairing_proof(
                offer["installation_jwk"],
                "brain",
                window.pairing_digest,
                offer["brain_proof"],
            )
            websocket.send_json(_agent_confirmation(local, window))
            assert verified.wait(1)
            status = client.get(f"/api/v1/companion/pairings/{public_id}")
            assert status.status_code == 200
            assert status.json()["state"] == "awaiting_confirmation"
            assert status.json()["comparison_code"] == proof.comparison_code(
                window.pairing_digest
            )
            assert local.service.persistence.companion_pin(raw_id) is None
            admitted = client.post(
                f"/api/v1/companion/pairings/{public_id}/confirm",
                json={"version": status.json()["version"]},
                headers=headers,
            )
            assert admitted.status_code == 200
            assert admitted.json()["state"] == "admitted"
            assert websocket.receive_json() == {
                "type": "pair.result",
                "pairing_id": public_id,
                "outcome": "confirmed",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1000
        assert local.service.persistence.companion_pin(raw_id) is not None
        assert local.service.persistence.window(raw_id) is None
        rendered = opened.text + status.text + admitted.text
        for grant in local.grants.values():
            if grant.compact_jws in rendered:
                pytest.fail(
                    "Bridge pairing status exposed retained grant", pytrace=False
                )


def test_confirmed_outcome_racing_worker_completion_preserves_delivered_pairing(
    application, monkeypatch
):
    app, service = application
    release = threading.Event()

    def pending_worker(*args):
        service.state = "confirmed"
        release.wait(2)

    monkeypatch.setattr(service, "confirm_agent", pending_worker)
    try:
        with client_for(app) as client:
            with client.websocket_connect(
                "/ws/agent/pairing", headers=socket_headers()
            ) as websocket:
                websocket.send_json(REQUEST)
                websocket.receive_json()
                websocket.send_json(CONFIRM)
                assert websocket.receive_json()["outcome"] == "confirmed"
                with pytest.raises(WebSocketDisconnect) as caught:
                    websocket.receive_json()
                assert caught.value.code == 1000
                assert not service.aborted.is_set()
                release.set()
    finally:
        release.set()


async def test_disconnect_during_result_delivery_aborts_pending_pin(monkeypatch):
    service = FakeService()
    incoming = asyncio.Queue()
    await incoming.put({"type": "websocket.receive", "text": json.dumps(REQUEST)})

    class Socket:
        def __init__(self):
            self.closed = []

        async def accept(self):
            pass

        async def receive(self):
            return await incoming.get()

        async def send_text(self, encoded):
            document = json.loads(encoded)
            if document["type"] == "pair.offer":
                await incoming.put(
                    {"type": "websocket.receive", "text": json.dumps(CONFIRM)}
                )
            else:
                await incoming.put({"type": "websocket.disconnect", "code": 1000})
                await asyncio.sleep(0)

        async def close(self, **kwargs):
            self.closed.append(kwargs)

    socket = Socket()
    monkeypatch.setattr(routes, "_socket_origin", lambda websocket: None)
    monkeypatch.setattr(routes, "companion_pairing_service", lambda: service)
    monkeypatch.setattr(
        service, "confirm_agent", lambda *args: setattr(service, "state", "confirmed")
    )
    monkeypatch.setattr(routes, "OUTCOME_POLL_SECONDS", 0.001)
    await asyncio.wait_for(routes.companion_pairing_socket(socket), timeout=2)
    assert service.aborted.is_set()
    assert socket.closed == [{"code": 1008, "reason": "pairing_state_refused"}]
