"""Fresh native continuation and closed HTTP dispatch on disposable SQLCipher."""
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.provisioning.app import create_provisioning_app
from app.provisioning.session import ProvisioningSessionManager, PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME, NATIVE_ENTRY_COOKIE_NAME
from test_registered_continuation_context import owner, request, context_request, selected, app_for, count, TOKEN, PATH

pytestmark = [pytest.mark.ci_tier("integration")]


def old_journey(owner, state="completed"):
    row = owner.journeys.open()
    owner.journeys.update(row["journey_id"], state=state, scope_json=json.dumps({
        "association_request_id": "request-1", "intended_creator_id": "creator-1", "organization_id": "organization-1",
        "onboarding_transaction_id": "source-transaction", "installation_id": "installation-1",
    }))
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET installation_id=? WHERE journey_id=?",
            ("installation-1", row["journey_id"]))
    owner.clock[0] += timedelta(minutes=31)
    return row["journey_id"]


@pytest.mark.parametrize("history", ["expired", "deleted"])
def test_bare_native_entry_offers_saved_target_before_retained_extension_launch(owner, history):
    prior = old_journey(owner)
    if history == "deleted":
        assert owner.journeys.get(prior) is None
        with owner.store.database.transaction() as connection:
            connection.execute("DELETE FROM onboarding_workspace_recovery WHERE previous_journey_id=?", (prior,))
    original_key = owner.store.installation_key_reference()
    native, incoming = request(owner)
    headers = {"Cookie": f"{NATIVE_ENTRY_COOKIE_NAME}={native.identifier}", "Origin": PROVISIONING_ORIGIN}
    with TestClient(app_for(owner), base_url=PROVISIONING_ORIGIN) as browser:
        offered = browser.get(PATH, headers=headers).json()
        headers["X-Provisioning-CSRF"] = offered["csrf_token"]
        # An ordinary pending extension launch cannot reopen an expired
        # journey or turn a UUID into authority.
        refused = browser.post(PATH, headers=headers, json={"journey_id": prior})
        assert refused.status_code == 409
        assert refused.json() == {"detail": "Saved setup is unavailable"}
        assert owner.key.calls == 0 and count(owner.store, "installation_continuation_contexts") == 0
        assert offered == {"state": "select_workspace", "entry_id": native.entry_id,
            "csrf_token": offered["csrf_token"], "continue_saved": True}
        admitted = browser.post(PATH, headers=headers, json={"journey_id": prior, "continue_saved": True})
        assert admitted.status_code == 200
        result = admitted.json()
        assert result["state"] == "selected" and result["previous_journey_id"] == prior
        assert result["journey_id"] != prior
        current = owner.contexts.require_current(result["journey_id"])
        assert current["scope"]["association_request_id"] == "request-1"
        cookie = headers["Cookie"] + f"; {PROVISIONING_SESSION_COOKIE_NAME}={admitted.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
        assert browser.get(PATH, headers={"Cookie": cookie}).json() == result
        assert browser.post(PATH, headers=headers, json={"journey_id": prior, "continue_saved": True}).status_code == 409
    assert owner.key.calls == 1 and owner.store.installation_key_reference() == original_key
    assert count(owner.store, "installation_continuation_contexts") == count(owner.store, "onboarding_native_selection_receipts") == 1


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


@pytest.mark.parametrize("prior", [None, "expired"])
def test_bare_saved_refusal_keeps_exact_offered_target_when_candidates_change(owner, prior):
    prior_id = old_journey(owner) if prior else None
    entry, incoming = request(owner)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    offered = entry.continuation_target
    owner.store.cancel_provisioning_candidate("request-1", resolved_at=owner.clock[0])
    from test_continuation_selection import add_candidate
    add_candidate(owner.store, "2")
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    assert entry.continuation_target is offered
    with pytest.raises(HTTPException) as refused:
        owner.sessions.select_native_workspace(incoming, prior_id, continue_saved=True)
    assert refused.value.status_code == 409
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


def test_bare_no_extension_saved_selection_has_no_prior_and_restores_exact_receipt(owner):
    entry, incoming = request(owner)
    offered = owner.sessions.native_entry_context(incoming)
    headers = {"Cookie": f"{NATIVE_ENTRY_COOKIE_NAME}={entry.identifier}", "Origin": PROVISIONING_ORIGIN,
        "X-Provisioning-CSRF": offered["csrf_token"]}
    with TestClient(app_for(owner), base_url=PROVISIONING_ORIGIN) as browser:
        reply = browser.post(PATH, headers=headers, json={"journey_id": None, "continue_saved": True})
        assert reply.status_code == 200
        result = reply.json()
        assert result == {"state": "selected", "journey_id": result["journey_id"]}
        identifier = reply.cookies[PROVISIONING_SESSION_COOKIE_NAME]
    restarted = ProvisioningSessionManager(TOKEN, journeys=owner.journeys, continuation_key=owner.key,
        monotonic=lambda: owner.clock[0].timestamp())
    assert restarted.native_entry_context(context_request(entry, identifier)) == result
    assert restarted.native_entry_context(context_request(entry)) == {"state": "unconfirmed"}
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None, continue_saved=True)
    assert owner.key.calls == 1
    assert count(owner.store, "onboarding_native_selection_receipts") == 1


@pytest.mark.parametrize("field", ["association_request_id", "intended_creator_id", "organization_id",
    "onboarding_transaction_id", "installation_id", "installation_key_id"])
