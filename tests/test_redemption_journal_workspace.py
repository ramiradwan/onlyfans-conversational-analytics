"""Terminal delivery evidence settles only its own saved workspace blocker."""
from datetime import timedelta
import json

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence import sqlite_api
from app.persistence.saved_workspace import SavedWorkspaceStore
from app.security.activation_delivery_recovery import ActivationDeliveryJournal
from app.security.capability_license_delivery_journal import SQLiteCapabilityLicenseDeliveryJournal
from app.security.capability_license_redemption import (
    CapabilityLicenseRedemptionClient, CapabilityLicenseRedemptionRefused, CapabilityLicenseRedemptionUnavailable,
)
from app.security.capability_license_redemption_journal import RedemptionJournalError, SQLiteRedemptionJournal
from app.security.hosted_grants import TransportResponse
from test_capability_license_opaque_redemption import CONTINUATION, ProofAuthority, QueueTransport, _challenge, _delivery
from test_registered_continuation_context import owner
from test_runtime_activation_entry import installed

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
ORIGIN = "https://control-plane.example.invalid"


def error_response(status=410, **changes):
    body = {"profile": "urn:bridge-clean:error:v1", "request_id": "019a00b0-0000-7000-8000-000000000031",
        "code": "expired", "detail": "continuation_expired", **changes}
    return TransportResponse(status, json.dumps(body).encode(), "application/json")


