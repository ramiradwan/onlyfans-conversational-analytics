"""Activation return is ephemeral data, never local session authority."""
from datetime import timedelta
from contextlib import contextmanager
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.provisioning.activation_return import ActivationReturn, COOKIE, ENTRY_PATH, PATH
from app.provisioning.app import create_provisioning_app
from app.provisioning.session import PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from test_registered_continuation_context import owner as owner
from test_installation_continuation_owner import approved, grant

pytestmark = [pytest.mark.ci_tier("integration")]
HOSTED = "https://setup.example.invalid"
CONTINUATION = "clr1." + "A" * 43


@pytest.fixture
def case(owner):
    with activation_case(owner) as value:
        yield value


@contextmanager
def activation_case(owner, *, binding=True, initial=False):
    if initial:
        row = owner.journeys.open()
        journey = row["journey_id"]
        owner.sessions._create_session(journey)
        with owner.store.database.transaction() as connection:
            connection.execute("DELETE FROM provisioning_candidates")
        owner.journeys.update(journey, state="completing", scope_json=json.dumps({
            "organization_id": "organization-1", "installation_id": row["installation_id"],
            "installation_key_jkt": owner.store.installation_key_reference().installation_key_jkt,
            "intended_creator_id": "creator-1", "association_request_id": "initial-request",
            "onboarding_transaction_id": "initial-transaction"}))
    else:
        journey = approved(owner)
        if binding:
            owner.contexts.complete_binding(journey, grant(owner), membership_reference_id="membership")
    session = next(value for value in owner.sessions._sessions.values() if value.journey_id == journey)
    calls = []
    outcome = [CapabilityLicenseDeliveryReceipt("reference", "license", "issuance")]
    def redeem(**kwargs):
        calls.append(kwargs)
        return outcome[0]() if callable(outcome[0]) else outcome[0]
    app = create_provisioning_app(claim_submission=lambda **_: None, creator_association_initiation=lambda **_: None,
        creator_binding_acquisition=lambda: None, completion_ready=lambda: False, finalize_action=lambda **_: None,
        session_manager=owner.sessions, hosted_onboarding_url=HOSTED + "/public/onboarding/setup",
        capability_license_redemption=SimpleNamespace(redeem=redeem))
    with TestClient(app, base_url=PROVISIONING_ORIGIN) as browser:
        yield SimpleNamespace(owner=owner, journey=journey, session=session, calls=calls, outcome=outcome,
            redeem=redeem, browser=browser)


def stage(case, *, continuation=CONTINUATION, headers=None, data=None):
    return case.browser.post(ENTRY_PATH, data=data or {
        "journey_id": case.journey, "activation_continuation": continuation},
        headers={"Origin": HOSTED, "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", **(headers or {})},
        follow_redirects=False)


def local(case, response):
    return {"Cookie": f"{COOKIE}={response.cookies[COOKIE]}; {PROVISIONING_SESSION_COOKIE_NAME}={case.session.identifier}",
        "X-Onboarding-Journey": case.journey, "Origin": PROVISIONING_ORIGIN,
        "X-Provisioning-CSRF": case.session.csrf_token}


def test_return_uses_one_ephemeral_entry_and_existing_local_authority(case):
    result = stage(case)
    assert result.status_code == 303
    assert result.headers["location"] == "/provisioning#journey=" + case.journey
    assert CONTINUATION not in str(dict(result.headers)) + result.text
    assert "no-store" in result.headers["cache-control"] and result.headers["referrer-policy"] == "no-referrer"
    assert "HttpOnly" in result.headers["set-cookie"] and "Secure" in result.headers["set-cookie"]
    assert case.calls == []
    headers = local(case, result)
    context = case.browser.get(PATH, headers=headers)
    assert context.json()["state"] == "ready" and set(context.json()) == {"state", "journey_id", "entry_id"}
    headers["X-Onboarding-Activation-Entry"] = context.json()["entry_id"]
    dispatched = case.browser.post(PATH, json={}, headers=headers)
    assert dispatched.status_code == 200 and dispatched.json()["state"] == "checking"
    key = case.owner.store.installation_key_reference()
    assert case.calls == [{"continuation": CONTINUATION, "expected_scope": (
        "organization-1", "installation-1", key.installation_key_id, key.installation_key_jkt)}]
    assert case.browser.post(PATH, json={}, headers=headers).json() == dispatched.json()
    assert case.browser.get(PATH, headers=headers).json() == dispatched.json()
    replay = stage(case)
    assert replay.cookies[COOKIE] == result.cookies[COOKIE]
    assert case.browser.post(PATH, json={}, headers=headers).json() == dispatched.json()
    assert len(case.calls) == 1
    assert CONTINUATION not in dispatched.text + context.text