def test_bare_saved_prior_mapping_cannot_contradict_offered_identity(owner, field):
    prior = old_journey(owner)
    _, incoming = request(owner)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    with owner.store.database.transaction() as connection:
        row = connection.execute("SELECT scope_json FROM onboarding_journeys WHERE journey_id=?", (prior,)).fetchone()
        scope = json.loads(row["scope_json"])
        scope[field] = "different"
        connection.execute("UPDATE onboarding_journeys SET scope_json=? WHERE journey_id=?", (json.dumps(scope), prior))
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("state", ["preparing", "prepare-unknown", "completing", "unknown"])
def test_bare_saved_offer_never_replaces_unresolved_initial_operation(owner, state, saved):
    old_journey(owner, state=state)
    _, incoming = request(owner)
    assert "continue_saved" not in owner.sessions.native_entry_context(incoming)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None, continue_saved=saved)
    assert owner.key.calls == 0
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


@pytest.mark.parametrize("state", ["new", "unknown"])
def test_bare_saved_offer_preserves_valid_initial_session(owner, state):
    original = owner.sessions._create_session(None)
    owner.journeys.update(original.journey_id, state=state)
    _, incoming = request(owner, session=original.identifier)
    assert "continue_saved" not in owner.sessions.native_entry_context(incoming)
    retained = owner.sessions.select_native_workspace(incoming, None)
    assert retained == original and owner.key.calls == 0
    assert count(owner.store, "installation_continuation_contexts") == 0


@pytest.mark.parametrize("failure", ["key-refused", "uncertain-created", "prior-changed", "entry-expired"])
def test_bare_saved_admission_rechecks_after_key_reopen(owner, failure):
    prior = old_journey(owner)
    entry, incoming = request(owner)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    def changed():
        if failure == "key-refused":
            from app.security.installation_key import InstallationKeyUnavailable
            raise InstallationKeyUnavailable("Fixture refusal")
        if failure == "entry-expired":
            owner.clock[0] += timedelta(seconds=301)
        elif failure == "uncertain-created":
            owner.journeys.update(prior, state="unknown")
        else:
            with owner.store.database.transaction() as connection:
                row = connection.execute("SELECT scope_json FROM onboarding_journeys WHERE journey_id=?", (prior,)).fetchone()
                scope = json.loads(row["scope_json"])
                scope["organization_id"] = "different"
                connection.execute("UPDATE onboarding_journeys SET scope_json=? WHERE journey_id=?", (json.dumps(scope), prior))
    owner.key.before_return = changed
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert not entry.consumed
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


def test_bare_ambiguous_saved_candidates_do_not_offer_or_admit(owner):
    owner.store.approve_provisioning_candidate("request-1", resolved_at=owner.clock[0])
    from test_continuation_selection import add_candidate
    add_candidate(owner.store, "2", approved=True)
    _, incoming = request(owner)
    assert "continue_saved" not in owner.sessions.native_entry_context(incoming)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None, continue_saved=True)
    assert owner.key.calls == 0 and count(owner.store, "onboarding_journeys") == 0


@pytest.mark.parametrize("retain", [False, True])
def test_saved_navigation_rechecks_protected_work_before_commit_and_rolls_back(owner, retain, monkeypatch):
    prior = old_journey(owner)
    existing = selected(owner)[1] if retain else None
    entry, incoming = request(owner, session=None if existing is None else existing.identifier)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    contexts = owner.sessions._continuations
    check = contexts.require_saved_native_target
    calls = []
    def changed(connection, target, previous):
        calls.append(previous)
        if len(calls) == 2:
            connection.execute("UPDATE onboarding_workspace_recovery SET state='unknown' WHERE previous_journey_id=?", (prior,))
        return check(connection, target, previous)
    monkeypatch.setattr(contexts, "require_saved_native_target", changed)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, prior, continue_saved=True)
    assert len(calls) == 2 and not entry.consumed
    assert count(owner.store, "installation_continuation_contexts") == int(retain)
    assert count(owner.store, "provisioning_browser_sessions") == int(retain)
    assert count(owner.store, "onboarding_native_selection_receipts") == int(retain)


def test_bare_saved_offer_cannot_be_downgraded_to_ordinary_null_selection(owner):
    _, incoming = request(owner)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None)
    assert owner.key.calls == 0


@pytest.mark.parametrize("when", ["key-reopen", "before-commit"])
def test_ordinary_null_registered_admission_rechecks_initial_uncertainty(owner, when, monkeypatch):
    prior = old_journey(owner)
    entry, incoming = request(owner)
    # Direct ordinary admission remains an existing API variant, even without
    # the read that offers a distinct saved selection.
    if when == "key-reopen":
        owner.key.before_return = lambda: owner.journeys.update(prior, state="unknown")
    else:
        contexts = owner.sessions._continuations
        check = contexts._saved_native_scope
        calls = []
        def changed(connection, previous):
            calls.append(previous)
            if len(calls) == 3:
                connection.execute("UPDATE onboarding_workspace_recovery SET state='unknown' WHERE previous_journey_id=?", (prior,))
            return check(connection, previous)
        monkeypatch.setattr(contexts, "_saved_native_scope", changed)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None)
    assert not entry.consumed
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


def test_elapsed_initial_recovery_deadline_does_not_permanently_block_saved_admission(owner):
    prior = old_journey(owner, state="unknown")
    owner.clock[0] += timedelta(minutes=30)
    entry, incoming = request(owner)
    assert owner.sessions.native_entry_context(incoming)["continue_saved"] is True
    # The read did not erase the old evidence or extend its original deadline.
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT state FROM onboarding_journeys WHERE journey_id=?", (prior,)).fetchone()[0] == "unknown"
    session = owner.sessions.select_native_workspace(incoming, None, continue_saved=True)
    assert session.journey_id != prior and entry.consumed
    assert owner.key.calls == 1
    assert count(owner.store, "installation_continuation_contexts") == 1
