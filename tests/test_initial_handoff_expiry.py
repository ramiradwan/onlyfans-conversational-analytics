"""Controlled-clock expiry regressions with real SQLCipher and external seams.

No live provider, TPM or browser is used. The assertions cover existing-only
preparation, bounded session authority and exact operation recovery.
"""
from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.provisioning.app import create_provisioning_app
from app.provisioning.session import (
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
)
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import InitialHandoffClient, InitialHandoffRefused, PROFILE, PROOF_PROFILE, CHALLENGE_PATH, canonical
from test_onboarding_enrollment_runtime import _enrollment_worker, _HandoffFixture, count
from test_webauthn_routes import INSTANT

pytestmark = [pytest.mark.ci_tier("integration")]


@pytest.fixture
def expiry_case(tmp_path, monkeypatch):
    clock = [INSTANT]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: clock[0])
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    resumed = []
    key_reads = []
    ensure_ready = client.key.ensure_ready

    def read_key():
        key_reads.append(True)
        return ensure_ready()

    # Recovery dispatch is observed, never executed against a hosted service.
    monkeypatch.setattr(client.key, "ensure_ready", read_key)
    monkeypatch.setattr(worker, "resume", resumed.append)
    return clock, store, client, worker, resumed, key_reads


@pytest.mark.asyncio
async def test_fresh_journey_prepares_once_and_starts_its_wait(expiry_case):
    _, store, client, worker, resumed, key_reads = expiry_case
    journey = worker.journeys.open()["journey_id"]

    result = await worker.prepare(journey)

    assert result["state"] == "waiting"
    assert result["journey_id"] == journey
    assert len(client.preparations) == len(key_reads) == 1
    assert resumed == [journey]
    assert worker.journeys.get(journey)["state"] == "waiting"
    assert count(store, "onboarding_uncertain_receipts") == 0


@pytest.mark.asyncio
async def test_existing_expired_journey_returns_typed_refusal_before_key_or_hosted_call(expiry_case):
    clock, _, client, worker, resumed, key_reads = expiry_case
    journey = worker.journeys.open()["journey_id"]
    clock[0] += timedelta(minutes=31)

    try:
        with pytest.raises(InitialHandoffRefused) as refused:
            await worker.prepare(journey)
        assert refused.value.code == "journey_expired"
    finally:
        assert client.preparations == client.waits == resumed == key_reads == []


@pytest.mark.asyncio
async def test_removed_expired_journey_cannot_be_recreated_by_prepare(expiry_case):
    clock, store, client, worker, resumed, key_reads = expiry_case
    journey = worker.journeys.open()["journey_id"]
    clock[0] += timedelta(minutes=31)
    assert worker.journeys.get(journey) is None
    with pytest.raises(InitialHandoffRefused) as refused:
        await worker.prepare(journey)
    assert refused.value.code == "journey_unavailable"
    assert client.preparations == client.waits == resumed == key_reads == []
    assert count(store, "onboarding_journeys") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["key", "write-lock"])
async def test_expiry_during_key_or_preparation_lock_cannot_send_hosted_request(expiry_case, monkeypatch, boundary):
    clock, store, client, worker, resumed, _ = expiry_case
    journey = worker.journeys.open()["journey_id"]
    if boundary == "key":
        read_key = client.key.ensure_ready
        def expired_key():
            result = read_key()
            clock[0] += timedelta(minutes=31)
            return result
        monkeypatch.setattr(client.key, "ensure_ready", expired_key)
    else:
        transaction = store.database.transaction
        @contextmanager
        def delayed_write(*args, **kwargs):
            with transaction(*args, **kwargs) as connection:
                clock[0] += timedelta(minutes=31)
                yield connection
        monkeypatch.setattr(store.database, "transaction", delayed_write)
    with pytest.raises(InitialHandoffRefused) as refused:
        await worker.prepare(journey)
    assert refused.value.code == "journey_expired"
    assert client.preparations == client.waits == resumed == []