@pytest.mark.parametrize("initial", [False, True])
def test_return_waits_for_native_approval_without_creating_authority(owner, initial):
    from app.provisioning.events import events
    from test_continuation_selection import add_candidate
    with activation_case(owner, binding=False, initial=initial) as value:
        result = stage(value)
        assert result.status_code == 303
        headers = local(value, result)
        context = value.browser.get(PATH, headers=headers).json()
        assert context["state"] == "waiting" and context["entry_id"]
        headers["X-Onboarding-Activation-Entry"] = context["entry_id"]
        assert value.browser.post(PATH, json={}, headers=headers).json() == context
        assert value.calls == []
        assert not any(item.grant_type == "creator_account_binding" for item in owner.store.verified_grants())
        serial = events._commit_serial
        if initial:
            row = owner.journeys.require_current(value.journey)
            scope = json.loads(row["scope_json"])
            assert owner.store.provisioning_candidate(scope["association_request_id"]) is None
            add_candidate(owner.store, approved=True, association_request_id=scope["association_request_id"],
                installation_id=row["installation_id"], onboarding_transaction_id=scope["onboarding_transaction_id"])
            owner.journeys.update(value.journey, state="completed")
        else:
            owner.contexts.complete_binding(value.journey, grant(owner), membership_reference_id="membership")
        assert events._commit_serial > serial
        ready = value.browser.get(PATH, headers=headers).json()
        assert ready == {**context, "state": "ready"}
        assert value.browser.post(PATH, json={}, headers=headers).json()["state"] == "checking"
        assert len(value.calls) == 1


def test_waiting_entry_expiry_erases_secret_and_publishes_one_owner_wake(owner, monkeypatch):
    managers = []
    original = ActivationReturn._immutable_scope
    def capture(self, journey):
        managers.append(self)
        return original(self, journey)
    monkeypatch.setattr(ActivationReturn, "_immutable_scope", capture)
    with activation_case(owner, binding=False) as value:
        response = stage(value)
        manager = managers[-1]
        headers = local(value, response)
        entry = manager._entries[response.cookies[COOKIE]]
        from app.provisioning.events import events
        serial = events._commit_serial
        owner.clock[0] += timedelta(seconds=301)
        manager._expire_secret(response.cookies[COOKIE])
        assert events._commit_serial == serial + 1
        assert entry.continuation is None
        assert value.browser.get(PATH, headers=headers).json()["state"] == "unconfirmed"
        assert value.calls == []


@pytest.mark.parametrize("boundary", ["secret", "journey", "session"])
def test_final_blocking_scope_cannot_outlive_original_deadlines_or_session(case, monkeypatch, boundary):
    result = stage(case)
    headers = local(case, result)
    headers["X-Onboarding-Activation-Entry"] = case.browser.get(PATH, headers=headers).json()["entry_id"]
    original, entries = ActivationReturn._scope, []
    def delayed_scope(self, journey):
        value = original(self, journey)
        entries.append(self._entries[result.cookies[COOKIE]])
        if boundary == "session":
            with case.owner.store.database.transaction() as connection:
                connection.execute("DELETE FROM provisioning_browser_sessions")
        else:
            case.owner.clock[0] += timedelta(seconds=301 if boundary == "secret" else 1801)
        return value
    monkeypatch.setattr(ActivationReturn, "_scope", delayed_scope)
    response = case.browser.post(PATH, json={}, headers=headers)
    assert len(entries) == 1 and case.calls == []
    if boundary == "secret":
        assert response.status_code == 200 and response.json()["state"] == "unconfirmed"
    else:
        assert response.status_code in {401, 409}
    if boundary != "session":
        assert entries[0].continuation is None and entries[0].state == "unconfirmed"


