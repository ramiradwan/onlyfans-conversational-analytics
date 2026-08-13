"""Constrains where the runtime activation gate is enforced.

Two surfaces reach account-scoped state: mounted HTTP/WebSocket routes and the
transport authentication entry points. This module enumerates the first from
the running application rather than from a hand-kept list, so a route that does
not exist yet is constrained the moment it is mounted, and covers each
transport entry point separately.
"""

from __future__ import annotations

from typing import Iterable, Iterator
from uuid import uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.routing import BaseRoute, Mount, Router, WebSocketRoute
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.activation import require_activated_runtime
from app.persistence.factory import create_canonical_repositories
from app.security.activation_gate import (
    ActivationDecision,
    ConditionOutcome,
    REQUIRED_GRANTS,
    record_activation,
    reset_activation,
)
from app.transport.manager import AuthenticationError, InMemoryTransportManager


ACCOUNT = "creator-account-1"
PRINCIPAL = "principal-1"
REFUSAL = "The local runtime is not activated"

# Every mounted surface that is deliberately reachable before activation, with
# the reason it is. A surface absent from this mapping must carry the
# activation dependency; a mounted surface that carries neither fails the guard
# below, including one added after this file was written.
UNGATED_SURFACES: dict[str, str] = {
    "GET /health": (
        "liveness: an operator must be able to see that an unactivated "
        "installation is running"
    ),
    "MOUNT /static": "compiled frontend assets; reads no account-scoped state",
    "GET,HEAD /openapi.json": "FastAPI's own schema document; serves no account-scoped state",
    "GET,HEAD /docs": "FastAPI's own documentation shell; serves no account-scoped state",
    "GET,HEAD /docs/oauth2-redirect": "FastAPI's own documentation shell",
    "GET,HEAD /redoc": "FastAPI's own documentation shell; serves no account-scoped state",
    "POST /api/v1/session/bootstrap": (
        "bounded loopback provisioning: establishes the local session an "
        "installation needs before it can activate"
    ),
    "POST /api/v1/session/handoff": "bounded loopback provisioning; see session bootstrap",
    "GET /api/v1/session/handoff": "bounded loopback provisioning; see session bootstrap",
    "GET /": "SPA shell; the client that reports an unactivated runtime to the operator",
    "GET /{frontend_path:path}": "SPA shell refresh of the same document as GET /",
    "WEBSOCKET /ws/agent": (
        "activation is enforced inside "
        "transport_manager.authenticate_agent_handshake"
    ),
    "WEBSOCKET /ws/bridge": "activation is enforced inside transport_manager.authenticate",
    "GET /api/v1/agent/config": (
        "activation is enforced inside transport_manager.authenticate_agent_config"
    ),
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _discard_recorded_activation() -> Iterator[None]:
    reset_activation()
    yield
    reset_activation()


def _refused() -> ActivationDecision:
    return ActivationDecision(
        activated=False,
        production=True,
        outcomes=(
            ConditionOutcome(
                REQUIRED_GRANTS,
                False,
                "no installation_grant reference is inside its validity boundary",
            ),
        ),
    )


def _activated() -> ActivationDecision:
    return ActivationDecision(activated=True, production=True)


def _manager() -> InMemoryTransportManager:
    return InMemoryTransportManager(create_canonical_repositories("memory"))


# --------------------------------------------------------------------------
# Guard: every mounted route
# --------------------------------------------------------------------------


def _mounted_routes(router: Router) -> Iterator[BaseRoute]:
    """Yield every leaf route the application actually serves.

    Included routers are branches rather than leaves; a branch is descended
    into so a route inherits nothing by being nested.
    """

    low_priority: Iterable[BaseRoute] = getattr(router, "_low_priority_routes", ())
    for route in (*router.routes, *low_priority):
        branch = getattr(route, "original_router", None)
        if branch is not None:
            yield from _mounted_routes(branch)
        else:
            yield route


def _surface(route: BaseRoute) -> str:
    path = getattr(route, "path", "<unnamed>")
    if isinstance(route, Mount):
        return f"MOUNT {path}"
    methods = getattr(route, "methods", None)
    if not methods:
        return f"WEBSOCKET {path}"
    return f"{','.join(sorted(methods))} {path}"


def _is_gated(route: BaseRoute) -> bool:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    def resolves_gate(node: object) -> bool:
        if getattr(node, "call", None) is require_activated_runtime:
            return True
        return any(
            resolves_gate(sub) for sub in getattr(node, "dependencies", ())
        )

    return resolves_gate(dependant)


def _ungated_surfaces(app: FastAPI) -> list[str]:
    return sorted(
        {
            _surface(route)
            for route in _mounted_routes(app.router)
            if not _is_gated(route)
        }
    )


def test_every_mounted_route_is_gated_or_exempt() -> None:
    """A mounted route either carries the activation gate or is exempt by name."""

    unexplained = [
        surface
        for surface in _ungated_surfaces(main_module.app)
        if surface not in UNGATED_SURFACES
    ]

    assert not unexplained, (
        "A mounted route neither carries the activation dependency nor names "
        "a reason to be reachable before activation:\n"
        + "\n".join(unexplained)
    )


def test_no_exemption_outlives_the_route_it_excuses() -> None:
    """An exemption disappears with the surface it names, so no reason rots."""

    ungated = set(_ungated_surfaces(main_module.app))
    stale = sorted(surface for surface in UNGATED_SURFACES if surface not in ungated)

    assert not stale, (
        "An exemption names a surface that is now gated or no longer mounted:\n"
        + "\n".join(stale)
    )
    assert all(reason.strip() for reason in UNGATED_SURFACES.values())


def test_the_guard_reports_an_ungated_router() -> None:
    """The guard is measured against a router it has never seen."""

    gated = APIRouter(
        prefix="/api/v1/later",
        dependencies=[Depends(require_activated_runtime)],
    )

    @gated.get("/covered")
    async def covered() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    ungated = APIRouter(prefix="/api/v1/later")

    @ungated.get("/forgotten")
    async def forgotten() -> dict[str, str]:  # pragma: no cover - never called
        return {}

    @ungated.websocket("/ws/forgotten")
    async def forgotten_socket() -> None:  # pragma: no cover - never called
        return None

    application = FastAPI()
    application.include_router(gated)
    application.include_router(ungated)

    assert _ungated_surfaces(application) == [
        "GET /api/v1/later/forgotten",
        "GET,HEAD /docs",
        "GET,HEAD /docs/oauth2-redirect",
        "GET,HEAD /openapi.json",
        "GET,HEAD /redoc",
        "WEBSOCKET /api/v1/later/ws/forgotten",
    ]


def test_an_unactivated_runtime_refuses_an_insights_route() -> None:
    """The gate answers before the analytics surface reads any projection."""

    client = TestClient(main_module.app)

    record_activation(_refused())
    refused = client.get("/api/v1/insights/topics")
    record_activation(_activated())
    admitted = client.get("/api/v1/insights/topics")

    assert refused.status_code == 503
    assert refused.json()["detail"] == REFUSAL
    assert admitted.status_code != 503


# --------------------------------------------------------------------------
# Transport entry points
# --------------------------------------------------------------------------


def test_an_unactivated_runtime_refuses_the_agent_handshake() -> None:
    """app/transport/manager.py:464 — the gate precedes pairing consumption."""

    manager = _manager()
    installation = uuid4()
    pairing, _ = manager.issue_agent_pairing_ticket(
        principal_id=PRINCIPAL, creator_account_id=ACCOUNT
    )

    record_activation(_refused())
    with pytest.raises(AuthenticationError) as refusal:
        manager.authenticate_agent_handshake(pairing, ACCOUNT, installation)

    # The same single-use ticket still works once activated, so the refusal
    # above came from the gate rather than from consuming the pairing.
    record_activation(_activated())
    principal_id, account_id, _, _ = manager.authenticate_agent_handshake(
        pairing, ACCOUNT, installation
    )

    assert str(refusal.value) == REFUSAL
    assert (principal_id, account_id) == (PRINCIPAL, ACCOUNT)


def test_an_unactivated_runtime_refuses_agent_configuration_authentication() -> None:
    """app/transport/manager.py:561 — the gate precedes config-ticket verification."""

    manager = _manager()
    installation = uuid4()
    pairing, _ = manager.issue_agent_pairing_ticket(
        principal_id=PRINCIPAL, creator_account_id=ACCOUNT
    )
    record_activation(_activated())
    _, _, _, config_ticket = manager.authenticate_agent_handshake(
        pairing, ACCOUNT, installation
    )

    record_activation(_refused())
    with pytest.raises(AuthenticationError) as refusal:
        manager.authenticate_agent_config(config_ticket, ACCOUNT, installation)

    # The ticket is unspent, so the refusal came from the gate.
    record_activation(_activated())
    admitted = manager.authenticate_agent_config(config_ticket, ACCOUNT, installation)

    assert str(refusal.value) == REFUSAL
    assert admitted == (PRINCIPAL, ACCOUNT)
