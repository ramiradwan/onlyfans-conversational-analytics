from types import SimpleNamespace

import pytest

from app.api.endpoints import companion_session
from app.api.endpoints.companion_session import SessionRPC
from app.security.analysis_authorization import AnalysisReadiness
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
from app.transport.companion_records import CompanionRecordError


class _Authority:
    def __init__(self) -> None:
        self.authentication = object()
        self.identity = AuthContext("principal-1", "creator-1", "agent")

    def current_policy(self) -> RuntimePolicy:
        return RuntimePolicy(
            identity=self.identity,
            authorization_epoch=AuthorizationEpoch(1),
        )


def test_analysis_readiness_requires_fresh_agent_authentication(monkeypatch) -> None:
    authority = _Authority()
    rpc = SessionRPC(authority, SimpleNamespace(creator_account_id="creator-1"))
    monkeypatch.setattr(
        companion_session,
        "current_analysis_readiness",
        lambda store, identity: AnalysisReadiness("active", "admitted"),
    )

    with pytest.raises(CompanionRecordError):
        rpc.call("agent.analysis.readiness", {})


def test_analysis_readiness_rpc_returns_only_closed_nonsecret_state(monkeypatch) -> None:
    authority = _Authority()
    rpc = SessionRPC(authority, SimpleNamespace(creator_account_id="creator-1"))
    rpc.auth_ticket = "authenticated"
    seen = []

    def readiness(store, identity):
        seen.append((store, identity))
        return AnalysisReadiness("required", "blocked")

    monkeypatch.setattr(companion_session, "current_analysis_readiness", readiness)

    assert rpc.call("agent.analysis.readiness", {}) == {
        "schema": "ofca-analysis-readiness/v1",
        "commercial_authority": "required",
        "analysis_admission": "blocked",
    }
    assert seen == [(authority.authentication, authority.identity)]


def test_analysis_readiness_rpc_refuses_cross_account_identity(monkeypatch) -> None:
    authority = _Authority()
    authority.identity = AuthContext("principal-1", "other-creator", "agent")
    rpc = SessionRPC(authority, SimpleNamespace(creator_account_id="creator-1"))
    rpc.auth_ticket = "authenticated"
    monkeypatch.setattr(
        companion_session,
        "current_analysis_readiness",
        lambda store, identity: AnalysisReadiness("active", "admitted"),
    )

    with pytest.raises(CompanionRecordError):
        rpc.call("agent.analysis.readiness", {})
