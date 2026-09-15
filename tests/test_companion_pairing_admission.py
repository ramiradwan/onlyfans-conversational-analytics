from __future__ import annotations

import base64
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

import pytest

from app.persistence.auth import (
    AgentChallengeBinding,
    AuthenticationStateError,
    AuthorizedAccountBinding,
    BridgeSessionIssue,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    TicketPurpose,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.persistence.companion_pairing import (
    CompanionPairingCASMismatch,
    CompanionPairingConflict,
    CompanionPairingGrantUnavailable,
    CompanionPairingPersistence,
    CompanionPairingState,
    CompanionPairingStateError,
    CompanionPin,
)


@dataclass
class Clock:
    at: datetime

    def __call__(self):
        return self.at


@pytest.fixture
def context(tmp_path):
    clock = Clock(datetime(2026, 9, 12, tzinfo=timezone.utc))
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    store.reserve_installation_key(
        InstallationKeyReservation("test", "test", "ES256", clock.at)
    )
    store.activate_installation_key(
        InstallationKeyReference(
            "test",
            "test",
            "ES256",
            "key-1",
            "thumbprint-1",
            "{}",
            clock.at,
            clock.at,
        )
    )
    grants = tuple(
        _grant(clock, kind)
        for kind in (
            "installation_grant",
            "creator_account_binding",
            "membership_snapshot",
        )
    )
    store.record_verified_grants(grants)
    store.register_webauthn_credential(
        WebAuthnCredential(
            "credential",
            "operator",
            "issuer",
            "subject",
            "brain",
            b"public",
            0,
            clock.at,
        )
    )
    session = store.issue_bridge_session(
        BridgeSessionIssue(
            "credential",
            "operator",
            "creator",
            "operator",
            clock.at + timedelta(hours=1),
            tuple(grant.reference_id for grant in grants),
        )
    )
    pairing = CompanionPairingPersistence(store)
    return store, pairing, clock, session


def _grant(clock, kind, suffix="", retained=True):
    reference = kind + suffix
    token = "header.payload." + reference
    return VerifiedGrantReference(
        reference_id=reference,
        grant_identifier="id-" + reference,
        grant_type=kind,
        grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        issuer="issuer",
        subject="subject",
        installation_id="brain",
        creator_account_id="creator" if kind == "creator_account_binding" else None,
        valid_from=clock.at - timedelta(minutes=1),
        expires_at=clock.at + timedelta(hours=2),
        verified_at=clock.at,
        organization_id="organization",
        installation_key_id="key-1",
        installation_key_jkt="thumbprint-1",
        compact_jws=token if retained else None,
    )


def _approve(context):
    store, _, clock, _ = context
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            "association",
            "brain",
            "onboarding",
            "organization",
            "creator",
            ProvisioningCandidateState.PENDING,
            clock.at,
        )
    )
    assert store.approve_provisioning_candidate("association", resolved_at=clock.at)
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            "creator",
            "brain",
            "platform-creator",
            "association",
            "a" * 64,
            clock.at,
            ("installation_grant", "creator_account_binding"),
        )
    )


def _open(context, label="first", session_id=None):
    _, pairing, clock, session = context
    return pairing.open_authorized_window(
        session_id=session_id or session.session_id,
        creator_account_id="creator",
        pairing_id=hashlib.sha256(label.encode()).digest(),
        opened_at=clock.at,
        expires_at=clock.at + timedelta(minutes=5),
        brain_nonce=b"n" * 32,
        brain_noise_public_key=b"b" * 32,
        wrapped_brain_noise_private_key=b"protected-test-noise-private-key",
    )


def _await(context, label="first"):
    _, pairing, clock, _ = context
    _open(context, label)
    window = pairing.claim_request()
    window = pairing.offer_window(
        window.pairing_id,
        expected_version=window.version,
        agent_installation_id="agent",
        agent_identity_jwk='{"kty":"EC"}',
        agent_identity_thumbprint="agent-thumbprint",
        agent_noise_public_key=b"a" * 32,
        agent_nonce=b"q" * 32,
        pairing_digest=b"p" * 32,
        grant_digest=b"g" * 32,
        installation_grant_reference_id=window.installation_grant_reference_id,
        creator_account_binding_reference_id=window.creator_account_binding_reference_id,
        offered_at=clock.at,
    )
    return pairing.await_confirmation(
        window.pairing_id, expected_version=window.version, at=clock.at
    )