@pytest.fixture
def workspace(owner):
    journey = installed(owner)
    target = owner.contexts.selection.select()
    key = owner.key.reopen_existing()
    scope = {"organization_id": target.claim.organization_id, "installation_id": target.claim.installation_id,
        "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt}
    def classify():
        return SavedWorkspaceStore(owner.contexts).inspect(target, journey, journey)["classification"]
    assert classify() == "same_target"
    challenge = json.loads(_challenge().body)
    challenge.update(organization_id=scope["organization_id"], installation_id=scope["installation_id"])
    return owner, key, scope, classify, TransportResponse(201, json.dumps(challenge).encode(), "application/json")


def redeem(client, key, scope):
    return client.redeem(CONTINUATION, organization_id=scope["organization_id"],
        installation_id=scope["installation_id"], key=key)


@pytest.mark.parametrize(("response", "result"), [
    (error_response(), "redemption_expired"),
    (error_response(404, code="not_found", detail="continuation_invalid"), "redemption_invalid"),
])
def test_terminal_commit_refusal_preserves_workspace_and_replays_without_network(workspace, response, result):
    owner, key, scope, classify, challenge = workspace
    journal = SQLiteRedemptionJournal(owner.store.database, ORIGIN)
    transport, proof = QueueTransport([challenge, response]), ProofAuthority(key)
    with pytest.raises(CapabilityLicenseRedemptionRefused) as failure:
        redeem(CapabilityLicenseRedemptionClient(transport, proof, journal), key, scope)
    assert failure.value.result == result and classify() == "same_target"
    original = journal.pending(CONTINUATION, scope)
    assert original is not None and original.terminal_result == result and original.response is None
    assert len(transport.requests) == 2 and len(proof.signed) == 1
    with pytest.raises(RedemptionJournalError):
        journal.complete(original, _delivery())
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM capability_license_references").fetchone()[0] == 0

    owner.clock[0] += timedelta(days=365)
    assert classify() == "inactive"
    reopened = SQLiteAuthenticationStore(owner.store.database.path, clock=lambda: owner.clock[0])
    reopened_journal = SQLiteRedemptionJournal(reopened.database, ORIGIN)
    offline, fresh_proof = QueueTransport([]), ProofAuthority(key)
    with pytest.raises(CapabilityLicenseRedemptionRefused) as replay:
        redeem(CapabilityLicenseRedemptionClient(offline, fresh_proof, reopened_journal), key, scope)
    assert replay.value.result == result and offline.requests == [] and fresh_proof.signed == []
    assert reopened_journal.pending(CONTINUATION, scope) == original
    with pytest.raises(sqlite_api.IntegrityError), reopened.database.transaction() as connection:
        connection.execute("UPDATE capability_license_redemption_journal SET terminal_json=NULL,terminal_status=NULL,terminal_digest=NULL")
    assert classify() == "inactive"


_UNRESOLVED = [
    pytest.param(OSError("unavailable"), id="transport-loss"),
    pytest.param(error_response(503, code="temporarily_unavailable", detail="authority_unavailable"), id="temporarily-unavailable"),
    pytest.param(error_response(401, code="unauthorized", detail="installation_proof_invalid"), id="proof-denial"),
    pytest.param(error_response(403, code="forbidden", detail="installation_binding_mismatch"), id="owner-denial"),
    pytest.param(error_response(409, code="conflict", detail="idempotency_conflict"), id="conflict"),
    pytest.param(error_response(400, code="invalid_request", detail="request_invalid"), id="general-invalid"),
    pytest.param(error_response(404), id="wrong-status"),
    pytest.param(error_response(code="not_found"), id="wrong-code"),
    pytest.param(error_response(detail="continuation_invalid"), id="wrong-detail"),
    pytest.param(error_response(profile="different"), id="wrong-profile"),
    pytest.param(error_response(request_id="unknown"), id="invalid-request-id"),
    pytest.param(error_response(extra=True), id="extra-member"),
    pytest.param(TransportResponse(410, b'{"code":"expired","detail":"continuation_expired"}', "application/json"), id="missing-members"),
    pytest.param(TransportResponse(410, error_response().body[:-1] + b',"code":"expired"}', "application/json"), id="duplicate-member"),
    pytest.param(TransportResponse(410, error_response().body, "text/plain"), id="wrong-content-type"),
]


@pytest.mark.parametrize("outcome", _UNRESOLVED)
def test_uncertain_or_nonterminal_commit_keeps_workspace_protected(workspace, outcome):
    owner, key, scope, classify, challenge = workspace
    journal = SQLiteRedemptionJournal(owner.store.database, ORIGIN)
    transport = QueueTransport([challenge, outcome, outcome])
    with pytest.raises((CapabilityLicenseRedemptionRefused, CapabilityLicenseRedemptionUnavailable)):
        redeem(CapabilityLicenseRedemptionClient(transport, ProofAuthority(key), journal), key, scope)
    pending = journal.pending(CONTINUATION, scope)
    assert pending is not None and pending.terminal_result is None and pending.response is None
    assert classify() == "indeterminate"


def test_terminal_challenge_refusal_does_not_create_commit_evidence(workspace):
    owner, key, scope, classify, _ = workspace
    journal = SQLiteRedemptionJournal(owner.store.database, ORIGIN)
    with pytest.raises(CapabilityLicenseRedemptionRefused):
        redeem(CapabilityLicenseRedemptionClient(QueueTransport([error_response()]), ProofAuthority(key), journal), key, scope)
    assert journal.pending(CONTINUATION, scope) is None and classify() == "same_target"


def test_terminal_evidence_cannot_replace_protected_success(workspace):
    owner, key, scope, classify, challenge = workspace
    journal = SQLiteRedemptionJournal(owner.store.database, ORIGIN)
    redeem(CapabilityLicenseRedemptionClient(QueueTransport([challenge, _delivery()]), ProofAuthority(key), journal), key, scope)
    pending = journal.pending(CONTINUATION, scope)
    with pytest.raises(RedemptionJournalError):
        journal.refuse(pending, error_response())
    assert journal.pending(CONTINUATION, scope) == pending and classify() == "indeterminate"


@pytest.mark.parametrize("other_work", ["reserved", "committed", "issuance"])
def test_terminal_refusal_settles_only_its_own_blocker(workspace, other_work):
    owner, key, scope, classify, challenge = workspace
    journal = SQLiteRedemptionJournal(owner.store.database, ORIGIN)
    binding = {**scope, "creator_account_id": "creator-1",
        "installation_key_id": "ik1." + "K" * 22, "installation_key_jkt": "J" * 43}
    activation = ActivationDeliveryJournal(owner.store.database, ORIGIN)
    operation = "019a00b0-0000-7000-8000-000000000034"
    if other_work in {"reserved", "committed"}:
        activation.reserve(operation, binding)
        if other_work == "committed":
            activation.activation_committed(operation, binding)
    else:
        SQLiteCapabilityLicenseDeliveryJournal(owner.store.database, ORIGIN).prepare(
            "activate", "exchange.fixture", "/fixture:activate", "original-issuance-key", {"request": {}, "proof": {}})
    with pytest.raises(CapabilityLicenseRedemptionRefused):
        redeem(CapabilityLicenseRedemptionClient(QueueTransport([challenge, error_response()]), ProofAuthority(key), journal), key, scope)
    assert classify() == ("same_target" if other_work == "reserved" else "indeterminate")
