"""Test-only entry point for protocol business rules after session admission.

The production /ws/agent route is always the authenticated Noise adapter.
This explicit test path keeps its inner protocol tests independent of crypto.
"""

import pytest
from app.api.endpoints.transport_ws import _agent_socket
from app.main import app


@pytest.fixture
def inner_protocol_app(monkeypatch):
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    app.add_api_websocket_route("/__test__/agent-protocol", _agent_socket)
    return app
