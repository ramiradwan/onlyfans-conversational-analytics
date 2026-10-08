"""Creator rebuild requests reuse current account-bound analysis admission."""

from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.analysis_authorization import build_current_analysis_policy, clear_analysis_policies
from app.security.runtime_policy import AuthContext, RuntimeAuthorizationDenied
from app.services import insights_service
from tests.test_analysis_restart_offline import ACCOUNT, PRINCIPAL, _seed_durable_authority


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    _seed_durable_authority(path)
    store = SQLiteAuthenticationStore(path)
    clear_analysis_policies()
    build_current_analysis_policy(store, AuthContext(PRINCIPAL, ACCOUNT, "agent"))
    creator = store.build_runtime_policy(AuthContext(PRINCIPAL, ACCOUNT, "creator"))
    runtime = SimpleNamespace(
        scheduler=SimpleNamespace(canonical_revision=AsyncMock(return_value=7), schedule=AsyncMock()),
        questions=SimpleNamespace(discard=Mock()),
    )
    monkeypatch.setattr(settings, "auth_database_path", path)
    monkeypatch.setattr(insights_service, "analytics_runtime", lambda: runtime)
    yield store, creator, runtime
    clear_analysis_policies()


@pytest.mark.asyncio
async def test_creator_rebuild_revalidates_admitted_agent_authority(admitted):
    _store, creator, runtime = admitted
    await insights_service.rebuild_projection(creator)
    assert creator.identity.role == "creator"
    runtime.scheduler.canonical_revision.assert_awaited_once_with(ACCOUNT)
    runtime.scheduler.schedule.assert_awaited_once_with(ACCOUNT, 7, retry_failed=True, force=True)
    runtime.questions.discard.assert_called_once_with(ACCOUNT)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "revoked", "other_account"])
async def test_rebuild_refuses_missing_stale_or_foreign_analysis_admission(admitted, failure):
    store, creator, runtime = admitted
    if failure == "missing":
        clear_analysis_policies()
    elif failure == "revoked":
        store.revoke_authorized_account_binding(ACCOUNT)
    else:
        creator = store.build_runtime_policy(AuthContext(PRINCIPAL, "synthetic-other-owner", "creator"))
    with pytest.raises(RuntimeAuthorizationDenied):
        await insights_service.rebuild_projection(creator)
    runtime.scheduler.canonical_revision.assert_not_awaited()
    runtime.scheduler.schedule.assert_not_awaited()
    runtime.questions.discard.assert_not_called()


def test_rebuild_route_refuses_operator_session(admitted, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.activation import require_activated_runtime
    from app.api.dependencies import get_authenticated_account_session
    from app.api.endpoints import insights
    from app.api.security import csrf_token

    _store, creator, runtime = admitted
    operator = replace(creator, identity=replace(creator.identity, role="operator"))
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[require_activated_runtime] = lambda: None
    app.dependency_overrides[get_authenticated_account_session] = lambda: operator
    service = AsyncMock()
    monkeypatch.setattr(insights_service, "rebuild_projection", service)
    with TestClient(app) as client:
        response = client.post("/api/v1/insights/rebuild", json={},
                               headers={"x-csrf-token": csrf_token(operator)})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    service.assert_not_awaited()
    runtime.scheduler.schedule.assert_not_awaited()