def test_session_cannot_outlive_journey_or_reach_prepare_after_its_expiry(expiry_case):
    clock, _, client, worker, resumed, key_reads = expiry_case
    journey = worker.journeys.open()["journey_id"]
    # A later session retains the draft's original deadline.
    clock[0] += timedelta(minutes=5)
    token = "t" * 32
    sessions = ProvisioningSessionManager(token, journeys=worker.journeys,
        wall_clock=lambda: clock[0].timestamp())
    session = sessions.redeem_handoff_code(sessions.issue_handoff_code(
        "Provisioning " + token, journey_id=journey))
    app = create_provisioning_app(claim_submission=lambda **_: None,
        creator_association_initiation=lambda **_: None,
        creator_binding_acquisition=lambda: None, completion_ready=lambda: False,
        finalize_action=lambda **_: None, session_manager=sessions,
        initial_enrollment=worker)
    clock[0] = INSTANT + timedelta(minutes=31)
    assert session.expires_at == (INSTANT + timedelta(minutes=30)).timestamp()

    with TestClient(app, base_url=PROVISIONING_ORIGIN, raise_server_exceptions=False) as browser:
        response = browser.post("/api/v1/provisioning/initial-handoff", json={}, headers={
            "Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}",
            "Origin": PROVISIONING_ORIGIN, "X-Provisioning-CSRF": session.csrf_token,
        })

    assert client.preparations == client.waits == resumed == key_reads == []
    assert response.status_code == 401
    assert response.json() == {"detail": "provisioning session is invalid"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "completing"])
