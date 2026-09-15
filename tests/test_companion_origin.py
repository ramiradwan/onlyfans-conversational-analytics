"""The Agent origin cannot mint ambient Bridge authority before pairing."""

import pytest

from app.transport.companion_origin import CompanionOriginBoundary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/", "/api/v1/session/bootstrap", "/api/v1/session/handoff", "/api/v1/runtime"],
)
async def test_agent_origin_never_reaches_cookie_issuing_application(path):
    sent = []

    async def application(scope, receive, send):
        raise AssertionError("Agent HTTP origin reached Bridge application")

    async def send(event):
        sent.append(event)

    await CompanionOriginBoundary(application)(
        {"type": "http", "path": path, "headers": [(b"host", b"127.0.0.1:17871")]},
        None,
        send,
    )
    assert sent[0]["status"] == 404
    assert all(name.lower() != b"set-cookie" for name, _ in sent[0]["headers"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,path",
    [
        ([(b"host", b"127.0.0.1:17871")], "/ws/bridge"),
        (
            [(b"host", b"127.0.0.1:17871"), (b"host", b"bridge.localhost:17871")],
            "/ws/agent",
        ),
    ],
)
async def test_agent_origin_refuses_bridge_socket_and_ambiguous_host(headers, path):
    sent = []

    async def application(scope, receive, send):
        raise AssertionError("Refused origin reached application")

    async def send(event):
        sent.append(event)

    await CompanionOriginBoundary(application)(
        {"type": "websocket", "path": path, "headers": headers}, None, send
    )
    assert sent == [
        {"type": "websocket.close", "code": 1008, "reason": "session_refused"}
    ]


@pytest.mark.asyncio
async def test_bridge_origin_keeps_its_existing_application_boundary():
    scopes = []

    async def application(scope, receive, send):
        scopes.append(scope)

    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"host", b"bridge.localhost:17871")],
    }
    await CompanionOriginBoundary(application)(scope, None, None)
    assert scopes == [scope]