def test_final_session_read_cannot_outlive_the_secret_deadline(case, monkeypatch):
    result = stage(case)
    headers = local(case, result)
    headers["X-Onboarding-Activation-Entry"] = case.browser.get(PATH, headers=headers).json()["entry_id"]
    original = ActivationReturn._scope
    def delay_authorization(self, journey):
        value = original(self, journey)
        authorize = self.authorize
        def delayed(*args):
            result = authorize(*args)
            case.owner.clock[0] += timedelta(seconds=301)
            return result
        self.authorize = delayed
        return value
    monkeypatch.setattr(ActivationReturn, "_scope", delay_authorization)
    response = case.browser.post(PATH, json={}, headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "unconfirmed"
    assert case.calls == []


def test_waiting_selection_revocation_erases_the_entry_instead_of_granting_approval(owner, monkeypatch):
    managers = []
    original = ActivationReturn._immutable_scope
    def capture(self, journey):
        managers.append(self)
        return original(self, journey)
    monkeypatch.setattr(ActivationReturn, "_immutable_scope", capture)
    with activation_case(owner, binding=False) as value:
        result = stage(value)
        headers = local(value, result)
        assert value.browser.get(PATH, headers=headers).json()["state"] == "waiting"
        entry = managers[-1]._entries[result.cookies[COOKIE]]
        with owner.store.database.transaction() as connection:
            connection.execute("UPDATE provisioning_candidates SET state='cancelled',resolved_at=? WHERE association_request_id='request-1'",
                (owner.clock[0].isoformat(),))
        assert value.browser.get(PATH, headers=headers).status_code == 409
        assert entry.continuation is None and entry.state == "unconfirmed"
        assert value.calls == []


@pytest.mark.parametrize("change", ["origin", "fetch-mode", "fetch-dest", "host", "unknown-journey", "extra", "noncanonical", "large"])
def test_unadmitted_return_never_runs_redemption(case, change):
    headers, data = {}, {"journey_id": case.journey, "activation_continuation": CONTINUATION}
    if change == "origin": headers["Origin"] = "https://unrelated.example"
    elif change == "fetch-mode": headers["Sec-Fetch-Mode"] = "cors"
    elif change == "fetch-dest": headers["Sec-Fetch-Dest"] = "iframe"
    elif change == "host": headers["Host"] = "unrelated.localhost:17871"
    elif change == "unknown-journey": data["journey_id"] = str(uuid4())
    elif change == "extra": data["return_url"] = "https://unrelated.example"
    elif change == "noncanonical": data["activation_continuation"] = "clr1." + "A" * 42 + "B"
    elif change == "large": data["activation_continuation"] = "A" * 513
    result = stage(case, headers=headers, data=data)
    assert result.status_code == 400 and COOKIE not in result.cookies
    assert case.calls == []


@pytest.mark.parametrize("change", ["cookie", "csrf", "journey", "entry", "body", "expired", "selection", "query"])
def test_local_activation_rechecks_session_scope_csrf_expiry_and_exact_entry(case, change):
    result = stage(case)
    headers = local(case, result)
    context = case.browser.get(PATH, headers=headers).json()
    headers["X-Onboarding-Activation-Entry"] = context["entry_id"]
    body, path = {}, PATH
    if change == "cookie": headers["Cookie"] = f"{COOKIE}={result.cookies[COOKIE]}"
    elif change == "csrf": headers["X-Provisioning-CSRF"] = "B" * 43
    elif change == "journey": headers["X-Onboarding-Journey"] = str(uuid4())
    elif change == "entry": headers["X-Onboarding-Activation-Entry"] = str(uuid4())
    elif change == "body": body = {"activation_continuation": CONTINUATION}
    elif change == "expired": case.owner.clock[0] += timedelta(seconds=301)
    elif change == "selection":
        with case.owner.store.database.transaction() as connection:
            connection.execute("UPDATE provisioning_candidates SET state='cancelled' WHERE association_request_id='request-1'")
    elif change == "query": path += "?other=true"
    response = case.browser.post(path, json=body, headers=headers)
    if change == "expired":
        assert response.status_code == 200 and response.json()["state"] == "unconfirmed"
    else:
        assert response.status_code in {400, 401, 403, 409}
    assert case.calls == []


def test_empty_startup_has_no_activation_and_staging_has_fixed_nonrenewable_expiry(case):
    headers = {"Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={case.session.identifier}", "X-Onboarding-Journey": case.journey}
    assert case.browser.get(PATH, headers=headers).json() == {"state": "none", "journey_id": case.journey, "entry_id": None}
    result = stage(case)
    entry_id = case.browser.get(PATH, headers=local(case, result)).json()["entry_id"]
    case.owner.clock[0] += timedelta(seconds=250)
    repeated = stage(case)
    assert repeated.cookies[COOKIE] == result.cookies[COOKIE] and "Max-Age=1550" in repeated.headers["set-cookie"]
    case.owner.clock[0] += timedelta(seconds=51)
    assert case.browser.get(PATH, headers=local(case, result)).json() == {
        "state": "unconfirmed", "journey_id": case.journey, "entry_id": entry_id}
    assert case.calls == []


def test_unknown_native_delivery_is_read_only_on_return_and_never_resubmitted(case):
    result = stage(case)
    headers = local(case, result)
    headers["X-Onboarding-Activation-Entry"] = case.browser.get(PATH, headers=headers).json()["entry_id"]
    case.outcome[0] = "hosted_unavailable"
    first = case.browser.post(PATH, json={}, headers=headers)
    assert first.json()["state"] == "unconfirmed"
    assert case.browser.get(PATH, headers=headers).json() == first.json()
    assert case.browser.post(PATH, json={}, headers=headers).json() == first.json()
    assert len(case.calls) == 1


def test_lost_reply_read_during_native_delivery_is_unknown_and_cannot_repeat(case):
    result = stage(case)
    headers = local(case, result)
    headers["X-Onboarding-Activation-Entry"] = case.browser.get(PATH, headers=headers).json()["entry_id"]
    started, release = Event(), Event()
    def delivering():
        started.set()
        assert release.wait(5)
        return "hosted_unavailable"
    case.outcome[0] = delivering
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(case.browser.post, PATH, json={}, headers=headers)
        try:
            assert started.wait(5)
            assert case.browser.get(PATH, headers=headers).json()["state"] == "unconfirmed"
            assert case.browser.post(PATH, json={}, headers=headers).json()["state"] == "unconfirmed"
            assert len(case.calls) == 1
        finally:
            release.set()
        assert pending.result().json()["state"] == "unconfirmed"


@pytest.mark.parametrize("identity", ["none", "wrong-creator", "creator"])
def test_runtime_return_requires_independent_current_creator_session_and_csrf(case, monkeypatch, identity):
    from app.api.endpoints import onboarding, capability_license
    from app.api.security import csrf_token
    from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
    onboarding._activation_return.cache_clear()
    monkeypatch.setattr(onboarding, "_store", lambda _: case.owner.store)
    monkeypatch.setattr("app.core.customer_release.load_customer_release_config",
        lambda: SimpleNamespace(hosted_onboarding_url=HOSTED + "/public/onboarding/setup"))
    monkeypatch.setattr("app.api.activation.require_activated_runtime", lambda: None)
    monkeypatch.setattr(capability_license, "_configured_redemption", lambda: SimpleNamespace(redeem=case.redeem))
    account = "other-creator" if identity == "wrong-creator" else "creator-1"
    policy = RuntimePolicy(None if identity == "none" else AuthContext("principal-1", account, "creator"), AuthorizationEpoch(1))
    monkeypatch.setattr(onboarding, "get_runtime_policy", lambda _: policy)
    app = FastAPI()
    app.dependency_overrides[onboarding.require_activated_runtime] = lambda: None
    app.include_router(onboarding.resume_router)
    try:
        with TestClient(app, base_url=PROVISIONING_ORIGIN) as browser:
            result = browser.post(ENTRY_PATH, data={"journey_id": case.journey, "activation_continuation": CONTINUATION},
                headers={"Origin": HOSTED, "Sec-Fetch-Mode": "navigate"}, follow_redirects=False)
            assert result.status_code == 303 and result.headers["location"] == "/#journey=" + case.journey
            headers = {"Cookie": f"{COOKIE}={result.cookies[COOKIE]}", "X-Onboarding-Journey": case.journey}
            context = browser.get(PATH, headers=headers)
            if identity != "creator":
                assert context.status_code == (401 if identity == "none" else 409)
                assert case.calls == []
                return
            assert context.json()["state"] == "ready"
            headers["X-Onboarding-Activation-Entry"] = context.json()["entry_id"]
            assert browser.post(PATH, json={}, headers={**headers, "Origin": PROVISIONING_ORIGIN}).status_code == 403
            headers.update({"Origin": PROVISIONING_ORIGIN, "X-CSRF-Token": csrf_token(policy)})
            assert browser.post(PATH, json={}, headers=headers).json()["state"] == "checking"
            assert len(case.calls) == 1
    finally:
        onboarding._activation_return.cache_clear()
