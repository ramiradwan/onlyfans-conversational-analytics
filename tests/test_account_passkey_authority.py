"""Exercise the local authority fence with a synthetic Hello signature."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import saved_data, webauthn
from app.api.security import get_runtime_policy
from app.onboarding.account_passkeys import DesktopAccountPasskeys
from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.onboarding.admission_routes import admission_router
from app.persistence.auth import AuthenticationStateError, AuthorizedAccountBinding, SQLiteAuthenticationStore
from app.security.account_passkey import ACCESS_PROFILE, BINDING_PROFILE, WITHDRAWAL_PROFILE, AccountPasskeyRefused
from app.security.account_passkey_authority import AccountPasskeyAuthority
from app.security.account_passkey_enrollment import EnrollmentAttempt, RegisteredPasskey
from app.security.account_passkey_local import AccountPasskeyLocalRecords
from app.security.account_passkey_records import VerifiedPasskeyRecord, record_digest
from app.security.account_passkey_webauthn import NativeAssertion, SAVED_DATA_ORIGIN
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.saved_data import InitialSavedDataAccess, SavedDataSessions
from app.security.signed_objects import SignedObjectRejected, encode
from app.security.webauthn import WebAuthnAuthorityPort
from test_continuation_selection import add_candidate, enroll
from test_webauthn_routes import (
    ACCOUNT_ID, CREDENTIAL_ID, INSTANT, INSTALLATION_ID, INSTALLATION_KEY_JKT, ORIGIN, _seeded_store, _service,
)


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
RP = "passkey-fixture.dipsy.fi"
HANDLE = encode(b"\x7b" * 32)


class Hello:
    def __init__(self, key):
        self.key = key
        self.challenges = []

    def assert_local(self, *, rp_id, credential_id, challenge):
        assert (rp_id, credential_id) == (RP, CREDENTIAL_ID)
        assert challenge not in self.challenges
        self.challenges.append(challenge)
        raw = json.dumps({
            "type": "webauthn.get", "challenge": encode(challenge), "origin": SAVED_DATA_ORIGIN,
            "crossOrigin": False,
        }, separators=(",", ":")).encode("utf-8")
        auth = hashlib.sha256(RP.encode("ascii")).digest() + b"\x05" + bytes(4)
        signature = self.key.sign(auth + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256()))
        return NativeAssertion(CREDENTIAL_ID, encode(raw), encode(auth), encode(signature), HANDLE)


class VerifiedRecords:
    """The wire/verifier has its independent signed-vector tests."""
    sequence = 3

    def __init__(self, key):
        public = key.public_key().public_numbers()
        cose = (bytes.fromhex("a5010203262001215820") + public.x.to_bytes(32, "big")
                + bytes.fromhex("225820") + public.y.to_bytes(32, "big"))
        scope = {
            "sub": HANDLE, "installation_id": INSTALLATION_ID, "rp_id": RP, "credential_id": CREDENTIAL_ID,
            "installation_key_jkt": INSTALLATION_KEY_JKT,
        }
        binding_payload = {**scope, "profile": BINDING_PROFILE, "generation": 1, "credential_public_key_cose": encode(cose)}
        self.binding = VerifiedPasskeyRecord("fixture.binding.record", record_digest("fixture.binding.record"), binding_payload)
        initial = {**scope, "profile": ACCESS_PROFILE, "generation": 1, "binding_sha256": self.binding.digest,
                   "enrollment_id": "enrollment", "attempt_id": "attempt", "initial_access_nonce": "nonce",
                   "exp": int(INSTANT.timestamp()) + 300}
        self.initial = VerifiedPasskeyRecord("fixture.initial.record", record_digest("fixture.initial.record"), initial)
        withdrawn = {**scope, "profile": WITHDRAWAL_PROFILE, "generation": 2, "withdrawn_binding_sha256": self.binding.digest}
        self.withdrawal = VerifiedPasskeyRecord("fixture.withdrawal.record", record_digest("fixture.withdrawal.record"), withdrawn)
        self.fail_ack = True
        self.acks = 0
        self.after_guard = lambda: None

    def verify(self, compact, *, profile, scope, now):
        record = {BINDING_PROFILE: self.binding, ACCESS_PROFILE: self.initial, WITHDRAWAL_PROFILE: self.withdrawal}[profile]
        if profile == ACCESS_PROFILE and now >= record.payload["exp"]:
            raise SignedObjectRejected()
        assert compact == record.compact
        assert scope.subject == HANDLE
        assert (scope.installation_id, scope.installation_key_jkt, scope.credential_id, scope.rp_id) == (
            INSTALLATION_ID, INSTALLATION_KEY_JKT, CREDENTIAL_ID, RP,
        )
        return record

    def read_binding(self, registration, *, before_send):
        before_send()
        return self.binding

    def read_withdrawal(self, binding, *, before_send):
        before_send()
        assert binding == self.binding
        return self.withdrawal

    def acknowledge_withdrawal(self, withdrawal, *, require_applied):
        assert withdrawal == self.withdrawal
        require_applied(withdrawal)
        self.after_guard()
        self.acks += 1
        if self.fail_ack:
            raise HostedGrantUnavailable("Test withheld the acknowledgment reply")


@pytest.mark.parametrize(("elapsed", "source_lifetime", "changed_claim", "requests"), [
    (20, 480, None, 1),
    (20, 480, "verified_at", 1),
    (20, 480, "source_expires_at", 1),
    (300, 480, None, 0),
    (120, 120, None, 0),
])
def test_initial_access_must_use_the_original_enrollment_window(elapsed, source_lifetime, changed_claim, requests):
    binding = VerifiedRecords(ec.generate_private_key(ec.SECP256R1())).binding
    source_expires_at = INSTANT + timedelta(seconds=source_lifetime)
    attempt = EnrollmentAttempt(
        enrollment_id="0199b234-5678-7000-8000-000000000011", attempt_id=encode(b"x" * 32), rp_id=RP,
        expires_at=INSTANT + timedelta(seconds=60), source_expires_at=source_expires_at,
        installation_id=INSTALLATION_ID, installation_key_jkt=INSTALLATION_KEY_JKT,
        initial_access_nonce=encode(b"n" * 32), challenge=b"c" * 32, user_handle=b"{" * 32,
        display_label="Dipsy passkey 787878",
    )
    enrollment = RegisteredPasskey(attempt, CREDENTIAL_ID, b"unused", INSTANT, source_expires_at)
    payload = {
        name: binding.payload[name] for name in (
            "sub", "installation_id", "installation_key_jkt", "rp_id", "credential_id", "generation",
        )
    }
    payload.update(profile=ACCESS_PROFILE, binding_sha256=binding.digest, verified_at=int(INSTANT.timestamp()),
        source_expires_at=int(source_expires_at.timestamp()), enrollment_id=attempt.enrollment_id,
        attempt_id=attempt.attempt_id, initial_access_nonce=attempt.initial_access_nonce)
    if changed_claim is not None:
        payload[changed_claim] += 1
    compact = "fixture.access.record"
    access = VerifiedPasskeyRecord(compact, record_digest(compact), payload)
    exchange = Mock(return_value={"record": compact})
    records = AccountPasskeyLocalRecords(
        SimpleNamespace(exchange=exchange), SimpleNamespace(verify=Mock(return_value=access)),
        clock=lambda: INSTANT + timedelta(seconds=elapsed),
    )
    if changed_claim is None and requests:
        assert records.issue_initial_access(enrollment, binding, before_send=lambda: None) == access
    else:
        with pytest.raises(SignedObjectRejected):
            records.issue_initial_access(enrollment, binding, before_send=lambda: None)
    assert exchange.call_count == requests


@pytest.mark.parametrize("result", ["fresh", "receipt", "issue-uncertain", "verified-refusal", "stale-refusal", "generic-refusal"])
def test_setup_requires_an_uninterrupted_enrollment_and_sends_initial_issue_once(tmp_path, result):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    journey = str(uuid4())
    admissions = ScopedAdmissions(store, stage="setup")
    presentation = admissions.consume(admissions.prepare_desktop(journey), role="desktop")
    binding = {"purpose": "connect-account", "intended_creator_id": ACCOUNT_ID}
    row = {"binding_json": json.dumps(binding), "ceremony_id": "ceremony"}
    selection = {"operation_id": "parent", "projection": {
        "ceremony_id": "ceremony", "operation_id": "selected-operation",
        "decision": {"expires_at": (INSTANT + timedelta(seconds=300)).isoformat(timespec="milliseconds").replace("+00:00", "Z")},
    }}
    ceremonies = Mock()
    ceremonies._journey.return_value = journey
    ceremonies.guarded.return_value = Mock()
    ceremonies.row.return_value = row
    ceremonies.decisions._request.return_value = row, {
        "binding": binding, **{k: selection["projection"][k] for k in ("ceremony_id", "operation_id")},
    }
    attempt = SimpleNamespace(expires_at=INSTANT + timedelta(seconds=120), installation_id=INSTALLATION_ID)
    registered = SimpleNamespace(attempt=attempt)
    enrollment = Mock()
    enrollment.begin.return_value = attempt
    enrollment.finish.return_value = enrollment.receipt.return_value = registered
    if result == "receipt":
        enrollment.finish.side_effect = HostedGrantUnavailable("Test lost the enrollment response")
    authority = Mock(store=store)
    authority.prepare_initial_access.side_effect = (
        HostedGrantUnavailable("Test lost the first-access response") if result == "issue-uncertain" else None
    )
    owner = DesktopAccountPasskeys(ceremonies, enrollment, authority, admissions)
    effect = {"journey_id": journey}
    scope = {"creator_account_id": ACCOUNT_ID, "installation_id": INSTALLATION_ID}
    if result.endswith("-refusal"):
        reference = None if result == "generic-refusal" else HANDLE
        enrollment.finish.side_effect = AccountPasskeyRefused("authorization_denied", reference)
        if result == "stale-refusal":
            ceremonies.guarded.return_value.side_effect = [
                None, None, HostedGrantUnavailable("The passkey setup presentation changed"),
            ]
        if result == "verified-refusal":
            with pytest.raises(AccountPasskeyRefused) as refused:
                owner._create(journey, selection, session=presentation, authorize=Mock())
            assert refused.value.support_ref == reference
        else:
            assert owner._create(journey, selection, session=presentation, authorize=Mock()) == "unavailable"
        owner.adopt_after_connect(effect, scope, authorize=Mock())
        authority.adopt.assert_not_called()
        assert owner.take_initial_access() is None
        return
    assert owner._create(journey, selection, session=presentation, authorize=Mock()) == "created"
    assert authority.prepare_initial_access.call_count == 0
    owner.adopt_after_connect(effect, scope, authorize=Mock())
    owner.adopt_after_connect(effect, scope, authorize=Mock())
    assert authority.adopt.call_count == 1
    assert authority.prepare_initial_access.call_count == (0 if result == "receipt" else 1)
    first = owner.take_initial_access()
    assert first == ((presentation, authority.prepare_initial_access.return_value) if result == "fresh" else None)
    assert owner.take_initial_access() is None


@pytest.mark.parametrize("first_access", ["fresh", "expired", "restart"])
def test_installed_presentation_uses_only_one_initial_saved_data_session(tmp_path, monkeypatch, first_access):
    now = INSTANT
    path = tmp_path / "auth.sqlite3"
    store = _seeded_store(path, now)
    store._now = lambda: now
    enroll(store, installation=INSTALLATION_ID)
    association = add_candidate(store, creator=ACCOUNT_ID, approved=True, installation_id=INSTALLATION_ID,
                                organization_id="organization-http")
    store.record_authorized_account_binding(AuthorizedAccountBinding(
        creator_account_id=ACCOUNT_ID, installation_id=INSTALLATION_ID, platform_creator_id=ACCOUNT_ID,
        association_request_id=association, grant_bundle_sha256="a" * 64, authorized_at=now,
        grant_reference_ids=("installation_grant-http", "membership_snapshot-http", "creator_account_binding-http"),
    ))
    key = ec.generate_private_key(ec.SECP256R1())
    records = VerifiedRecords(key)
    hello = Hello(key)
    authority = AccountPasskeyAuthority(store, records, records, environment="qa", broker=hello)
    owner = authority.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None)
    setup = ScopedAdmissions(store, stage="setup")
    presentation = setup.consume(setup.prepare_desktop(None), role="desktop")
    staged = SavedDataSessions()
    staged.stage_initial(InitialSavedDataAccess(presentation.generation, owner, records.initial))

    setup.continue_in_runtime(presentation.identifier, presentation.generation)
    with pytest.raises(AdmissionRefused):
        setup.require(presentation.identifier, role="desktop", generation=presentation.generation)
    sessions = SavedDataSessions() if first_access == "restart" else staged
    if first_access == "expired":
        now += timedelta(seconds=300)
    app = FastAPI()
    app.state.desktop_admissions = ScopedAdmissions(store, stage="runtime")
    app.state.account_passkey_authority = authority
    app.include_router(admission_router(app.state.desktop_admissions, launcher_token="launcher-fixture".ljust(32, "a")))
    app.include_router(saved_data.router)
    app.dependency_overrides[saved_data.require_local_installation] = lambda: None
    monkeypatch.setattr(saved_data, "_sessions", sessions)
    with TestClient(app, base_url=ORIGIN.replace("http:", "https:"), headers={"Origin": ORIGIN}) as browser:
        headers = {"X-Desktop-Generation": presentation.generation}
        browser.cookies.set("__Host-desktop_presentation", presentation.identifier)
        session = browser.get("/api/v1/desktop/session", headers=headers)
        assert session.json()["stage"] == "runtime"
        assert session.json()["expires_at"] == presentation.expires_at
        headers["X-Desktop-CSRF"] = session.json()["csrf_token"]

        path = "/api/v1/saved-data/login/windows-hello"
        response = browser.post(path, headers=headers)
        assert response.status_code == 200
        assert sessions.accepts(owner, browser.cookies[saved_data.COOKIE], response.json()["csrf_token"])
        assert webauthn.settings.bridge_session_cookie_name not in browser.cookies
        challenges = 0 if first_access == "fresh" else 1
        assert len(hello.challenges) == challenges

        browser.post("/api/v1/saved-data/lock")
        assert browser.post(path, headers=headers).status_code == 200
        assert len(hello.challenges) == challenges + 1


def test_saved_data_binding_and_withdrawal_survive_restart(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    store = _seeded_store(path, INSTANT)
    enroll(store, installation=INSTALLATION_ID)
    association = add_candidate(
        store, creator=ACCOUNT_ID, approved=True, installation_id=INSTALLATION_ID,
        organization_id="organization-http",
    )
    key = ec.generate_private_key(ec.SECP256R1())
    records = VerifiedRecords(key)

    def authority(store):
        return AccountPasskeyAuthority(store, records, records, environment="qa", broker=Hello(key))

    setup = authority(store)
    with pytest.raises(AuthenticationStateError):
        setup.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None)

    store.record_authorized_account_binding(AuthorizedAccountBinding(
        creator_account_id=ACCOUNT_ID, installation_id=INSTALLATION_ID, platform_creator_id=ACCOUNT_ID,
        association_request_id=association, grant_bundle_sha256="a" * 64, authorized_at=INSTANT,
        grant_reference_ids=("installation_grant-http", "membership_snapshot-http", "creator_account_binding-http"),
    ))
    owner = setup.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None)
    assert setup.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None) == owner

    def runtime_application(authority, sessions):
        runtime_store = authority.store
        monkeypatch.setattr(saved_data, "_sessions", sessions)
        app = FastAPI()
        app.include_router(webauthn.router)
        app.include_router(saved_data.router)
        admissions = ScopedAdmissions(runtime_store, stage="runtime")
        app.state.desktop_admissions = admissions
        app.include_router(admission_router(admissions, launcher_token="private-fixture-token".ljust(32, "a")))
        app.dependency_overrides[webauthn.require_activated_runtime] = lambda: None
        app.dependency_overrides[saved_data.require_local_installation] = lambda: None
        app.dependency_overrides[webauthn.authentication_store] = lambda: runtime_store
        app.dependency_overrides[webauthn.webauthn_authority_port] = lambda: WebAuthnAuthorityPort(
            runtime_store, clock=runtime_store._now,
        )
        app.dependency_overrides[webauthn.webauthn_service] = lambda: _service(runtime_store)
        app.dependency_overrides[get_runtime_policy] = runtime_store.build_runtime_policy
        app.state.account_passkey_authority = authority
        return app

    def admit(browser):
        code = browser.app.state.desktop_admissions.prepare_desktop(None)
        activated = browser.post("/api/v1/desktop/entry/consume", json={"code": code})
        assert activated.status_code == 200
        session = activated.json()
        return {"X-Desktop-Generation": session["generation"], "X-Desktop-CSRF": session["csrf_token"]}

    base_url = ORIGIN.replace("http:", "https:")
    app = runtime_application(setup, SavedDataSessions())
    with TestClient(app, base_url=base_url, headers={"Origin": ORIGIN}) as browser:
        assert browser.post("/api/v1/webauthn/login/begin").json() == {"windows_hello": True}
        assert browser.post("/api/v1/webauthn/login/windows-hello").status_code == 401
        headers = admit(browser)
        logged_in = browser.post("/api/v1/webauthn/login/windows-hello", headers=headers)
        assert logged_in.status_code == 200
        assert webauthn.settings.bridge_session_cookie_name in browser.cookies

    # A new process has the signed binding but no sessions. All online grants
    # in the fixture are expired, so this proof can open saved data only.
    store = SQLiteAuthenticationStore(path, clock=lambda: INSTANT + timedelta(days=30))
    local = authority(store)
    sessions = SavedDataSessions()
    app = runtime_application(local, sessions)
    with TestClient(app, base_url=base_url, headers={"Origin": ORIGIN}) as browser:
        assert browser.post("/api/v1/webauthn/login/windows-hello").status_code == 403
        assert browser.post("/api/v1/saved-data/login/begin").json() == {"windows_hello": True}
        assert browser.post("/api/v1/saved-data/login/windows-hello").status_code == 401
        signed_in = browser.post("/api/v1/saved-data/login/windows-hello", headers=admit(browser))
        assert signed_in.status_code == 200
        assert webauthn.settings.bridge_session_cookie_name not in browser.cookies
        previous_token = browser.cookies[saved_data.COOKIE]
        previous_csrf = signed_in.json()["csrf_token"]
        assert sessions.accepts(owner, previous_token, previous_csrf)
    assert store.read_bridge_session(previous_token) is None

    store = SQLiteAuthenticationStore(path, clock=store._now)
    local = authority(store)
    sessions = SavedDataSessions()
    app = runtime_application(local, sessions)
    with TestClient(app, base_url=base_url, headers={"Origin": ORIGIN}) as browser:
        browser.cookies.set(saved_data.COOKIE, previous_token, path=saved_data.PREFIX)
        replay = browser.post("/api/v1/saved-data/view", headers={"X-CSRF-Token": previous_csrf})
        assert saved_data.COOKIE + "=" in replay.request.headers.get("cookie", "")
        assert replay.status_code == 403 and not local.broker.challenges
        signed_in = browser.post("/api/v1/saved-data/login/windows-hello", headers=admit(browser))
        assert signed_in.status_code == 200 and len(local.broker.challenges) == 1
        token = signed_in.cookies[saved_data.COOKIE]
        csrf = signed_in.json()["csrf_token"]
        assert token != previous_token and sessions.accepts(owner, token, csrf)

    def assert_closed():
        with store.database.read() as db:
            assert local.active(db) is None
        assert not sessions.accepts(owner, token, csrf)

    records.after_guard = assert_closed
    local.synchronize_withdrawals(sessions)
    assert records.acks == 1
    assert_closed()
    with pytest.raises(AuthenticationStateError):
        local.unlock(sessions, authorize=lambda connection: None, lifetime=120)
    with pytest.raises(AuthenticationStateError):
        local.adopt(None, creator_account_id=ACCOUNT_ID, guard=lambda connection=None: None)

    records.fail_ack = False
    restarted = authority(SQLiteAuthenticationStore(path, clock=store._now))
    with restarted.store.database.read() as db:
        pending = restarted.bindings.read(db, INSTALLATION_ID, RP, CREDENTIAL_ID)
        assert pending.binding_record is None and pending.pending_withdrawal == records.withdrawal.compact
    restarted.synchronize_withdrawals(SavedDataSessions())
    restarted.synchronize_withdrawals(SavedDataSessions())
    assert records.acks == 2
    with restarted.store.database.read() as db:
        assert restarted.active(db) is None
        record = restarted.bindings.read(db, INSTALLATION_ID, RP, CREDENTIAL_ID)
        assert record.binding_record is record.pending_withdrawal is None
        assert record.withdrawal_digest == records.withdrawal.digest and record.withdrawal_generation == 2

    records.sequence = 2
    with pytest.raises(AuthenticationStateError):
        authority(store)
