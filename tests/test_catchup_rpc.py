from uuid import uuid4

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]

from test_companion_session_routes import (
    route, admitted, local, contract, grant_references, ORIGIN, _client,
    _establish, _prove, _send, _receive, _rpc, _refused_rpc,
)
from test_catchup_ledger import START, report, request


@pytest.mark.parametrize("bad_ticket", [False, True])
def test_catchup_rpcs_use_current_config_ticket(route, bad_ticket):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        authenticated = _prove(socket, oracle, route)
        _send(socket, oracle, {
            "type": "agent.hello", "protocol_version": "2", "message_id": str(uuid4()),
            "payload": {
                "auth_ticket": authenticated["auth_ticket"],
                "agent_installation_id": route.snapshot.pin.agent_installation_id,
                "requested_creator_account_id": route.local.account,
                "capabilities": ["capture.chats", "history.catchup.v1"],
                "extension_version": "2.0.1", "agent_stream_id": str(uuid4()),
                "last_acknowledged_source_seq": 0, "applied_config_revision": None,
            },
        })
        session = _receive(socket, oracle)
        assert session["type"] == "agent.session"
        auth = dict(protocol_version="2", auth_ticket="invalid" if bad_ticket else session["payload"]["config_auth_ticket"],
                    agent_installation_id=route.snapshot.pin.agent_installation_id, creator_account_id=route.local.account)
        body = dict(auth, operation="capture.state.report", **report(START))
        if bad_ticket:
            assert _receive(socket, oracle)["type"] == "sync.required"
            _refused_rpc(socket, oracle, "capture.state.report", body)
            return
        assert _rpc(socket, oracle, "capture.state.report", body) == {"acknowledged_seq": 1}
        assert _rpc(socket, oracle, "capture.state.report", body) == {"acknowledged_seq": 1}
        result = _rpc(socket, oracle, "history.check.begin", dict(auth, operation="history.check.begin", **request()))
        assert result["result"] == "deferred"
        assert result["reason"] in {"not_runnable", "history_incomplete"}


@pytest.mark.parametrize("method", ["capture.state.report", "history.check.begin"])
def test_catchup_rpc_requires_identity_proof(route, method):
    with _client(route) as client, client.websocket_connect(
        "/ws/agent", headers={"Host": "127.0.0.1:17871", "Origin": ORIGIN}
    ) as socket:
        oracle = _establish(socket, route)
        _refused_rpc(socket, oracle, method, {})