def _confirm(context, window):
    _, pairing, clock, session = context
    return pairing.confirm_and_admit(
        window.pairing_id,
        expected_version=window.version,
        confirmation_principal_id="operator",
        confirmation_session_id=session.session_id,
        confirmed_at=clock.at,
    )


def _key_equal(actual, expected):
    if not secrets.compare_digest(actual, expected):
        pytest.fail("protected key mismatch", pytrace=False)


def test_open_requires_current_session_and_approved_account(context):
    _, pairing, _, _ = context
    with pytest.raises(CompanionPairingStateError):
        _open(context, session_id="unknown")
    with pytest.raises(CompanionPairingStateError):
        _open(context)
    assert pairing.highest_generation("brain") is None
    _approve(context)
    opened = _open(context)
    assert opened.installation_grant_reference_id == "installation_grant"
    assert opened.creator_account_binding_reference_id == "creator_account_binding"
    assert opened.opening_principal_id == "operator"


def test_second_claim_cancels_before_offer_and_erases_key(context):
    _approve(context)
    _, pairing, _, _ = context
    opened = _open(context)
    claimed = pairing.claim_request()
    assert claimed.version == opened.version + 1
    assert claimed.request_claimed_at is not None
    with pytest.raises(CompanionPairingConflict):
        pairing.claim_request()
    cancelled = pairing.window(opened.pairing_id)
    assert cancelled.state is CompanionPairingState.CANCELLED
    assert cancelled.wrapped_brain_noise_private_key is None


def test_concurrent_claims_commit_exactly_one_claim_and_cancellation(context):
    _approve(context)
    _, pairing, _, _ = context
    opened = _open(context)

    def claim():
        try:
            pairing.claim_request()
            return "claimed"
        except CompanionPairingConflict:
            return "cancelled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: claim(), range(2))) == [
            "cancelled",
            "claimed",
        ]
    assert pairing.window(opened.pairing_id).state is CompanionPairingState.CANCELLED


def test_admission_consumes_staging_and_cannot_repeat(context):
    _approve(context)
    _, pairing, _, session = context
    awaiting = _await(context)
    pin = _confirm(context, awaiting)
    assert isinstance(pin, CompanionPin)
    assert pin.version == awaiting.version + 1
    assert pin.expires_at == awaiting.expires_at
    assert pairing.window(pin.pairing_id) is None
    reconstructed = CompanionPairingPersistence(context[0]).companion_pin(
        pin.pairing_id
    )
    assert reconstructed == pin
    _key_equal(
        reconstructed.wrapped_brain_noise_private_key,
        b"protected-test-noise-private-key",
    )
    assert isinstance(
        pairing.authorized_record(session.session_id, pin.pairing_id), CompanionPin
    )
    assert pairing.highest_generation("brain") == 1
    with pytest.raises(CompanionPairingCASMismatch):
        _confirm(context, awaiting)
    field = next(
        field
        for field in fields(CompanionPin)
        if field.name == "wrapped_brain_noise_private_key"
    )
    assert not field.repr and not field.compare


def test_new_confirmed_pin_replaces_old_pin_in_same_transaction(context):
    _approve(context)
    _, pairing, _, _ = context
    previous = _confirm(context, _await(context))
    replacement = _confirm(context, _await(context, "second"))
    assert pairing.companion_pin(previous.pairing_id) is None
    assert pairing.companion_pin(replacement.pairing_id) is not None
    assert replacement.generation == previous.generation + 1
    with context[0].database.read() as connection:
        revoked = connection.execute(
            "SELECT protected_brain_noise_static_private_key FROM agent_pairings WHERE revoked_at IS NOT NULL"
        ).fetchall()
        assert len(revoked) == 1 and revoked[0][0] is None


