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
    cases = json.loads((Path(__file__).parents[1] / "contracts/initial-installation-handoff-v1/proof-cases.json").read_text())
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

    def prepare(self, request):
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
        hosted_start_url="https://fixture.invalid/public/onboarding/start", client=client,
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
async def test_restart_unknown_completion_reconciles_without_replay(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    journey = worker.journeys.open()["journey_id"]
    worker.journeys.update(journey, state="unknown", operation_id=worker.journeys.new_operation())
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
    expires = asyncio.get_running_loop().time() + 0.02
    reads = []
    def read():
        reads.append(1)
        current = asyncio.get_running_loop().time() < expires
        return owner.snapshot(journey_id=journey, facts={"installation": "verified" if current else "unknown"})
    def remaining():
        value = expires - asyncio.get_running_loop().time()
        return value if value > 0 else None
    stream = owner.stream(read, lambda: None, expires_in=remaining)
    await anext(stream)
    result = await asyncio.wait_for(anext(stream), 1)
    if result.startswith(": keepalive"):
        result = await asyncio.wait_for(anext(stream), 1)
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
