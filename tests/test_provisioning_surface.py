"""The first-stage ASGI app has no runtime routes or schemas."""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from app.provisioning.app import create_provisioning_app
from app.provisioning.session import PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME


def test_provisioning_app_exposes_no_runtime_route() -> None:
    application = create_provisioning_app(launcher_handoff_token="t" * 32)

    assert application.openapi_url is None
    assert {(route.path, tuple(sorted(route.methods or ()))) for route in application.routes} == {
        ("/health", ("GET",)),
        ("/api/v1/provisioning/handoff", ("POST",)),
        ("/provisioning/handoff", ("GET",)),
        ("/provisioning", ("GET",)),
        ("/api/v1/provisioning/status", ("GET",)),
        ("/api/v1/provisioning/retry", ("POST",)),
    }
    assert not any(isinstance(route, WebSocketRoute) for route in application.routes)


def test_ready_provisioning_requests_distinguished_restart() -> None:
    exits: list[str] = []
    application = create_provisioning_app(
        launcher_handoff_token="t" * 32,
        completion_ready=lambda: True,
        completion_exit=lambda: exits.append("restart"),
    )
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + "t" * 32},
    )
    redeemed = client.get(
        f"/provisioning/handoff?code={handoff.json()['handoff_code']}",
        follow_redirects=False,
    )

    status = client.get(
        "/api/v1/provisioning/status",
        headers={
            "Cookie": (
                f"{PROVISIONING_SESSION_COOKIE_NAME}="
                f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
            )
        },
    )

    assert status.json() == {"state": "configured_restart"}
    assert application.state.completion_requested is True
    assert exits == ["restart"]