def test_admission_failure_rolls_back_previous_pin_and_staging(context, monkeypatch):
    _approve(context)
    store, pairing, _, _ = context
    previous = _confirm(context, _await(context))
    awaiting = _await(context, "replacement")

    def fail(_connection):
        raise RuntimeError("injected admission write failure")

    monkeypatch.setattr(store, "_increment_authorization_epoch", fail)
    with pytest.raises(RuntimeError, match="injected"):
        _confirm(context, awaiting)
    assert pairing.companion_pin(previous.pairing_id) is not None
    assert pairing.companion_pin(awaiting.pairing_id) is None
    assert (
        pairing.window(awaiting.pairing_id).state
        is CompanionPairingState.AWAITING_CONFIRMATION
    )


@pytest.mark.parametrize(
    "scope,identifier",
    [
        (RevocationScopeType.BRIDGE_SESSION, None),
        (RevocationScopeType.VERIFIED_GRANT, "installation_grant"),
        (RevocationScopeType.CREATOR_ACCOUNT, "creator"),
        (RevocationScopeType.INSTALLATION, "brain"),
    ],
)
def test_revocation_fences_pending_admission(context, scope, identifier):
    _approve(context)
    store, pairing, _, session = context
    awaiting = _await(context)
    store.revoke(RevocationKey(scope, identifier or session.session_id))
    with pytest.raises(CompanionPairingCASMismatch):
        _confirm(context, awaiting)
    assert pairing.companion_pin(awaiting.pairing_id) is None
    assert pairing.window(awaiting.pairing_id).wrapped_brain_noise_private_key is None


def test_expiry_fences_confirmation_and_first_claim(context):
    _approve(context)
    _, pairing, clock, _ = context
    awaiting = _await(context)
    clock.at = awaiting.expires_at
    with pytest.raises(CompanionPairingStateError):
        _confirm(context, awaiting)
    with pytest.raises(CompanionPairingConflict):
        pairing.claim_request()
    assert pairing.window(awaiting.pairing_id).state is CompanionPairingState.EXPIRED


def test_late_socket_abort_erases_just_admitted_pin(context):
    _approve(context)
    _, pairing, _, _ = context
    pin = _confirm(context, _await(context))
    assert pairing.abort_candidate(pin.pairing_id)
    assert not pairing.abort_candidate(pin.pairing_id)
    assert pairing.companion_pin(pin.pairing_id) is None
    assert pairing.highest_generation("brain") == 1


def test_legacy_plaintext_auth_cannot_activate_or_challenge_companion_pin(context):
    _approve(context)
    store, pairing, clock, session = context
    pin = _confirm(context, _await(context))
    identifier = base64.urlsafe_b64encode(pin.pairing_id).decode().rstrip("=")
    active = store.read_bridge_session(session.session_value)
    assert not store.activate_agent_pairing(active.policy, identifier)
    with pytest.raises(AuthenticationStateError):
        store.issue_agent_challenge(
            AgentChallengeBinding(
                "agent:agent",
                identifier,
                "POST",
                "/agent/auth/challenge",
                "a" * 64,
                TicketPurpose.AGENT_CONFIG,
                "agent",
                "creator",
                identifier,
                "brain",
            ),
            expires_at=clock.at + timedelta(seconds=10),
        )


def test_authorized_cancel_checks_session_and_cas(context):
    _approve(context)
    _, pairing, _, session = context
    window = _await(context)
    with pytest.raises(CompanionPairingStateError):
        pairing.cancel_authorized_window(
            window.pairing_id, expected_version=window.version, session_id="unknown"
        )
    with pytest.raises(CompanionPairingCASMismatch):
        pairing.cancel_authorized_window(
            window.pairing_id,
            expected_version=window.version + 1,
            session_id=session.session_id,
        )
    declined = pairing.cancel_authorized_window(
        window.pairing_id,
        expected_version=0,
        session_id=session.session_id,
        decline=True,
    )
    assert declined.state is CompanionPairingState.DECLINED
    with pytest.raises(CompanionPairingCASMismatch):
        _confirm(context, window)


def test_opening_session_revocation_clears_unoffered_candidate(context):
    _approve(context)
    store, pairing, _, session = context
    window = _open(context)
    store.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, session.session_id))
    assert pairing.window(window.pairing_id).state is CompanionPairingState.REVOKED
    assert pairing.window(window.pairing_id).wrapped_brain_noise_private_key is None


