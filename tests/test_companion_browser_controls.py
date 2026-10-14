"""Browser status reports and controls ride authenticated companion sessions (ADR 0045)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import companion_pairing as routes
from app.api.endpoints.companion_session import BrowserSessionLink, browser_surface
from app.api.security import csrf_token
from app.bootstrap import transport_manager
from app.protocol import BRAIN_TO_BRIDGE_ADAPTER
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
from app.transport.companion_records import CompanionRecordError

pytestmark = [pytest.mark.ci_tier('fast')]

ACCOUNT = "creator-1"
ORIGIN = "http://bridge.localhost:17871"
REPORT = {
    "schema": "ofca-browser-surface/v2",
    "capture": "paused",
    "site_access": "granted",
    "history_permission": "missing",
    "legal_review_required": False,
    "history_error_code": None,
}


@pytest.fixture(autouse=True)
def reset_manager():
    transport_manager.reset()
    yield
    transport_manager.reset()


class Channel:
    def __init__(self, identity=AuthContext("agent:a", ACCOUNT, "agent")):
        self.sent: list[dict] = []
        self.identity = identity
        self.authority = SimpleNamespace(current_policy=self.policy)

    def policy(self):
        return RuntimePolicy(identity=self.identity, authorization_epoch=AuthorizationEpoch(1))

    async def send(self, value):
        self.sent.append(value)


def link(channel, *, authenticated=True, pairing_id=b"p" * 32):
    rpc = SimpleNamespace(auth_ticket="ticket" if authenticated else None)
    return BrowserSessionLink(
        channel, rpc, SimpleNamespace(creator_account_id=ACCOUNT, pairing_id=pairing_id)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "ofca-browser-surface/v1"},
        {"capture": "on"},
        {"site_access": "unknown"},
        {"history_permission": True},
        {"history_error_code": "internal-private-reason"},
        {"history_error_code": {"private": "value"}},
        {"legal_review_required": "false"},
        {"extra": 1},
    ],
)
def test_browser_reports_are_closed_and_identifier_free(mutation):
    with pytest.raises(CompanionRecordError):
        browser_surface({**REPORT, **mutation})
    assert browser_surface(REPORT) == {key: REPORT[key] for key in REPORT if key != "schema"}


def test_reports_require_an_authenticated_agent_of_the_pinned_account():
    for session in (
        link(Channel(), authenticated=False),
        link(Channel(AuthContext("agent:a", "other", "agent"))),
        link(Channel(AuthContext("operator", ACCOUNT, "creator"))),
    ):
        with pytest.raises(CompanionRecordError):
            asyncio.run(session.report(REPORT))
    assert transport_manager.browser_surface(ACCOUNT) is None


def test_the_newest_open_report_is_the_account_state_and_closing_clears_it():
    first, second = link(Channel()), link(Channel(), pairing_id=b"q" * 32)

    async def exercise():
        await first.report(REPORT)
        await second.report({**REPORT, "capture": "active"})
        assert transport_manager.browser_surface(ACCOUNT)["capture"] == "active"
        await first.report({**REPORT, "capture": "paused"})
        assert transport_manager.browser_surface(ACCOUNT)["capture"] == "paused"
        payload = transport_manager.agent_state_payload(ACCOUNT)
        document = {
            "type": "agent.state",
            "protocol_version": "2",
            "message_id": "00000000-0000-4000-8000-000000000016",
            "payload": payload,
        }
        assert BRAIN_TO_BRIDGE_ADAPTER.validate_json(json.dumps(document)) is not None
        await first.close()
        await second.close()
        assert transport_manager.browser_surface(ACCOUNT) is None
        assert transport_manager.agent_state_payload(ACCOUNT)["browser"] is None

    asyncio.run(exercise())


def test_controls_reach_only_reporting_sessions_and_revocation_only_its_pin():
    reporting, silent, other_pin = Channel(), Channel(), Channel()

    async def exercise():
        await link(reporting).report(REPORT)
        transport_manager.register_browser_session(ACCOUNT, "p" * 43, silent.send)
        await link(other_pin, pairing_id=b"q" * 32).report(REPORT)
        assert await transport_manager.send_browser_control(ACCOUNT, "capture.resume") == 2
        assert await transport_manager.send_browser_control("other", "capture.pause") == 0
        revoked = await transport_manager.send_browser_control(
            ACCOUNT, "companion.revoked", pairing_id="cCcC"
        )
        assert revoked == 0
        with pytest.raises(ValueError):
            await transport_manager.send_browser_control(ACCOUNT, "consent.full")

    asyncio.run(exercise())
    assert [message["action"] for message in reporting.sent] == ["capture.resume"]
    assert silent.sent == []
    assert set(reporting.sent[0]) == {"type", "id", "action"}
    assert reporting.sent[0]["type"] == "session.control"


def _app(monkeypatch, role):
    policy = RuntimePolicy(
        AuthContext("principal", ACCOUNT, role, session_id="session"), AuthorizationEpoch(1)
    )
    monkeypatch.setattr(routes.settings, "bridge_origin", ORIGIN)
    monkeypatch.setattr(routes, "require_activated_runtime", lambda: None)
    monkeypatch.setattr(routes, "get_runtime_policy", lambda request: policy)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app, base_url=ORIGIN), {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(policy)}


def test_capture_controls_are_creator_only_and_report_unreachable_browsers(monkeypatch):
    sent = []

    async def deliver(account, action, **options):
        sent.append((account, action, options))
        return 1 if action == "capture.pause" else 0

    monkeypatch.setattr(transport_manager, "send_browser_control", deliver)
    client, headers = _app(monkeypatch, "creator")
    with client:
        paused = client.post("/api/v1/companion/browser/capture", json={"action": "pause"}, headers=headers)
        resumed = client.post("/api/v1/companion/browser/capture", json={"action": "resume"}, headers=headers)
        invalid = client.post("/api/v1/companion/browser/capture", json={"action": "full"}, headers=headers)
        no_csrf = client.post("/api/v1/companion/browser/capture", json={"action": "pause"}, headers={"Origin": ORIGIN})
    assert (paused.status_code, paused.json()) == (202, {"delivered": 1})
    assert (resumed.status_code, resumed.json()) == (409, {"detail": "browser_unreachable"})
    assert invalid.status_code == 400
    assert no_csrf.status_code == 403
    assert sent == [(ACCOUNT, "capture.pause", {}), (ACCOUNT, "capture.resume", {})]

    operator, operator_headers = _app(monkeypatch, "operator")
    with operator:
        refused = operator.post(
            "/api/v1/companion/browser/capture", json={"action": "pause"}, headers=operator_headers
        )
    assert refused.status_code == 403
    assert len(sent) == 2


def task_payload():
    return {"creator_account_id": ACCOUNT, "task": {"task_id": str(uuid4()), "admission_generation": str(uuid4()),
        "expires_at": time.time() + 60}, "ceremony": {"ceremony_id": "synthetic-ceremony", "attempt_id": "synthetic-attempt"}}


def task_report(value, *, notification=None):
    return {"task_id": value["task"]["task_id"], "admission_generation": value["task"]["admission_generation"],
        "state": "opening" if notification is None else "checking", "notification": notification}


@pytest.mark.asyncio
async def test_focused_browser_dispatch_pins_one_session_and_only_correlates_results():
    first, other = link(Channel()), link(Channel(), pairing_id=b"q" * 32)
    await first.report(REPORT)
    value, received, gates = task_payload(), [], []

    async def receive(result):
        received.append(result)

    pending = asyncio.create_task(transport_manager.send_browser_task(ACCOUNT, first.token, value,
        authorize=lambda: gates.append(True), receive=receive))
    await asyncio.sleep(0)
    assert gates == [True]
    assert first.channel.sent[-1] == {"type": "session.control", "id": value["task"]["task_id"], "action": "browser.task", "payload": value}
    await other.report(REPORT)
    assert transport_manager.browser_task_target(ACCOUNT) is None, "Multiple pins require an explicit browser choice"
    await other.task_report(task_report(value))
    assert not received and not pending.done()
    await first.task_report(task_report(value))
    assert await pending == "dispatched"
    note = {**value["ceremony"], "notification": "changed"}
    await first.task_report(task_report(value, notification=note))
    assert received[-1]["notification"] == note
    assert set(received[-1]) == {"task_id", "admission_generation", "state", "notification"}
    await first.close()
    await other.close()


@pytest.mark.asyncio
async def test_browser_dispatch_refuses_stale_reports_and_unknown_actions():
    session = link(Channel())
    await session.report(REPORT)
    value, received = task_payload(), []

    async def receive(result):
        received.append(result)

    pending = asyncio.create_task(transport_manager.send_browser_task(ACCOUNT, session.token, value, authorize=lambda: None, receive=receive))
    await asyncio.sleep(0)
    await session.task_report(task_report(value) | {"admission_generation": str(uuid4())})
    assert not received
    for invalid in [task_report(value) | {"completed": True}, task_report(value) | {"state": "complete"},
                    task_report(value) | {"state": []},
                    task_report(value, notification={**value["ceremony"], "notification": []}),
                    task_report(value, notification={**value["ceremony"], "notification": "changed", "license": "never-accepted"}),
                    task_report(value, notification={**value["ceremony"], "attempt_id": "wrong", "notification": "changed"})]:
        with pytest.raises(ValueError):
            await session.task_report(invalid)
    value["task"]["expires_at"] = time.time() - 1
    await session.task_report(task_report(value))
    assert not received
    with pytest.raises(ValueError):
        await transport_manager.send_browser_navigation(ACCOUNT, session.token, "https://example.test")
    await session.close()
    assert await pending == "unconfirmed"


@pytest.mark.asyncio
async def test_browser_reports_require_current_account_authority_and_dispatch_rechecks_operator():
    session = link(Channel())
    await session.report(REPORT)
    value = task_payload()

    def refused():
        raise ValueError("operator changed")

    async def receive(_value):
        pytest.fail("A refused operation must not receive an outcome")

    result = await transport_manager.send_browser_task(ACCOUNT, session.token, value, authorize=refused, receive=receive)
    assert result == "unconfirmed" and not session.channel.sent
    session.channel.identity = None
    with pytest.raises(CompanionRecordError):
        await session.task_report(task_report(value))
    await session.close()


def test_browser_navigation_uses_operator_csrf_and_closed_destinations(monkeypatch):
    calls = []

    async def navigate(account, target, destination):
        calls.append((account, target, destination))
        return True

    monkeypatch.setattr(transport_manager, "browser_task_target", lambda account: 17)
    monkeypatch.setattr(transport_manager, "send_browser_navigation", navigate)
    client, headers = _app(monkeypatch, "operator")
    with client:
        assert client.post("/api/v1/companion/browser/open", json={"action": "history"}, headers=headers).status_code == 202
        assert client.post("/api/v1/companion/browser/open", json={"action": "https://example.test"}, headers=headers).status_code == 400
        assert client.post("/api/v1/companion/browser/open", json={"action": "history", "creator": "other"}, headers=headers).status_code == 400
        assert client.post("/api/v1/companion/browser/open", json={"action": "history"}, headers={"Origin": ORIGIN}).status_code == 403
    assert calls == [(ACCOUNT, 17, "history")]


@pytest.mark.asyncio
async def test_repeated_delivery_fences_the_old_waiter_and_disconnect_clears_the_bounded_session():
    session = link(Channel())
    await session.report(REPORT)
    value = task_payload()

    async def receive(_value):
        pass

    def dispatch(payload):
        return asyncio.create_task(transport_manager.send_browser_task(ACCOUNT, session.token, payload,
            authorize=lambda: None, receive=receive))

    first = dispatch(value)
    await asyncio.sleep(0)
    replacement = {**value, "task": {**value["task"], "admission_generation": str(uuid4())}}
    second = dispatch(replacement)
    await asyncio.sleep(0)
    assert await first == "unconfirmed"
    await session.task_report(task_report(value))
    assert not second.done()
    await session.task_report(task_report(replacement))
    assert await second == "dispatched"
    pending = [dispatch(task_payload()) for _ in range(8)]
    await asyncio.sleep(0)
    assert await pending[-1] == "unconfirmed", "Only eight fixed-lifetime records may occupy the session"
    await session.close()
    assert await asyncio.gather(*pending) == ["unconfirmed"] * 8
    assert transport_manager.browser_task_target(ACCOUNT) is None
