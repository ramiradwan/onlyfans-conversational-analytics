"""The pilot summary is a passive read, including before a pack is downloaded."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session
from app.api.endpoints import paid_insights
from app.security.runtime_policy import RuntimeAuthorizationDenied


pytestmark = pytest.mark.ci_tier("fast")
RELEASE = SimpleNamespace(profile_sha256="d" * 64)
POLICY = SimpleNamespace(identity=SimpleNamespace(creator_account_id="example", role="creator"))
URL = "/api/v1/nlp/paid-insights"


@pytest.mark.parametrize(("state", "licensed", "expected"), [
    (None, False, (True, False, True, False)),
    ("available", False, (False, True, True, False)),
    ("reserved", False, (False, False, True, False)),
    ("spent", True, (False, False, False, True)),
])
def test_passive_status_only_reads_existing_authority_and_separate_eligibility(state, licensed, expected):
    authorities = Mock(spec_set=["preview_state", "read_current"])
    authorities.preview_state.return_value = state
    authorities.read_current.return_value = object() if licensed else None
    acquisition = Mock(spec_set=["status"])
    acquisition.status.return_value = {"eligible": True, "current": None}
    result = paid_insights._status_for(RELEASE, POLICY, authorities, acquisition)
    assert (result.can_acquire_preview, result.can_preview, result.can_purchase, result.can_process) == expected
    authorities.preview_state.assert_called_once_with(POLICY)
    authorities.read_current.assert_called_once_with(POLICY, kind="license")
    assert all(call.args[0] is POLICY for call in acquisition.status.call_args_list)


def test_selected_release_alone_grants_no_actions():
    result = paid_insights._status_for(RELEASE, POLICY, None, None)
    assert result.profile_sha256 == RELEASE.profile_sha256
    assert not any((result.can_acquire_preview, result.can_preview, result.can_purchase, result.can_process))


def test_a_refused_commercial_read_keeps_the_saved_report_entry_available():
    authorities = Mock(spec_set=["preview_state", "read_current"])
    authorities.preview_state.side_effect = RuntimeAuthorizationDenied("commercial_reference_unavailable")
    acquisition = Mock(spec_set=["status"])
    result = paid_insights._status_for(RELEASE, POLICY, authorities, acquisition)
    assert result == paid_insights.PaidInsightsStatus(profile_sha256=RELEASE.profile_sha256)
    acquisition.status.assert_not_called()


def test_http_status_requires_current_creator_and_rejects_account_override(monkeypatch):
    app = FastAPI()
    app.include_router(paid_insights.router)
    app.dependency_overrides[require_activated_runtime] = lambda: None
    def signed_out():
        raise HTTPException(401)
    app.dependency_overrides[get_authenticated_account_session] = signed_out
    snapshot = Mock(return_value=None)
    monkeypatch.setattr(paid_insights, "pilot_status", snapshot)
    with TestClient(app) as client:
        response = client.get(URL)
        assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
        snapshot.assert_not_called()
        app.dependency_overrides[get_authenticated_account_session] = lambda: POLICY
        assert client.get(URL, params={"account": "other"}).status_code == 422
        snapshot.assert_not_called()
        assert client.get(URL).status_code == 404
        snapshot.return_value = paid_insights._status_for(RELEASE, POLICY, None, None)
        response = client.get(URL)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json() == snapshot.return_value.model_dump()
        snapshot.side_effect = RuntimeAuthorizationDenied("account_changed")
        assert client.get(URL).status_code == 403
