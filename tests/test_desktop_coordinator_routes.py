import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.onboarding.admission_routes import COOKIE_NAMES, CSRF, GENERATION, ORIGIN, csrf_token
from app.onboarding.routes import DesktopEventResponse, desktop_router
from app.provisioning.events import events
from test_desktop_coordinator import command, setup as coordinator_setup


@pytest.fixture
def setup(request):
    return coordinator_setup.__wrapped__(request.getfixturevalue("tmp_path"))

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


def test_phase_observation_is_scoped_closed_and_has_no_authority(setup, caplog):
    import logging
    coordinator, owner, session, _ = setup
    caplog.set_level(logging.INFO, logger="app.onboarding.timing")
    with client_for(coordinator, session) as client:
        value = {"operation": "frontend", "phase": "command-send", "status": "ok", "elapsed_ms": 12}
        assert client.post("/api/v1/desktop/setup/timing", json=value).status_code == 204
        assert client.post("/api/v1/desktop/setup/timing", json={**value, "url": "private"}).status_code == 400
        assert client.post("/api/v1/desktop/setup/timing", json=value, headers={CSRF: "wrong"}).status_code == 403
        assert not owner.calls
    assert caplog.messages == ["event=sign_in_phase operation=frontend phase=command-send status=ok elapsed_ms=12"]


def client_for(coordinator, session):
    app = FastAPI()
    app.include_router(desktop_router(coordinator))
    client = TestClient(app, base_url=ORIGIN)
    client.headers.update({"Host": "bridge.localhost:17871", "Origin": ORIGIN,
        GENERATION: session.generation, CSRF: csrf_token(session.identifier),
        "Cookie": COOKIE_NAMES["desktop"] + "=" + session.identifier})
    return client


def test_desktop_snapshot_and_commands_require_the_exact_role_and_generation(setup):
    coordinator, owner, session, _ = setup
    with client_for(coordinator, session) as client:
        snapshot = client.get("/api/v1/desktop/setup")
        assert snapshot.status_code == 200 and snapshot.headers["cache-control"] == "no-store"
        value = command(snapshot.json())
        response = client.post("/api/v1/desktop/setup/commands", json=value, headers={CSRF: "wrong"})
        assert response.status_code == 403
        response = client.post("/api/v1/desktop/setup/commands", json=value, headers={GENERATION: "wrong"})
        assert response.status_code == 401
        response = client.get("/api/v1/desktop/setup", headers={"Cookie": COOKIE_NAMES["browser_task"] + "=" + session.identifier})
        assert response.status_code == 401
    assert not owner.calls and not owner.admissions


@pytest.mark.parametrize("change,status", [
    ({"Host": "localhost:17871"}, 421), ({"Origin": "https://elsewhere.test"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
])
def test_request_origin_cannot_be_substituted(setup, change, status):
    coordinator, _, session, _ = setup
    with client_for(coordinator, session) as client:
        assert client.get("/api/v1/desktop/setup", headers=change).status_code == status


@pytest.mark.parametrize("content,status", [
    ('{"action":"browser.access","action":"account.connect"}', 400),
    ('{"x":NaN}', 400), ('{"x":Infinity}', 400), ('[]', 400),
    ('"' + 'a' * 16384 + '"', 413),
])
def test_closed_parser_rejects_duplicate_keys_nonfinite_values_and_oversize_bodies(setup, content, status):
    coordinator, owner, session, _ = setup
    with client_for(coordinator, session) as client:
        response = client.post("/api/v1/desktop/setup/commands", content=content,
            headers={"Content-Type": "application/json"})
    assert response.status_code == status and not owner.calls


@pytest.mark.asyncio
async def test_event_subscribes_before_read_and_retains_changes_while_sending(setup):
    coordinator, owner, session, _ = setup
    active = set()
    response = DesktopEventResponse(coordinator, session, actor=lambda: None, active=active, deadline=lambda: 1000)
    frames = []
    original = coordinator.read
    subscriptions_before = len(events._listeners)

    def read(*args, **kwargs):
        assert len(events._listeners) == subscriptions_before + 1
        return original(*args, **kwargs)

    coordinator.read = read

    async def send(message):
        if message["type"] == "http.response.body" and message["body"].startswith(b"event: setup"):
            frames.append(json.loads(message["body"].decode().split("data: ", 1)[1]))
            if len(frames) == 1:
                owner.facts["browser_access"] = "verified"
                events.publish()
            else:
                raise OSError("disconnected")

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 1)
    assert [value["next_task"] for value in frames] == ["browser-access", "account-connection"]
    assert not active and len(events._listeners) == subscriptions_before


@pytest.mark.asyncio
async def test_event_subscription_released_if_headers_cannot_be_sent(setup):
    coordinator, _, session, _ = setup
    active = set()
    response = DesktopEventResponse(coordinator, session, actor=lambda: None, active=active, deadline=lambda: 1000)
    before = len(events._listeners)

    async def send(message):
        raise OSError("disconnected before first frame")

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not active and len(events._listeners) == before
