"""Fresh native continuation and closed HTTP dispatch on disposable SQLCipher."""
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.provisioning.app import create_provisioning_app
from app.provisioning.session import ProvisioningSessionManager, PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME
from test_registered_continuation_context import owner, request, context_request, selected, app_for, count, TOKEN, PATH

pytestmark = [pytest.mark.ci_tier("integration")]


def old_journey(owner, state="completed"):
    row = owner.journeys.open()
    owner.journeys.update(row["journey_id"], state=state, scope_json=json.dumps({
        "association_request_id": "request-1", "intended_creator_id": "creator-1", "organization_id": "organization-1",
        "onboarding_transaction_id": "source-transaction", "installation_id": "installation-1",
    }))
    owner.clock[0] += timedelta(minutes=31)
    return row["journey_id"]


@pytest.mark.parametrize("history", ["expired", "deleted"])
def test_separate_server_offered_native_intent_keeps_saved_identity_and_receipt(owner, history):
    prior = old_journey(owner)
    if history == "deleted":
        assert owner.journeys.get(prior) is None
        with owner.store.database.transaction() as connection:
            connection.execute("DELETE FROM onboarding_workspace_recovery WHERE previous_journey_id=?", (prior,))
    native, incoming = request(owner, targeted=prior)
    offered = owner.sessions.native_entry_context(incoming)
    assert offered["continue_saved"] is True and offered["target_journey_id"] == prior
    assert owner.key.calls == 0
    session = owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    current = owner.contexts.require_current(session.journey_id)
    assert session.journey_id != prior and current["installation_id"] == "installation-1"
    assert current["scope"]["association_request_id"] == "request-1"
    restarted = ProvisioningSessionManager(TOKEN, journeys=owner.journeys, continuation_key=owner.key,
        monotonic=lambda: owner.clock[0].timestamp())
    assert restarted.native_entry_context(context_request(native, session.identifier)) == {
        "state": "selected", "journey_id": session.journey_id, "previous_journey_id": prior,
    }
    assert restarted.native_entry_context(context_request(native)) == {"state": "unconfirmed"}


def test_missing_previous_uuid_is_only_correlation_and_cannot_select_ambiguous_account(owner):
    owner.store.approve_provisioning_candidate("request-1", resolved_at=owner.clock[0])
    from test_continuation_selection import add_candidate
    add_candidate(owner.store, "2", approved=True)
    prior = str(uuid4())
    _, incoming = request(owner, targeted=prior)
    assert "continue_saved" not in owner.sessions.native_entry_context(incoming)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert count(owner.store, "onboarding_journeys") == 0 and owner.key.calls == 0


def test_browser_cannot_invent_saved_intent_or_convert_old_recover_request(owner):
    prior = old_journey(owner)
    _, incoming = request(owner, targeted=prior)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, recover=True)
    assert count(owner.store, "installation_continuation_contexts") == 0


def test_unknown_initial_operation_does_not_offer_registered_substitution(owner):
    prior = old_journey(owner, state="unknown")
    _, incoming = request(owner, targeted=prior)
    assert "continue_saved" not in owner.sessions.native_entry_context(incoming)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert owner.key.calls == 0


def test_live_registered_unknown_context_keeps_same_operation_on_saved_native_entry(owner):
    _, original = selected(owner)
    before = owner.contexts.begin_prepare(original.journey_id)
    _, incoming = request(owner, targeted=original.journey_id)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    session = owner.sessions.select_native_workspace(incoming, original.journey_id, continue_saved=True)
    after = owner.contexts.require_current(session.journey_id)
    assert session.journey_id == original.journey_id and after["operation_id"] == before["operation_id"]
    assert after["state"] == "prepare-unknown" and session.expires_at == original.expires_at


def test_changed_saved_proposal_refuses_at_admission_without_new_context(owner):
    prior = old_journey(owner)
    _, incoming = request(owner, targeted=prior)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    owner.store.cancel_provisioning_candidate("request-1", resolved_at=owner.clock[0])
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert count(owner.store, "installation_continuation_contexts") == 0


def test_continuation_http_uses_exact_session_owner_and_read_is_not_prepare(owner):
    _, session = selected(owner)
    calls = []
    async def prepare(journey):
        calls.append(("prepare", journey)); return {"state": "waiting", "journey_id": journey,
            "continuation_reference": "a" * 43, "hosted_start_url": "https://setup.example/public/onboarding/installation-continue"}
    async def read(journey):
        calls.append(("read", journey)); return {"state": "unknown", "journey_id": journey}
    service = SimpleNamespace(prepare=prepare, read_browser_entry=read)
    headers = {"Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}", "Origin": PROVISIONING_ORIGIN,
        "X-Provisioning-CSRF": session.csrf_token, "X-Onboarding-Journey": session.journey_id}
    with TestClient(app_for(owner, continuation=service), base_url=PROVISIONING_ORIGIN) as browser:
        status = browser.get("/api/v1/provisioning/status", headers=headers).json()
        assert status["context_kind"] == "registered-continuation" and status["continuation_state"] == "new"
        path = "/api/v1/provisioning/installation-continuation"
        assert browser.post(path, headers=headers, json={}).status_code == 200
        assert browser.get(path, headers=headers).status_code == 200
        assert calls == [("prepare", session.journey_id), ("read", session.journey_id)]
        for wrong in [{**headers, "X-Onboarding-Journey": str(uuid4())}, {**headers, "X-Provisioning-CSRF": "wrong"},
                      {**headers, "Origin": "https://elsewhere.example"}]:
            assert browser.post(path, headers=wrong, json={}).status_code == 403
        assert browser.post(path, headers=headers, json={"creator_account_id": "other"}).status_code == 422
        assert browser.get(path + "?x=y", headers=headers).status_code == 403
        assert len(calls) == 2


def test_http_finalizer_gets_trusted_journey_only_from_session(owner):
    _, session = selected(owner)
    calls = []
    app = create_provisioning_app(claim_submission=lambda **_: None, creator_association_initiation=lambda **_: None,
        creator_binding_acquisition=lambda: None, completion_ready=lambda: False, session_manager=owner.sessions,
        finalize_action=lambda **kwargs: calls.append(kwargs) or "account_approval_missing")
    headers = {"Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}", "Origin": PROVISIONING_ORIGIN,
        "X-Provisioning-CSRF": session.csrf_token}
    body = {"association_request_id": "request-1", "detected_creator_account_id": "creator-1"}
    with TestClient(app, base_url=PROVISIONING_ORIGIN) as browser:
        assert browser.post("/api/v1/provisioning/finalize", headers=headers,
            json={**body, "trusted_journey_id": str(uuid4())}).status_code == 422
        assert browser.post("/api/v1/provisioning/finalize", headers=headers, json=body).status_code == 409
    assert calls[0]["trusted_journey_id"] == session.journey_id
