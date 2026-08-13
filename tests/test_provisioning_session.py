"""Provisioning sessions remain launcher-bound and CSRF-protected."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
)


def test_cross_site_post_is_refused_at_the_origin_check() -> None:
    manager = ProvisioningSessionManager("t" * 32)
    code = manager.issue_handoff_code("Provisioning " + "t" * 32)
    session = manager.redeem_handoff_code(code)
    application = FastAPI()

    @application.post("/mutate")
    async def mutate(request: Request):
        manager.require_mutation(request)
        return {"ok": True}

    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    response = client.post(
        "/mutate",
        headers={
            "Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}",
            PROVISIONING_CSRF_HEADER: session.csrf_token,
            "Origin": "https://attacker.invalid",
        },
    )

    assert response.status_code == 403


def test_missing_csrf_is_refused_at_the_csrf_check() -> None:
    manager = ProvisioningSessionManager("t" * 32)
    code = manager.issue_handoff_code("Provisioning " + "t" * 32)
    session = manager.redeem_handoff_code(code)
    application = FastAPI()

    @application.post("/mutate")
    async def mutate(request: Request):
        manager.require_mutation(request)
        return {"ok": True}

    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    response = client.post(
        "/mutate",
        headers={
            "Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}",
            "Origin": PROVISIONING_ORIGIN,
        },
    )

    assert response.status_code == 403