@pytest.mark.parametrize(
    "scope,identifier",
    [
        (RevocationScopeType.CREATOR_ACCOUNT, "creator"),
        (RevocationScopeType.INSTALLATION, "brain"),
        (RevocationScopeType.INSTALLATION, "agent"),
        (RevocationScopeType.PRINCIPAL, "operator"),
        (RevocationScopeType.PRINCIPAL, "agent:agent"),
    ],
)
def test_scoped_revocation_erases_admitted_noise_key(context, scope, identifier):
    _approve(context)
    store, pairing, _, _ = context
    pin = _confirm(context, _await(context))
    store.revoke(RevocationKey(scope, identifier))
    assert pairing.companion_pin(pin.pairing_id) is None
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT revoked_at, protected_brain_noise_static_private_key FROM agent_pairings"
        ).fetchone()
        assert row["revoked_at"] is not None
        assert row["protected_brain_noise_static_private_key"] is None


def test_revoke_pin_requires_current_bridge_authority(context):
    _approve(context)
    _, pairing, _, session = context
    pin = _confirm(context, _await(context))
    with pytest.raises(CompanionPairingStateError):
        pairing.revoke_companion_pin(pin.pairing_id, session_id="unknown")
    with pytest.raises(CompanionPairingCASMismatch):
        pairing.revoke_companion_pin(
            pin.pairing_id,
            session_id=session.session_id,
            expected_version=pin.version - 1,
        )
    assert pairing.companion_pin(pin.pairing_id) is not None
    assert pairing.revoke_companion_pin(
        pin.pairing_id, session_id=session.session_id, expected_version=pin.version
    )
    assert pairing.companion_pin(pin.pairing_id) is None


def test_grant_refresh_does_not_delete_long_lived_pin(context):
    _approve(context)
    store, pairing, clock, _ = context
    pin = _confirm(context, _await(context))
    replacement = _grant(clock, "installation_grant", "-replacement")
    store.replace_verified_grant("installation_grant", replacement)
    reconstructed = pairing.companion_pin(pin.pairing_id)
    assert reconstructed is not None
    _key_equal(
        reconstructed.wrapped_brain_noise_private_key,
        pin.wrapped_brain_noise_private_key,
    )


def test_offer_cannot_replace_frozen_grants(context):
    _approve(context)
    store, pairing, clock, _ = context
    _open(context)
    window = pairing.claim_request()
    replacement = _grant(clock, "creator_account_binding", "-another")
    store.record_verified_grant(replacement)
    with pytest.raises(CompanionPairingGrantUnavailable):
        pairing.offer_window(
            window.pairing_id,
            expected_version=window.version,
            agent_installation_id="agent",
            agent_identity_jwk='{"kty":"EC"}',
            agent_identity_thumbprint="agent-thumbprint",
            agent_noise_public_key=b"a" * 32,
            agent_nonce=b"q" * 32,
            pairing_digest=b"p" * 32,
            grant_digest=b"g" * 32,
            installation_grant_reference_id=window.installation_grant_reference_id,
            creator_account_binding_reference_id=replacement.reference_id,
            offered_at=clock.at,
        )
    unchanged = pairing.window(window.pairing_id)
    assert unchanged.version == window.version
    assert unchanged.creator_account_binding_reference_id == "creator_account_binding"


def test_admission_rejects_an_already_revoked_agent_installation(context):
    _approve(context)
    store, pairing, _, _ = context
    store.revoke(RevocationKey(RevocationScopeType.INSTALLATION, "agent"))
    awaiting = _await(context)
    with pytest.raises(CompanionPairingStateError):
        _confirm(context, awaiting)
    assert pairing.companion_pin(awaiting.pairing_id) is None


def test_opening_session_expiry_fences_offer_even_with_live_window(context):
    _approve(context)
    store, pairing, clock, _ = context
    short_session = store.issue_bridge_session(
        BridgeSessionIssue(
            "credential",
            "operator",
            "creator",
            "operator",
            clock.at + timedelta(seconds=1),
            ("installation_grant", "creator_account_binding", "membership_snapshot"),
        )
    )
    window = _open(context, session_id=short_session.session_id)
    clock.at += timedelta(seconds=2)
    with pytest.raises(CompanionPairingStateError):
        pairing.claim_request()
    assert pairing.window(window.pairing_id).version == window.version


