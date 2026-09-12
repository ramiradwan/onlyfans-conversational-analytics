"""Real-store pairing flow with the contract's reconstructable signing fixture."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import (
    AuthorizedAccountBinding,
    BridgeSessionIssue,
    SQLiteAuthenticationStore,
    WebAuthnCredential,
    RevocationKey,
    RevocationScopeType,
    ProvisioningCandidate,
    ProvisioningCandidateState,
)
from app.security.companion_pairing import (
    CompanionPairingService,
    CompanionPairingError,
    encode_pairing_id,
)
from app.security import companion_pairing_proof as proof
from app.security.installation_key import InstallationKeyAuthority
from app.security.local_data_key import unprotect_local_secret
from app.security.companion_pairing import NOISE_KEY_PURPOSE
from test_companion_pairing_proof import (
    FixtureProvider,
    contract,
    grant_references,
    ORDER,
)


@pytest.fixture
def local(tmp_path, contract, grant_references):
    clock = SimpleNamespace(
        now=datetime.fromtimestamp(contract["vector"]["now"], timezone.utc)
    )
    store = SQLiteAuthenticationStore(
        tmp_path / "auth.sqlite3", clock=lambda: clock.now
    )
    authority = InstallationKeyAuthority(store, FixtureProvider(contract))
    authority.ensure_ready()
    installation = grant_references["installation_grant"]
    binding = grant_references["creator_account_binding"]
    membership = replace(
        installation,
        reference_id="membership",
        grant_identifier="membership-jti",
        grant_type="membership_snapshot",
        grant_digest=hashlib.sha256(b"membership fixture").hexdigest(),
        compact_jws=None,
        allowed_creator_account_ids=(binding.creator_account_id,),
    )
    license = replace(
        installation,
        reference_id="license",
        grant_identifier="license-jti",
        grant_type="license_entitlement",
        grant_digest=hashlib.sha256(b"license fixture").hexdigest(),
        compact_jws=None,
    )
    store.record_verified_grants((installation, binding, membership, license))
    references = (
        installation.reference_id,
        binding.reference_id,
        membership.reference_id,
        license.reference_id,
    )
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id="association-fixture",
            installation_id=installation.installation_id,
            onboarding_transaction_id="onboarding-fixture",
            organization_id=installation.organization_id,
            creator_account_id=binding.creator_account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=clock.now,
        )
    )
    store.approve_provisioning_candidate("association-fixture", resolved_at=clock.now)
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=binding.creator_account_id,
            installation_id=installation.installation_id,
            platform_creator_id="12345",
            association_request_id="association-fixture",
            grant_bundle_sha256="0" * 64,
            authorized_at=clock.now,
            grant_reference_ids=references,
        )
    )
    store.register_webauthn_credential(
        WebAuthnCredential(
            credential_id="operator-credential",
            principal_id="operator",
            external_issuer=installation.issuer,
            external_subject=installation.subject,
            installation_id=installation.installation_id,
            public_key=b"fixture-webauthn-key",
            signature_count=0,
            enrolled_at=clock.now,
        )
    )
    session = store.issue_bridge_session(
        BridgeSessionIssue(
            credential_id="operator-credential",
            principal_id="operator",
            creator_account_id=binding.creator_account_id,
            role="operator",
            expires_at=clock.now + timedelta(minutes=10),
            grant_reference_ids=references,
        )
    )
    return SimpleNamespace(
        store=store,
        service=CompanionPairingService(store, authority),
        policy=session.policy,
        authority=authority,
        clock=clock,
        contract=contract,
        account=binding.creator_account_id,
        request=contract["vector"]["request"],
        grants=grant_references,
    )


def _agent_confirmation(local, window):
    label = local.contract["vector"]["fixture_labels"]["agent_identity_key"]
    prefix = local.contract["profile"]["test_fixture_derivation"]["label_prefix"]
    material = hashlib.sha256((prefix + label).encode()).digest()
    key = ec.derive_private_key(
        int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1()
    )
    encoded = key.sign(
        proof.proof_message("agent", window.pairing_digest), ec.ECDSA(hashes.SHA256())
    )
    r, s = decode_dss_signature(encoded)
    raw = r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")
    return {
        "type": "pair.confirm",
        "pairing_id": encode_pairing_id(window.pairing_id),
        "agent_proof": encode_pairing_id(raw),
    }


def _awaiting(local):
    opened = local.service.open(local.policy, local.account)
    claimed = local.service.claim()
    local.service.offer(claimed, local.request)
    offered = local.service.persistence.window(claimed.pairing_id)
    local.service.confirm_agent(claimed.pairing_id, _agent_confirmation(local, offered))
    return claimed.pairing_id, local.service.status(local.policy, opened["pairing_id"])


def test_real_pairing_requires_both_proofs_and_local_confirmation(local, caplog):
    opened = local.service.open(local.policy, local.account)
    claimed = local.service.claim()
    assert local.service.outcome(claimed.pairing_id) is None
    offer = local.service.offer(claimed, local.request)
    row = local.service.persistence.window(claimed.pairing_id)
    assert proof.verify_pairing_proof(
        offer["installation_jwk"], "brain", row.pairing_digest, offer["brain_proof"]
    )
    assert (
        local.service.status(local.policy, opened["pairing_id"])["comparison_code"]
        is None
    )
    local.service.confirm_agent(claimed.pairing_id, _agent_confirmation(local, row))
    status = local.service.status(local.policy, opened["pairing_id"])
    assert status["comparison_code"] == proof.comparison_code(row.pairing_digest)
    assert local.service.persistence.companion_pin(claimed.pairing_id) is None
    approved = local.service.confirm(
        local.policy, opened["pairing_id"], status["version"]
    )
    assert approved["state"] == "admitted"
    assert local.service.outcome(claimed.pairing_id) == "confirmed"
    pin = local.service.persistence.companion_pin(claimed.pairing_id)
    assert pin is not None
    # Only a protected key reaches durable storage, and the staging copy is consumed.
    assert local.service.persistence.window(claimed.pairing_id) is None
    assert (
        len(
            unprotect_local_secret(
                pin.wrapped_brain_noise_private_key, purpose=NOISE_KEY_PURPOSE
            )
        )
        == 32
    )
    rendered = json.dumps([opened, status, approved]) + caplog.text
    for grant in local.grants.values():
        if grant.compact_jws in rendered:
            pytest.fail(
                "public pairing status exposed retained authorization", pytrace=False
            )


def test_second_request_cancels_the_first_before_offer(local):
    local.service.open(local.policy, local.account)
    first = local.service.claim()
    with pytest.raises(CompanionPairingError):
        local.service.claim()
    with pytest.raises(CompanionPairingError):
        local.service.offer(first, local.request)
    assert local.service.outcome(first.pairing_id) == "cancelled"


@pytest.mark.parametrize("interruption", ["cancel", "expire", "revoke"])
def test_signing_completion_cannot_bypass_commit_fence(
    local, monkeypatch, interruption
):
    local.service.open(local.policy, local.account)
    claimed = local.service.claim()
    sign = local.authority.sign_challenge

    def late_signature(message):
        signature = sign(message)
        if interruption == "cancel":
            local.service.abort(claimed.pairing_id)
        elif interruption == "expire":
            local.clock.now += timedelta(seconds=301)
        else:
            local.store.revoke(
                RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, local.account)
            )
        return signature

    monkeypatch.setattr(local.authority, "sign_challenge", late_signature)
    with pytest.raises(CompanionPairingError):
        local.service.offer(claimed, local.request)
    assert local.service.outcome(claimed.pairing_id) in {"cancelled", "expired"}
    assert local.service.persistence.companion_pin(claimed.pairing_id) is None


def test_abort_after_bridge_approval_removes_the_pending_pin(local):
    pairing_id, awaiting = _awaiting(local)
    local.service.confirm(local.policy, awaiting["pairing_id"], awaiting["version"])
    local.service.abort(pairing_id)
    assert local.service.persistence.companion_pin(pairing_id) is None
    assert local.service.outcome(pairing_id) == "cancelled"


def test_bridge_cancellation_catches_concurrent_admission(local):
    pairing_id, awaiting = _awaiting(local)
    local.service.confirm(local.policy, awaiting["pairing_id"], awaiting["version"])
    cancelled = local.service.cancel(local.policy, awaiting["pairing_id"], 0)
    assert cancelled["state"] == "cancelled"
    assert local.service.persistence.companion_pin(pairing_id) is None


def test_decline_cannot_be_replayed_as_confirmation(local):
    pairing_id, awaiting = _awaiting(local)
    declined = local.service.cancel(
        local.policy, awaiting["pairing_id"], awaiting["version"], decline=True
    )
    assert declined["state"] == "declined"
    with pytest.raises(CompanionPairingError):
        local.service.confirm(local.policy, awaiting["pairing_id"], awaiting["version"])
    assert local.service.outcome(pairing_id) == "declined"


def test_wrong_agent_proof_never_enables_comparison_or_confirmation(local):
    opened = local.service.open(local.policy, local.account)
    claimed = local.service.claim()
    local.service.offer(claimed, local.request)
    with pytest.raises(CompanionPairingError):
        local.service.confirm_agent(
            claimed.pairing_id,
            {
                "type": "pair.confirm",
                "pairing_id": opened["pairing_id"],
                "agent_proof": "A" * 86,
            },
        )
    assert (
        local.service.status(local.policy, opened["pairing_id"])["comparison_code"]
        is None
    )


@pytest.mark.parametrize(
    "mutation", ["extra", "missing", "bad-key", "bad-id", "bad-nonce"]
)
def test_malformed_request_is_refused(local, mutation):
    local.service.open(local.policy, local.account)
    claimed = local.service.claim()
    request = dict(local.request)
    if mutation == "extra":
        request["unexpected"] = "value"
    if mutation == "missing":
        request.pop("agent_nonce")
    if mutation == "bad-key":
        request["agent_identity_jwk"] = {"kty": "EC"}
    if mutation == "bad-id":
        request["agent_installation_id"] = "bad/id"
    if mutation == "bad-nonce":
        request["agent_nonce"] = "short"
    with pytest.raises(CompanionPairingError):
        local.service.offer(claimed, request)