async def test_live_unresolved_completion_resumes_without_another_prepare(expiry_case, state):
    _, _, client, worker, resumed, key_reads = expiry_case
    row = worker.journeys.open()
    operation = worker.journeys.new_operation()
    worker.journeys.update(row["journey_id"], state=state, operation_id=operation,
        scope_json=json.dumps({"installation_id": row["installation_id"]}))

    result = await worker.prepare(row["journey_id"])

    assert result == {"state": state, "journey_id": row["journey_id"]}
    assert resumed == [row["journey_id"]]
    assert client.preparations == client.waits == key_reads == []
    assert worker.journeys.get(row["journey_id"])["operation_id"] == operation


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "completing"])
async def test_expired_unresolved_completion_requires_exact_workspace_receipt_recovery(expiry_case, state):
    clock, store, client, worker, resumed, key_reads = expiry_case
    original = worker.journeys.open()
    operation = worker.journeys.new_operation()
    scope = json.dumps({"installation_id": original["installation_id"]})
    worker.journeys.update(original["journey_id"], state=state,
        operation_id=operation, scope_json=scope)
    clock[0] += timedelta(minutes=31)

    # Untargeted discovery cannot select the latest unrelated receipt.
    with pytest.raises(OnboardingJourneyUnavailable):
        worker.journeys.open()
    selected = worker.journeys.renew_native_session(previous_journey_id=original["journey_id"],
        identifier="r" * 43, csrf="c" * 43, existing_identifier=None,
        ttl_seconds=1800, authorize=lambda: None)
    recovered = worker.journeys.require_current(selected["journey_id"])
    assert recovered["journey_id"] != original["journey_id"]
    assert recovered["state"] == "unknown"
    assert recovered["installation_id"] == original["installation_id"]
    assert recovered["operation_id"] == operation
    assert recovered["scope_json"] == scope
    assert recovered["handoff_reference"] is None
    assert recovered["prepare_json"] is None
    assert recovered["expires_at"] == (INSTANT + timedelta(minutes=60)).isoformat()
    assert recovered["recovery_deadline"] == recovered["expires_at"]
    assert count(store, "onboarding_journeys") == count(store, "onboarding_uncertain_receipts") == 1

    result = await worker.prepare(recovered["journey_id"])

    assert result == {"state": "unknown", "journey_id": recovered["journey_id"]}
    assert resumed == [recovered["journey_id"]]
    assert client.preparations == client.waits == key_reads == []
    assert worker.journeys.get(recovered["journey_id"])["operation_id"] == operation


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["preparing", "prepare-unknown"])
async def test_expired_uncertain_preparation_reuses_exact_request_and_bounds_expired_reply(expiry_case, monkeypatch, state):
    clock, _, client, worker, resumed, _ = expiry_case
    original = worker.journeys.open()
    await worker.prepare(original["journey_id"])
    request = client.preparations[0]
    worker.journeys.update(original["journey_id"], state=state,
        handoff_reference=None, handoff_expires_at=None)
    clock[0] += timedelta(minutes=31)
    selected = worker.journeys.renew_native_session(previous_journey_id=original["journey_id"],
        identifier="r" * 43, csrf="c" * 43, existing_identifier=None,
        ttl_seconds=1800, authorize=lambda: None)
    recovered = worker.journeys.require_current(selected["journey_id"])
    assert recovered["state"] == "prepare-unknown"
    assert recovered["installation_id"] == original["installation_id"]
    assert json.loads(recovered["prepare_json"]) == request
    assert recovered["operation_id"] == request["operation_id"]

    # Keep the production response validator while substituting only transport.
    production = InitialHandoffClient(client.transport, client.key, clock=lambda: clock[0])
    attempts = []
    monkeypatch.setattr(production, "envelope", lambda operation, body, **_: {"request": body})
    def expired_reply(path, envelope, *, expected, before_send=None):
        if before_send is not None:
            before_send()
        attempts.append(envelope["request"])
        return {"profile": PROFILE, "reference": "A" * 43,
            "operation_id": request["operation_id"],
            "expires_at": (INSTANT + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    monkeypatch.setattr(production, "_request", expired_reply)
    worker.client = production
    resumed.clear()
    with pytest.raises(HostedGrantUnavailable, match="^Initial preparation expired$"):
        await worker.prepare(recovered["journey_id"])
    assert attempts == [request, request, request]
    assert resumed == []
    saved = worker.journeys.require_current(recovered["journey_id"])
    assert saved["state"] == "prepare-unknown" and saved["operation_id"] == request["operation_id"]
    assert saved["recovery_deadline"] == (INSTANT + timedelta(minutes=60)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["internal-key", "signing"])
async def test_production_prepare_rechecks_expiry_after_internal_key_and_signing(expiry_case, monkeypatch, boundary):
    clock, _, fixture, worker, resumed, _ = expiry_case
    journey = worker.journeys.open()["journey_id"]
    read_key = fixture.key.ensure_ready
    key_reads, requests = [], []
    def ensure_ready():
        key_reads.append(True)
        if boundary == "internal-key" and len(key_reads) == 2:
            clock[0] += timedelta(minutes=31)
        return read_key()
    def sign_challenge(message):
        if boundary == "signing":
            clock[0] += timedelta(minutes=31)
        return SimpleNamespace(installation_key_id="ik1.AAECAwQFBgcICQoLDA0ODw", algorithm="ES256", signature=b"s" * 64)
    def transport_request(method, path, *, json_body):
        requests.append(path)
        assert path == CHALLENGE_PATH
        value = {"profile": PROOF_PROFILE, "challenge": "A" * 43,
            "request_digest": hashlib.sha256(canonical(json_body["target"]["request"])).hexdigest(),
            "expires_at": (clock[0] + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        return SimpleNamespace(status_code=200, body=json.dumps(value).encode())
    key = SimpleNamespace(ensure_ready=ensure_ready, sign_challenge=sign_challenge)
    production = InitialHandoffClient(SimpleNamespace(request=transport_request), key, clock=lambda: clock[0])
    worker.client = production
    with pytest.raises(InitialHandoffRefused) as refused:
        await worker.prepare(journey)
    assert refused.value.code == "journey_expired"
    assert len(key_reads) == 2
    assert requests == ([] if boundary == "internal-key" else [CHALLENGE_PATH])
    assert resumed == []