def test_session_expiry_during_signing_fences_offer_commit(context):
    _approve(context)
    store, pairing, clock, _ = context
    short_session = store.issue_bridge_session(
        BridgeSessionIssue(
            "credential",
            "operator",
            "creator",
            "operator",
            clock.at + timedelta(seconds=1),
            ("installation_grant", "creator_account_binding", "membership_snapshot"),
        )
    )
    _open(context, session_id=short_session.session_id)
    window = pairing.claim_request()
    clock.at += timedelta(seconds=2)
    with pytest.raises(CompanionPairingStateError):
        pairing.offer_window(
            window.pairing_id,
            expected_version=window.version,
            agent_installation_id="agent",
            agent_identity_jwk='{"kty":"EC"}',
            agent_identity_thumbprint="agent-thumbprint",
            agent_noise_public_key=b"a" * 32,
            agent_nonce=b"q" * 32,
            pairing_digest=b"p" * 32,
            grant_digest=b"g" * 32,
            installation_grant_reference_id=window.installation_grant_reference_id,
            creator_account_binding_reference_id=window.creator_account_binding_reference_id,
            offered_at=clock.at,
        )
    assert pairing.window(window.pairing_id).version == window.version


@pytest.mark.parametrize("after_open", [False, True], ids=["opening", "admission"])
def test_pairing_binds_approved_organization(context, after_open):
    _approve(context)
    store, pairing, _, _ = context
    awaiting = _await(context) if after_open else None
    with store.database.transaction() as connection:
        connection.execute(
            "UPDATE provisioning_candidates SET organization_id = 'another-organization' "
            "WHERE association_request_id = 'association'"
        )
    with pytest.raises(CompanionPairingStateError):
        if after_open:
            _confirm(context, awaiting)
        else:
            _open(context)
    if awaiting is not None:
        assert pairing.companion_pin(awaiting.pairing_id) is None
    else:
        assert pairing.highest_generation("brain") is None


@pytest.mark.parametrize(
    "requested", [False, True], ids=["no-agent", "awaiting-confirmation"]
)
def test_authorized_status_expires_and_clears_candidate_without_socket_polling(
    context, requested
):
    _approve(context)
    _, pairing, clock, session = context
    window = _await(context) if requested else _open(context)
    clock.at = window.expires_at
    expired = pairing.authorized_record(session.session_id, window.pairing_id)
    assert expired.state is CompanionPairingState.EXPIRED
    assert expired.version == window.version + 1
    assert expired.wrapped_brain_noise_private_key is None
    assert pairing.window(window.pairing_id).state is CompanionPairingState.EXPIRED


def test_authorized_pin_inventory_requires_current_session_and_omits_revoked(context):
    _approve(context)
    _, pairing, _, session = context
    pin = _confirm(context, _await(context))
    with pytest.raises(CompanionPairingStateError):
        pairing.authorized_pins("unknown")
    assert [
        item.pairing_id for item in pairing.authorized_pins(session.session_id)
    ] == [pin.pairing_id]
    pairing.revoke_companion_pin(pin.pairing_id, session_id=session.session_id)
    assert pairing.authorized_pins(session.session_id) == ()


@pytest.mark.parametrize("column", ["creator_account_id", "installation_id"])
def test_authorized_pin_inventory_and_revoke_do_not_cross_account_or_installation(
    context, column
):
    _approve(context)
    store, pairing, _, session = context
    pin = _confirm(context, _await(context))
    # Simulate another account/installation's independently admitted row.
    with store.database.transaction() as connection:
        connection.execute(
            f"UPDATE agent_pairings SET {column} = ?", ("another-scope",)
        )
    assert pairing.authorized_pins(session.session_id) == ()
    with pytest.raises(CompanionPairingStateError):
        pairing.revoke_companion_pin(pin.pairing_id, session_id=session.session_id)
