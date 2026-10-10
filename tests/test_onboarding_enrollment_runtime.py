"""Real SQLite ceremonies and stream orchestration; external hosted/TPM are fixtures."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from starlette.requests import Request

from app.launcher import parse_workspace_app_link
from app.persistence.auth import SQLiteAuthenticationStore, RevocationKey, RevocationScopeType
from app.persistence.onboarding import OnboardingJourneyStore
from app.provisioning.events import OnboardingEvents
from app.provisioning.session import ProvisioningSessionManager, PROVISIONING_SESSION_COOKIE_NAME
from app.security.initial_handoff import InitialHandoffClient, PROOF_PROFILE, canonical
from app.security.hosted_grants import TransportResponse
from app.security.hosted_grants import HostedGrantUnavailable
from app.provisioning.initial_handoff import InitialInstallationEnrollment
from app.security.webauthn import WebAuthnAuthorityPort, WebAuthnVerificationError
from test_webauthn_routes import _seeded_store, _service, _policy, INSTANT, PRINCIPAL_ID, CREDENTIAL_ID
from test_webauthn import registration_response

pytestmark = [pytest.mark.ci_tier("integration")]


def enrollment(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    service = _service(store)
    port = WebAuthnAuthorityPort(store, clock=lambda: INSTANT)
    authority = port.registration_authority(_policy(store)).authority
    options, context = service.begin_first_registration(authority)
    response = registration_response(ec.generate_private_key(ec.SECP256R1()), options["challenge"])
    return store, service, port, authority, context, response


def count(store, table):
    with store.database.read() as connection:
        return connection.execute("SELECT count(*) FROM " + table).fetchone()[0]


def test_first_ceremony_issues_one_session_and_never_replays_it(tmp_path):
    store, service, port, authority, context, response = enrollment(tmp_path)
    issued = service.complete_first_registration(authority, response, context=context,
        policy=_policy(store), authority_port=port)
    assert len(issued.csrf_value) == 43
    assert store.bridge_session_csrf_is_current(issued.policy, issued.csrf_value)
    assert store.read_bridge_session(issued.session_value) is not None
    assert count(store, "webauthn_credentials") == count(store, "bridge_sessions") == 1
    with pytest.raises(WebAuthnVerificationError):
        service.complete_first_registration(authority, response, context=context,
            policy=_policy(store), authority_port=port)
    assert count(store, "bridge_sessions") == 1


@pytest.mark.parametrize("failure", ["wrong-browser", "revoked", "stale-policy"])
def test_first_ceremony_rechecks_context_and_current_authority_at_commit(tmp_path, failure):
    store, service, port, authority, context, response = enrollment(tmp_path)
    policy = _policy(store)
    if failure == "wrong-browser":
        context = "other-browser"
    else:
        store.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, PRINCIPAL_ID))
        if failure == "revoked":
            policy = _policy(store)
    with pytest.raises(WebAuthnVerificationError):
        service.complete_first_registration(authority, response, context=context, policy=policy, authority_port=port)
    assert count(store, "webauthn_credentials") == count(store, "bridge_sessions") == 0


def test_first_ceremony_storage_failure_rolls_back_context_challenge_and_credential(tmp_path, monkeypatch):
    store, service, port, authority, context, response = enrollment(tmp_path)
    original = store._issue_bridge_session
    monkeypatch.setattr(store, "_issue_bridge_session", lambda *_: (_ for _ in ()).throw(RuntimeError("injected transaction failure")))
    with pytest.raises(RuntimeError):
        service.complete_first_registration(authority, response, context=context, policy=_policy(store), authority_port=port)
    assert count(store, "webauthn_credentials") == count(store, "bridge_sessions") == 0
    with store.database.read() as connection:
        assert connection.execute("SELECT consumed_at FROM first_enrollment_contexts").fetchone()[0] is None
        assert connection.execute("SELECT consumed_at FROM auth_challenges").fetchone()[0] is None
    monkeypatch.setattr(store, "_issue_bridge_session", original)
    service.complete_first_registration(authority, response, context=context, policy=_policy(store), authority_port=port)
    assert count(store, "bridge_sessions") == 1


def test_persisted_ui_session_survives_restart_without_extending_its_thirty_minutes(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    journeys = OnboardingJourneyStore(store)
    clock = [INSTANT.timestamp()]
    first = ProvisioningSessionManager("t" * 43, journeys=journeys, wall_clock=lambda: clock[0])
    session = first.redeem_handoff_code(first.issue_handoff_code("Provisioning " + "t" * 43))
    request = Request({"type": "http", "headers": [(b"host", b"bridge.localhost:17871"),
        (b"cookie", f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}".encode()),
        (b"origin", b"http://bridge.localhost:17871"), (b"x-provisioning-csrf", session.csrf_token.encode())]})
    restarted = ProvisioningSessionManager(None, journeys=OnboardingJourneyStore(
        SQLiteAuthenticationStore(store.database.path, clock=lambda: INSTANT)), wall_clock=lambda: clock[0])
    clock[0] += 301
    assert restarted.require_mutation(request).journey_id == session.journey_id
    clock[0] = INSTANT.timestamp() + 1800
    with pytest.raises(HTTPException):
        restarted.require_session(request)


@pytest.mark.asyncio
async def test_commit_during_initial_snapshot_is_drained_without_polling():
    owner = OnboardingEvents()
    journey = str(uuid4())
    facts = {name: "unknown" for name in ("installation", "enrollment", "pairing", "activation", "analysis")}
    reads = []
    def read():
        reads.append(1)
        result = owner.snapshot(journey_id=journey, facts=dict(facts))
        if len(reads) == 1:
            facts["installation"] = "verified"
            owner.publish()
        return result
    stream = owner.stream(read, lambda: None)
    first = await anext(stream)
    second = await asyncio.wait_for(anext(stream), 1)
    assert '"revision":0' in first
    assert '"revision":1' in second and '"installation":"verified"' in second
    assert len(reads) == 2
    await stream.aclose()


@pytest.mark.parametrize("suffix", ["", "&code=secret", "&journey=bad", "#secret"])
def test_app_link_is_only_a_non_authorizing_exact_journey(suffix):
    journey = str(uuid4())
    value = "ofca://onboarding?journey=" + journey + suffix
    if suffix:
        with pytest.raises(ValueError):
            parse_workspace_app_link(value)
    else:
        assert parse_workspace_app_link(value) == journey


def test_initial_key_client_uses_published_domain_and_body_proof():
    cases = json.loads((Path(__file__).parents[1] / "contracts/initial-installation-handoff-v2/proof-cases.json").read_text())
    record = cases[0]
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical(record["public_key"])).digest()).rstrip(b"=").decode()
    key_id = record["request"]["destination"]["installation_key"]["kid"]
    class Key:
        def ensure_ready(self):
            return SimpleNamespace(installation_key_id=key_id, installation_key_jkt=thumbprint)
        def sign_challenge(self, value):
            assert value.hex() == record["proof_bytes_hex"]
            return SimpleNamespace(installation_key_id=key_id, algorithm="ES256", signature=b"s" * 64)
    class Transport:
        def request(self, method, path, *, json_body):
            assert method == "POST" and "?" not in path
            assert json_body == {"profile": PROOF_PROFILE, "target": {"operation": "initial-handoff-prepare", "request": record["request"]}}
            value = {"profile": PROOF_PROFILE, "challenge": record["challenge"],
                     "request_digest": hashlib.sha256(canonical(record["request"])).hexdigest(),
                     "expires_at": (INSTANT + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            return TransportResponse(200, json.dumps(value).encode(), "application/json")
    envelope = InitialHandoffClient(Transport(), Key(), clock=lambda: INSTANT).envelope("prepare", record["request"])
    assert set(envelope) == {"request", "proof"}
    assert "claim_secret" not in json.dumps(envelope)


class _HandoffFixture:
    def __init__(self, failures=0):
        self.failures = failures
        self.preparations = []
        self.waits = []
        self.transport = object()
        self.key = SimpleNamespace(ensure_ready=lambda: SimpleNamespace(
            installation_key_id="ik1.AAECAwQFBgcICQoLDA0ODw", installation_key_jkt="A" * 43,
            public_key_jwk=json.dumps({"crv": "P-256", "kty": "EC", "x": "A" * 43, "y": "A" * 43})))

    def prepare(self, request, *, before_send=None):
        if before_send is not None:
            before_send()
        self.preparations.append(json.loads(json.dumps(request)))
        if self.failures:
            self.failures -= 1
            raise HostedGrantUnavailable("fixture response lost")
        return {"reference": "B" * 43, "operation_id": request["operation_id"],
                "expires_at": (INSTANT + timedelta(minutes=10)).isoformat()}

    def envelope(self, operation, request):
        self.waits.append((operation, request))
        return {"request": request}


class _WaitingStream:
    async def events(self, envelope):
        await asyncio.Event().wait()
        yield {}


def _enrollment_worker(store, client):
    return InitialInstallationEnrollment(store, hosted_origin="https://fixture.invalid",
        hosted_start_url="https://fixture.invalid/public/onboarding/setup/start", client=client,
        stream=_WaitingStream(), grants=SimpleNamespace(close=lambda: None),
        continuity=object(), continuity_stream=_WaitingStream())


@pytest.mark.asyncio
async def test_lost_prepare_response_retries_same_exact_operation_with_new_client_proof(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture(failures=1)
    worker = _enrollment_worker(store, client)
    journey = worker.journeys.open()["journey_id"]
    result = await worker.prepare(journey)
    assert result["state"] == "waiting"
    assert len(client.preparations) == 2 and client.preparations[0] == client.preparations[1]
    assert worker.journeys.get(journey)["state"] == "waiting"
    await worker.stop()


@pytest.mark.asyncio
async def test_prepare_unknown_after_restart_reuses_durable_exact_request(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    unavailable = _HandoffFixture(failures=3)
    first = _enrollment_worker(store, unavailable)
    journey = first.journeys.open()["journey_id"]
    with pytest.raises(HostedGrantUnavailable):
        await first.prepare(journey)
    assert first.journeys.get(journey)["state"] == "prepare-unknown"
    assert len(unavailable.waits) == 0
    await first.stop()
    ready = _HandoffFixture()
    restarted = _enrollment_worker(SQLiteAuthenticationStore(store.database.path, clock=lambda: INSTANT), ready)
    assert (await restarted.prepare(journey))["state"] == "waiting"
    assert ready.preparations == [unavailable.preparations[0]]
    await restarted.stop()


@pytest.mark.asyncio
async def test_opening_stream_before_prepare_cannot_send_unregistered_wait(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    journey = worker.journeys.open()["journey_id"]
    worker.resume(journey)
    await asyncio.sleep(0)
    assert client.preparations == client.waits == []
    await worker.stop()


@pytest.mark.asyncio
async def test_lost_wait_never_attempts_registered_receipt(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    class Disconnected:
        async def events(self, envelope):
            raise HostedGrantUnavailable("fixture disconnected")
            yield {}
    worker.stream = Disconnected()
    journey = worker.journeys.open()["journey_id"]
    await worker.prepare(journey)
    await worker._tasks[journey]
    assert worker.journeys.get(journey)["state"] == "waiting"
    assert [operation for operation, _ in client.waits] == ["wait", "wait", "wait"]
    await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["authentication-required", "confirmation-required"])
@pytest.mark.parametrize("boundary", ["wait", "complete"])
async def test_owner_session_and_confirmation_refusals_keep_exact_pending_operation(tmp_path, status, boundary):
    from app.security.initial_handoff import InitialHandoffRefused
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    journey = worker.journeys.open()["journey_id"]
    row = worker.journeys.get(journey)
    completions = []
    def complete(request):
        completions.append(request)
        raise InitialHandoffRefused(status.replace("-", "_"))
    def receipt(request):
        pytest.fail("An explicitly refused unregistered completion must not use registered receipt recovery")
    client.complete, client.receipt = complete, receipt
    class RefusingStream:
        attempts = 0
        async def events(self, envelope):
            self.attempts += 1
            if boundary == "complete" and self.attempts == 1:
                yield {"status": "authorized", "revision": 1, "scope": {
                    "installation_id": row["installation_id"], "installation_key_jkt": "A" * 43}}
            else:
                yield {"status": status, "revision": self.attempts + 1}
    worker.stream = RefusingStream()
    await worker.prepare(journey)
    await worker._tasks[journey]
    saved = worker.journeys.get(journey)
    assert saved["state"] == "waiting" and saved["reason"] == status
    assert len(client.preparations) == 1
    assert len(completions) == (1 if boundary == "complete" else 0)
    assert len(client.waits) == 3
    assert all(request == client.waits[0][1] for _, request in client.waits)
    assert saved["operation_id"] == client.preparations[0]["operation_id"]
    await worker.stop()


@pytest.mark.asyncio
async def test_restart_unknown_completion_reconciles_without_replay(tmp_path, monkeypatch):
    from app.security.initial_handoff import PROFILE
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    journey = worker.journeys.open()["journey_id"]
    worker.journeys.update(journey, state="unknown", operation_id=worker.journeys.new_operation(),
        prepare_json=json.dumps({"profile": PROFILE}))
    recoveries, resumed = [], []
    async def recovery(row):
        recoveries.append(row["operation_id"])
        worker.journeys.update(journey, state="completed")
    async def continuity(row):
        resumed.append(row["state"])
    monkeypatch.setattr(worker, "_recover", recovery)
    monkeypatch.setattr(worker, "_continuity", continuity)
    await worker._run(journey)
    assert recoveries == [worker.journeys.get(journey)["operation_id"]]
    assert resumed == ["completed"]
    assert client.preparations == client.waits == []
    await worker.stop()


def test_launcher_journey_is_bound_to_consumed_handoff_not_latest_draft(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    journeys = OnboardingJourneyStore(store)
    intended = journeys.open()["journey_id"]
    journeys.open(str(uuid4()))
    sessions = ProvisioningSessionManager("t" * 43, journeys=journeys)
    code = sessions.issue_handoff_code("Provisioning " + "t" * 43, journey_id=intended)
    assert sessions.redeem_handoff_code(code).journey_id == intended
    with pytest.raises(HTTPException):
        sessions.redeem_handoff_code(code)


def test_targeted_native_bootstrap_strips_code_before_session_selection_and_binds_its_journey(tmp_path):
    from fastapi.testclient import TestClient
    from app.provisioning.app import create_provisioning_app
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    journeys = OnboardingJourneyStore(store)
    intended = str(uuid4())
    sessions = ProvisioningSessionManager("t" * 43, journeys=journeys)
    application = create_provisioning_app(claim_submission=lambda **_: None,
        creator_association_initiation=lambda **_: None, creator_binding_acquisition=lambda: None,
        completion_ready=lambda: False, finalize_action=lambda **_: None,
        session_manager=sessions, extension_id="a" * 32)
    with TestClient(application, base_url="http://bridge.localhost:17871") as browser:
        from app.provisioning.session import NATIVE_ENTRY_COOKIE_NAME
        code = sessions.issue_native_entry("Provisioning " + "t" * 43, journey_id=intended)
        response = browser.get("/provisioning/native-entry", params={"code": code}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/provisioning/native-return#journey=" + intended
        assert code not in response.headers["location"]
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert PROVISIONING_SESSION_COOKIE_NAME not in response.cookies
        assert browser.get("/provisioning/native-entry", params={"code": code}).status_code == 401
        assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 0
        headers = {"Cookie": f"{NATIVE_ENTRY_COOKIE_NAME}={response.cookies[NATIVE_ENTRY_COOKIE_NAME]}"}
        context = browser.get("/api/v1/provisioning/native-entry", headers=headers).json()
        headers.update({"Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]})
        refused = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": str(uuid4())}, headers=headers)
        assert refused.status_code == 409
        assert "set-cookie" not in refused.headers
        assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 0
        selected = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": intended}, headers=headers)
        assert selected.status_code == 200 and selected.json() == {"state": "selected", "journey_id": intended}
        assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 1


def _native_entry_browser(tmp_path):
    from fastapi.testclient import TestClient
    from app.provisioning.app import create_provisioning_app
    from app.provisioning.session import NATIVE_ENTRY_COOKIE_NAME
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    clock = [10.0]
    sessions = ProvisioningSessionManager("t" * 43, journeys=OnboardingJourneyStore(store),
        monotonic=lambda: clock[0])
    application = create_provisioning_app(claim_submission=lambda **_: None,
        creator_association_initiation=lambda **_: None, creator_binding_acquisition=lambda: None,
        completion_ready=lambda: False, finalize_action=lambda **_: None,
        session_manager=sessions, extension_id="a" * 32)
    browser = TestClient(application, base_url="http://bridge.localhost:17871")
    response = browser.post("/api/v1/provisioning/handoff", headers={
        "Authorization": "Provisioning " + "t" * 43, "X-Onboarding-Native-Entry": "discover"})
    assert response.status_code == 200
    code = response.json()["handoff_code"]
    assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 0
    redeemed = browser.get("/provisioning/native-entry", params={"code": code}, follow_redirects=False)
    assert redeemed.status_code == 303 and redeemed.headers["location"] == "/provisioning/native-return"
    assert PROVISIONING_SESSION_COOKIE_NAME not in redeemed.cookies
    cookie = f"{NATIVE_ENTRY_COOKIE_NAME}={redeemed.cookies[NATIVE_ENTRY_COOKIE_NAME]}"
    assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 0
    context = browser.get("/api/v1/provisioning/native-entry", headers={"Cookie": cookie}).json()
    return store, sessions, browser, clock, code, cookie, context


def test_manual_native_entry_selects_once_after_discovery_without_creating_an_extra_draft(tmp_path):
    store, sessions, browser, _, code, cookie, context = _native_entry_browser(tmp_path)
    intended = str(uuid4())
    headers = {"Cookie": cookie, "Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]}
    response = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": intended}, headers=headers)
    assert response.status_code == 200 and response.json() == {"state": "selected", "journey_id": intended}
    assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 1
    assert browser.post("/api/v1/provisioning/native-entry", json={"journey_id": intended}, headers=headers).status_code == 409
    assert browser.get("/provisioning/native-entry", params={"code": code}).status_code == 401
    assert count(store, "provisioning_browser_sessions") == 1
    # Losing Set-Cookie leaves an unknown result, not another session issue.
    assert browser.get("/api/v1/provisioning/native-entry", headers={"Cookie": cookie}).json() == {"state": "unconfirmed"}
    session_cookie = response.cookies[PROVISIONING_SESSION_COOKIE_NAME]
    confirmed = browser.get("/api/v1/provisioning/native-entry", headers={"Cookie": cookie + f"; {PROVISIONING_SESSION_COOKIE_NAME}={session_cookie}"})
    assert confirmed.json() == {"state": "selected", "journey_id": intended}


def test_native_discovery_cannot_replace_an_existing_session_or_protected_journey(tmp_path):
    store, sessions, browser, _, _, cookie, context = _native_entry_browser(tmp_path)
    existing_journey = OnboardingJourneyStore(store).open()["journey_id"]
    OnboardingJourneyStore(store).update(existing_journey, state="waiting")
    existing = sessions.redeem_handoff_code(sessions.issue_handoff_code("Provisioning " + "t" * 43, journey_id=existing_journey))
    original = OnboardingJourneyStore(store).session(existing.identifier)
    headers = {"Cookie": cookie + f"; {PROVISIONING_SESSION_COOKIE_NAME}={existing.identifier}",
        "Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]}
    refused = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": str(uuid4())}, headers=headers)
    assert refused.status_code == 409 and "set-cookie" not in refused.headers
    assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 1
    kept = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": existing_journey}, headers=headers)
    assert kept.status_code == 200 and kept.cookies[PROVISIONING_SESSION_COOKIE_NAME] == existing.identifier
    assert OnboardingJourneyStore(store).session(existing.identifier) == original
    assert OnboardingJourneyStore(store).get(existing_journey)["state"] == "waiting"


def test_native_entry_rechecks_expiry_after_durable_write_lock_and_rolls_back_session(tmp_path, monkeypatch):
    from contextlib import contextmanager
    store, _, browser, clock, _, cookie, context = _native_entry_browser(tmp_path)
    intended = OnboardingJourneyStore(store).open()["journey_id"]
    original = store.database.transaction
    writes = [0]
    @contextmanager
    def expires_at_write_lock(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            writes[0] += 1
            if writes[0] == 1:
                clock[0] += 300
            yield connection
    monkeypatch.setattr(store.database, "transaction", expires_at_write_lock)
    response = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": intended}, headers={
        "Cookie": cookie, "Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]})
    assert response.status_code == 401
    assert count(store, "provisioning_browser_sessions") == 0


def test_native_entry_expiry_at_final_insert_check_rolls_back_durable_and_memory_session(tmp_path, monkeypatch):
    store, sessions, browser, clock, _, cookie, context = _native_entry_browser(tmp_path)
    intended = OnboardingJourneyStore(store).open()["journey_id"]
    original = sessions._journeys.record_session
    def expired_after_insert(**arguments):
        authorize = arguments.pop("authorize")
        checks = [0]
        def guard():
            checks[0] += 1
            if checks[0] == 2:
                clock[0] += 300
            authorize()
        return original(**arguments, authorize=guard)
    monkeypatch.setattr(sessions._journeys, "record_session", expired_after_insert)
    response = browser.post("/api/v1/provisioning/native-entry", json={"journey_id": intended}, headers={
        "Cookie": cookie, "Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]})
    assert response.status_code == 401
    assert count(store, "provisioning_browser_sessions") == 0
    assert sessions._sessions == {}


@pytest.mark.parametrize("failure", ["expiry", "origin", "csrf", "host", "invalid-journey", "extra-field"])
def test_native_entry_refusals_create_no_session_or_journey(tmp_path, failure):
    store, _, browser, clock, _, cookie, context = _native_entry_browser(tmp_path)
    headers = {"Cookie": cookie, "Origin": "http://bridge.localhost:17871", "X-Provisioning-CSRF": context["csrf_token"]}
    body = {"journey_id": str(uuid4())}
    if failure == "expiry": clock[0] += 300
    if failure == "origin": headers["Origin"] = "https://example.test"
    if failure == "csrf": headers["X-Provisioning-CSRF"] = "wrong"
    if failure == "host": headers["Host"] = "evil.example"
    if failure == "invalid-journey": body["journey_id"] = "invalid"
    if failure == "extra-field": body["creator"] = "untrusted"
    assert browser.post("/api/v1/provisioning/native-entry", json=body, headers=headers).status_code in {400, 401, 403, 421, 422}
    assert count(store, "onboarding_journeys") == count(store, "provisioning_browser_sessions") == 0


def test_durable_session_reconciliation_does_not_accept_launcher_identity(tmp_path):
    store, service, port, authority, context, response = enrollment(tmp_path)
    assert not store.bridge_session_is_current(_policy(store))
    issued = service.complete_first_registration(authority, response, context=context,
        policy=_policy(store), authority_port=port)
    assert store.bridge_session_is_current(issued.policy)
    store.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, PRINCIPAL_ID))
    assert not store.bridge_session_is_current(issued.policy)


_CONTINUITY_CASES = [case for case in json.loads((Path(__file__).parents[1] /
    "contracts/onboarding-continuity-v1/schema-cases.json").read_text())
    if case["schema"].endswith("hosted-snapshot.schema.json")]


@pytest.mark.parametrize("case", _CONTINUITY_CASES, ids=lambda case: case["case_id"])
def test_production_continuity_validator_matches_published_snapshots(case):
    from app.security.onboarding_continuity import validated_event, PROFILE as HOSTED_PROFILE
    value = case["value"]
    event = {"profile": HOSTED_PROFILE, "kind": "snapshot", "snapshot": value}
    if case["valid"]:
        assert validated_event(event, transaction_id="journey-fixture") == value
    else:
        with pytest.raises(HostedGrantUnavailable):
            validated_event(event, transaction_id="journey-fixture")


@pytest.mark.asyncio
async def test_live_workspace_focus_is_bounded_to_journey_without_new_revision():
    owner = OnboardingEvents()
    journey, unrelated = str(uuid4()), str(uuid4())
    facts = {name: "unknown" for name in ("installation", "enrollment", "pairing", "activation", "analysis")}
    read = lambda: owner.snapshot(journey_id=journey, facts=facts)
    stream = owner.stream(read, lambda: None, journey_id=journey)
    await anext(stream)
    assert owner.request_focus(unrelated) is None
    assert owner.request_focus(journey) == journey
    focused = await asyncio.wait_for(anext(stream), 1)
    assert focused == 'event: workspace-focus\ndata: {"journey_id":"' + journey + '"}\n\n'
    assert read()["revision"] == 0
    await stream.aclose()
    assert owner.request_focus(journey) is None


@pytest.mark.asyncio
async def test_known_expiry_wakes_snapshot_without_repeating_status_reads():
    owner = OnboardingEvents()
    journey = str(uuid4())
    expires = None
    reads = []
    def read():
        nonlocal expires
        now = asyncio.get_running_loop().time()
        if expires is None:
            expires = now + 0.02
        reads.append(1)
        return owner.snapshot(journey_id=journey, facts={"installation": "verified" if now < expires else "unknown"})
    def remaining():
        if len(reads) > 1:
            return None
        return max(0.0, expires - asyncio.get_running_loop().time())
    stream = owner.stream(read, lambda: None, expires_in=remaining)
    first = await anext(stream)
    assert '"installation":"verified"' in first
    async with asyncio.timeout(1):
        while True:
            result = await anext(stream)
            if result.startswith("event: onboarding"):
                break
    assert '"installation":"unknown"' in result and len(reads) == 2
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("admission-refused",1),("closed-after-snapshot",1),("transport-unavailable",0)])
async def test_stream_denial_or_closure_requests_one_signed_reconciliation(tmp_path,monkeypatch,mode,expected):
    from app.security.initial_handoff import OnboardingStreamAdmissionRefused
    from app.security.onboarding_continuity import PROFILE as HOSTED_PROFILE
    store=_seeded_store(tmp_path/"auth.sqlite3",INSTANT)
    worker=_enrollment_worker(store,_HandoffFixture())
    snapshot=json.loads(json.dumps(next(value["value"] for value in _CONTINUITY_CASES if value["valid"])))
    snapshot["facts"]["association"]="pending"
    snapshot["facts"]["authorization"]="current"
    scope={"onboarding_transaction_id":snapshot["onboarding_transaction_id"],"organization_id":"organization-1","installation_id":"installation-1"}
    row={"scope_json":json.dumps(scope)}
    monkeypatch.setattr(store,"consumed_claim_submissions",lambda:(object(),))
    monkeypatch.setattr(worker,"_candidate",lambda value:None)
    calls=[]
    def refresh(reference):
        calls.append(reference)
        raise HostedGrantUnavailable("Signed reconciliation response unavailable")
    worker.grants=SimpleNamespace(refresh_reference=refresh,close=lambda:None)
    monkeypatch.setattr(store,"verified_grants",lambda:(SimpleNamespace(installation_id="installation-1",organization_id="organization-1",valid_from=INSTANT-timedelta(minutes=1),expires_at=INSTANT+timedelta(minutes=1),reference_id="current-grant"),))
    worker.continuity=SimpleNamespace(envelope=lambda value:{"request":value})
    class Stream:
        async def events(self,envelope):
            if mode=="admission-refused":
                raise OnboardingStreamAdmissionRefused("refused")
            if mode=="transport-unavailable":
                raise HostedGrantUnavailable("unknown")
            yield {"profile":HOSTED_PROFILE,"kind":"snapshot","snapshot":snapshot}
    worker.continuity_stream=Stream()
    before=count(store,"auth_revocation_bindings")
    await worker._continuity(row)
    assert calls==["current-grant"]*expected
    assert count(store,"auth_revocation_bindings")==before
    await worker.stop()


def _continuity_diagnostic_fixture(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    worker = _enrollment_worker(store, _HandoffFixture())
    snapshot = json.loads(json.dumps(next(value["value"] for value in _CONTINUITY_CASES if value["valid"])))
    snapshot["facts"].update(association="pending", authorization="current")
    scope = {"onboarding_transaction_id": snapshot["onboarding_transaction_id"],
             "organization_id": "organization-fixture", "installation_id": "installation-fixture",
             "association_request_id": "0199b234-5678-7000-8000-000000000002", "intended_creator_id": "creator-fixture"}
    monkeypatch.setattr(store, "consumed_claim_submissions", lambda: (object(),))
    monkeypatch.setattr(worker, "_candidate", lambda _: None)
    async def refresh(_):
        pass
    monkeypatch.setattr(worker, "_refresh_current_grants", refresh)
    return worker, {"scope_json": json.dumps(scope)}, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,reason", [
    ("challenge_envelope", "unexpected_failure"),
    ("stream_admission", "admission_refused"),
    ("stream_frame", "hosted_unavailable"),
    ("association_request", "association_refused"),
    ("binding_acquisition", "installation_key_unavailable"),
    ("authority_refresh", "grant_verification_refused"),
])
async def test_continuity_diagnostics_keep_closed_stage_codes_without_error_content(
        tmp_path, monkeypatch, caplog, stage, reason):
    from app.persistence.auth import ProvisioningCandidateState
    from app.security.hosted_grants import CreatorAssociationRefused, GrantVerificationRefused
    from app.security.initial_handoff import OnboardingStreamAdmissionRefused
    from app.security.installation_key import InstallationKeyUnavailable
    from app.security.onboarding_continuity import PROFILE as HOSTED_PROFILE

    worker, row, snapshot = _continuity_diagnostic_fixture(tmp_path, monkeypatch)
    secret = "fixture-challenge-key-cookie-session-must-not-be-logged"
    class OpaqueFailure(RuntimeError):
        def __str__(self):
            raise AssertionError("Diagnostics must not format the exception")
        def __repr__(self):
            raise AssertionError("Diagnostics must not inspect the exception")
    errors = {
        "challenge_envelope": OpaqueFailure(secret),
        "stream_admission": OnboardingStreamAdmissionRefused(secret),
        "stream_frame": HostedGrantUnavailable(secret),
        "association_request": CreatorAssociationRefused(secret),
        "binding_acquisition": InstallationKeyUnavailable(secret),
        "authority_refresh": GrantVerificationRefused(secret),
    }
    attempts, actions = [], []
    def fail(*_, **__):
        actions.append(stage)
        raise errors[stage]
    def envelope(request):
        attempts.append(request)
        return fail() if stage == "challenge_envelope" else {"request": request}
    worker.continuity = SimpleNamespace(envelope=envelope)
    if stage == "association_request":
        snapshot["facts"]["association"] = "none"
        worker.grants.request_creator_association = fail
    if stage == "binding_acquisition":
        snapshot["facts"]["association"] = "approved"
        monkeypatch.setattr(worker.store, "provisioning_candidate",
                            lambda _: SimpleNamespace(state=ProvisioningCandidateState.PENDING))
        monkeypatch.setattr("app.provisioning.binding_acquisition.acquire_creator_account_binding", fail)
    if stage == "authority_refresh":
        snapshot["facts"]["authorization"] = "denied"
        async def refresh(_):
            fail()
        monkeypatch.setattr(worker, "_refresh_current_grants", refresh)
    class Stream:
        async def events(self, envelope):
            if stage == "stream_admission":
                fail()
            yield {"profile": HOSTED_PROFILE, "kind": "snapshot", "snapshot": snapshot}
            if stage == "stream_frame":
                fail()
    worker.continuity_stream = Stream()
    before = count(worker.store, "auth_revocation_bindings")
    try:
        await worker._continuity(row)
        records = [record for record in caplog.records if record.msg.startswith("onboarding_continuity_event ")]
        assert [record.getMessage() for record in records] == [
            f"onboarding_continuity_event stage_code={stage} reason_code={reason} attempt={attempt} outcome={outcome}"
            for attempt, outcome in [(1, "retry"), (2, "retry"), (3, "exhausted")]]
        assert len(attempts) == 3
        assert len(actions) == (4 if stage == "authority_refresh" else 3)
        assert count(worker.store, "auth_revocation_bindings") == before
        for record in caplog.records:
            assert secret not in record.getMessage()
            assert record.exc_info is None and record.stack_info is None
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("result,reason", [
    ("membership_reference_unavailable", "membership_reference_unavailable"),
    ("fixture-secret-return-value", "invalid_binding_result"),
])
@pytest.mark.parametrize("frame_count", [1, 4], ids=["one-frame", "successive-frames"])
async def test_continuity_binding_refusal_logs_only_allowlisted_result_and_then_stream_closure(
        tmp_path, monkeypatch, caplog, result, reason, frame_count):
    from app.persistence.auth import ProvisioningCandidateState
    from app.security.onboarding_continuity import PROFILE as HOSTED_PROFILE

    worker, row, snapshot = _continuity_diagnostic_fixture(tmp_path, monkeypatch)
    snapshot["facts"]["association"] = "approved"
    monkeypatch.setattr(worker.store, "provisioning_candidate",
                        lambda _: SimpleNamespace(state=ProvisioningCandidateState.PENDING))
    calls = []
    def acquire(**_):
        calls.append(1)
        return result
    monkeypatch.setattr("app.provisioning.binding_acquisition.acquire_creator_account_binding", acquire)
    worker.continuity = SimpleNamespace(envelope=lambda request: {"request": request})
    class Stream:
        async def events(self, envelope):
            for index in range(frame_count):
                yield {"profile": HOSTED_PROFILE, "kind": "snapshot" if index == 0 else "committed",
                       "snapshot": {**snapshot, "revision": snapshot["revision"] + index}}
    worker.continuity_stream = Stream()
    try:
        await worker._continuity(row)
        assert len(calls) == frame_count * 3
        assert [record.getMessage() for record in caplog.records] == [message
            for attempt, outcome in [(1, "retry"), (2, "retry"), (3, "exhausted")]
            for message in [
                f"onboarding_continuity_event stage_code=binding_acquisition reason_code={reason} attempt={attempt} outcome=unresolved",
                f"onboarding_continuity_event stage_code=stream_frame reason_code=stream_closed attempt={attempt} outcome={outcome}"]]
        assert "fixture-secret-return-value" not in caplog.text
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_continuity_reconciliation_failure_logs_closed_category_without_changing_authority(
        tmp_path, monkeypatch, caplog):
    worker, row, _ = _continuity_diagnostic_fixture(tmp_path, monkeypatch)
    async def refresh(_):
        raise HostedGrantUnavailable("fixture-private-signed-response")
    monkeypatch.setattr(worker, "_refresh_current_grants", refresh)
    before = count(worker.store, "auth_revocation_bindings")
    try:
        await worker._reconcile_closed_stream(json.loads(row["scope_json"]))
        assert [record.getMessage() for record in caplog.records] == [
            "onboarding_continuity_reconciliation_failed stage_code=authority_refresh reason_code=hosted_unavailable"]
        assert count(worker.store, "auth_revocation_bindings") == before
        assert caplog.records[0].exc_info is None
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["challenge_envelope", "stream_admission", "reconciliation"])
async def test_continuity_cancellation_propagates_without_failure_diagnostics(tmp_path, monkeypatch, caplog, stage):
    worker, row, _ = _continuity_diagnostic_fixture(tmp_path, monkeypatch)
    attempts = []
    def envelope(request):
        attempts.append(request)
        if stage == "challenge_envelope":
            raise asyncio.CancelledError()
        return {"request": request}
    worker.continuity = SimpleNamespace(envelope=envelope)
    class Stream:
        async def events(self, envelope):
            raise asyncio.CancelledError()
            yield
    worker.continuity_stream = Stream()
    async def refresh(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(worker, "_refresh_current_grants", refresh)
    try:
        with pytest.raises(asyncio.CancelledError):
            if stage == "reconciliation":
                await worker._reconcile_closed_stream(json.loads(row["scope_json"]))
            else:
                await worker._continuity(row)
        assert len(attempts) == (0 if stage == "reconciliation" else 1)
        assert caplog.records == []
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuity", [False, True], ids=["initial", "registered"])
async def test_hosted_sse_delivers_small_frames_before_eof_and_closes_on_cancellation(monkeypatch, continuity):
    import httpx
    from app.security.initial_handoff import InitialHandoffStream, PROFILE
    from app.security.onboarding_continuity import PROFILE as HOSTED_PROFILE, validated_event

    class OpenResponse(httpx.AsyncByteStream):
        def __init__(self):
            self.arrivals = asyncio.Queue()
            self.waiting = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            while True:
                self.waiting.set()
                chunk = await self.arrivals.get()
                self.waiting.clear()
                yield chunk

        async def aclose(self):
            self.closed = True

    if continuity:
        snapshot = json.loads(json.dumps(next(value["value"] for value in _CONTINUITY_CASES if value["valid"])))
        second = {**snapshot, "revision": snapshot["revision"] + 1}
        events = [{"profile": HOSTED_PROFILE, "kind": kind, "snapshot": value}
            for kind, value in [("snapshot", snapshot), ("committed", second)]]
        for event in events:
            validated_event(event, transaction_id=snapshot["onboarding_transaction_id"])
        packets = [(f"event: onboarding\nid: {event['snapshot']['epoch']}:{event['snapshot']['revision']}\n"
                    + "data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode() for event in events]
    else:
        events = [{"profile": PROFILE, "status": "waiting", "expires_at": "2026-10-11T00:00:00.000Z", "revision": 0},
                  {"profile": PROFILE, "status": "expired", "revision": 1}]
        packets = [("event: initial-handoff\ndata: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
                   for event in events]
    assert all(len(packet) < 1024 for packet in packets)
    body = OpenResponse()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: actual_client(**kwargs, transport=transport))
    stream = InitialHandoffStream("https://onboarding.example", continuity=continuity).events({})
    pending = None
    try:
        body.arrivals.put_nowait(packets[0])
        assert await asyncio.wait_for(anext(stream), 1) == events[0]
        assert not body.closed
        # The next owner transition arrives on the same still-open connection,
        # split across transport arrivals. Neither delivery depends on EOF.
        halfway = len(packets[1]) // 2
        body.arrivals.put_nowait(b": keepalive\n\n")
        body.arrivals.put_nowait(packets[1][:halfway])
        body.arrivals.put_nowait(packets[1][halfway:])
        assert await asyncio.wait_for(anext(stream), 1) == events[1]
        assert not body.closed
        assert len(requests) == 1
        assert requests[0].method == "POST"
        assert requests[0].url.path == ("/v1/onboarding/streams" if continuity
            else "/v2/onboarding/installation-handoffs:wait")
        body.waiting.clear()
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(body.waiting.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert body.closed
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
