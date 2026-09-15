"""Session-bound credentials exercised against real pairing fixtures and SQL."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import (
    RevocationKey,
    RevocationScopeType,
    TicketBinding,
    TicketPurpose,
)
from app.security.companion_session_authority import (
    CompanionSessionAuthority,
    CompanionSessionError,
    request_signing_message,
)
from test_companion_pairing_service import local, _awaiting
from test_companion_pairing_proof import contract, grant_references, ORDER


@pytest.fixture
def admitted(local):
    identifier, view = _awaiting(local)
    local.service.confirm(local.policy, view["pairing_id"], view["version"])
    authority = CompanionSessionAuthority(local.store)
    snapshot = authority.prepare(identifier)
    return local, authority, snapshot, authority.authorize(snapshot)


def _sign(local, snapshot, challenge):
    label = local.contract["vector"]["fixture_labels"]["agent_identity_key"]
    prefix = local.contract["profile"]["test_fixture_derivation"]["label_prefix"]
    material = hashlib.sha256((prefix + label).encode()).digest()
    key = ec.derive_private_key(
        int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1()
    )
    encoded = key.sign(
        request_signing_message(
            challenge["session_id"], challenge["challenge"], snapshot
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    r, s = decode_dss_signature(encoded)
    raw = r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _authenticate(admitted, handle=None):
    local, _, snapshot, original = admitted
    handle = handle or original
    challenge = handle.challenge()
    return handle.authenticate(
        challenge["challenge_id"], _sign(local, snapshot, challenge)
    )


def _hello(admitted):
    _, _, snapshot, handle = admitted
    ticket = _authenticate(admitted)
    return handle.accept_hello(
        ticket["auth_ticket"],
        snapshot.pin.creator_account_id,
        snapshot.pin.agent_installation_id,
    )


def test_prepare_requires_live_confirmed_pin(local):
    with pytest.raises(CompanionSessionError, match="^companion_session_refused$"):
        CompanionSessionAuthority(local.store).prepare(b"x" * 32)


def test_session_authority_requires_fresh_agent_proof_and_survives_own_epoch_changes(
    admitted,
):
    local, _, snapshot, handle = admitted
    with pytest.raises(CompanionSessionError):
        handle.accept_hello(
            "unknown", local.account, snapshot.pin.agent_installation_id
        )
    principal, account, reconnect, config = _hello(admitted)
    assert principal == "agent:" + snapshot.pin.agent_installation_id
    assert account == local.account
    policy = handle.current_policy()
    assert policy.identity.role == "agent"
    handle.validate_config(config, account, snapshot.pin.agent_installation_id)
    handle.validate_config(config, account, snapshot.pin.agent_installation_id)
    assert handle.current_policy().identity == policy.identity
    local.clock.now += timedelta(seconds=60)
    handle.validate_config(config, account, snapshot.pin.agent_installation_id)
    with pytest.raises(CompanionSessionError):
        handle.accept_hello(reconnect, account, snapshot.pin.agent_installation_id)


def test_plain_ticket_and_challenge_consumers_refuse_companion_credentials(admitted):
    local, _, snapshot, handle = admitted
    challenge = handle.challenge()
    assert (
        local.store.consume_agent_challenge(
            challenge["challenge"], handle._request_binding()
        )
        is None
    )
    ticket = handle.authenticate(
        challenge["challenge_id"], _sign(local, snapshot, challenge)
    )["auth_ticket"]
    binding = TicketBinding(
        TicketPurpose.AGENT_WEBSOCKET,
        local.account,
        "agent",
        expected_agent_installation_id=snapshot.pin.agent_installation_id,
    )
    assert local.store.consume_ticket(ticket, binding) is None
    assert (
        handle.accept_hello(ticket, local.account, snapshot.pin.agent_installation_id)[
            1
        ]
        == local.account
    )


def test_proof_is_single_use_and_cannot_cross_noise_sessions(admitted):
    local, authority, snapshot, first = admitted
    second = authority.authorize(snapshot)
    original = first.challenge()
    another = second.challenge()
    signature = _sign(local, snapshot, original)
    with pytest.raises(CompanionSessionError):
        second.authenticate(another["challenge_id"], signature)
    issued = first.authenticate(original["challenge_id"], signature)
    with pytest.raises(CompanionSessionError):
        first.authenticate(original["challenge_id"], signature)
    _authenticate(admitted, second)
    with pytest.raises(CompanionSessionError):
        second.accept_hello(
            issued["auth_ticket"], local.account, snapshot.pin.agent_installation_id
        )
    assert (
        first.accept_hello(
            issued["auth_ticket"], local.account, snapshot.pin.agent_installation_id
        )[1]
        == local.account
    )


def test_invalid_signature_burns_challenge_without_issuing_ticket(admitted):
    local, _, _, handle = admitted
    challenge = handle.challenge()
    with pytest.raises(CompanionSessionError):
        handle.authenticate(challenge["challenge_id"], "A" * 86)
    with local.store.database.read() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM runtime_tickets").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT consumed_at FROM auth_challenges").fetchone()[0]
            is not None
        )


@pytest.mark.parametrize(
    "scope", ["pin", "account", "brain", "agent", "principal", "grant"]
)
def test_revocation_fences_every_session_operation(admitted, scope):
    local, _, snapshot, handle = admitted
    _hello(admitted)
    key = {
        "pin": RevocationKey(
            RevocationScopeType.AGENT_PAIRING, handle.binding.pairing_id
        ),
        "account": RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, local.account),
        "brain": RevocationKey(
            RevocationScopeType.INSTALLATION, snapshot.pin.installation_id
        ),
        "agent": RevocationKey(
            RevocationScopeType.INSTALLATION, snapshot.pin.agent_installation_id
        ),
        "principal": RevocationKey(
            RevocationScopeType.PRINCIPAL, handle.binding.principal_id
        ),
        "grant": RevocationKey(
            RevocationScopeType.VERIFIED_GRANT, snapshot.installation_grant.reference_id
        ),
    }[scope]
    local.store.revoke(key)
    with pytest.raises(CompanionSessionError):
        handle.current_policy()
    with pytest.raises(CompanionSessionError):
        with handle.operation():
            pytest.fail("revoked operation entered")


def test_grant_refresh_selects_new_bytes_only_for_next_session(admitted):
    local, authority, snapshot, handle = admitted
    prior = snapshot.installation_grant
    header, payload, _ = prior.compact_jws.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["jti"] = claims["jti"][:-1] + ("0" if claims["jti"][-1] != "0" else "1")
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    label = local.contract["vector"]["fixture_labels"]["issuer_key"]
    prefix = local.contract["profile"]["test_fixture_derivation"]["label_prefix"]
    material = hashlib.sha256((prefix + label).encode()).digest()
    key = ec.derive_private_key(
        int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1()
    )
    signature = key.sign(
        f"{header}.{payload}".encode("ascii"), ec.ECDSA(hashes.SHA256())
    )
    r, s = decode_dss_signature(signature)
    signature = (
        base64.urlsafe_b64encode(
            r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")
        )
        .rstrip(b"=")
        .decode()
    )
    token = f"{header}.{payload}.{signature}"
    replacement = replace(
        prior,
        reference_id=prior.reference_id + "-replacement",
        grant_identifier=claims["jti"],
        compact_jws=token,
        grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
    )
    local.store.replace_verified_grant(prior.reference_id, replacement)
    with pytest.raises(CompanionSessionError):
        handle.current_policy()
    fresh = authority.prepare(snapshot.pin.pairing_id)
    assert fresh.installation_grant.reference_id == replacement.reference_id
    assert fresh.pin.pairing_digest == snapshot.pin.pairing_digest
    assert (
        authority.authorize(fresh).current_policy().identity.creator_account_id
        == local.account
    )


def test_lifetime_limit_requires_new_handshake(admitted):
    local, authority, snapshot, handle = admitted
    assert snapshot.expires_at <= local.clock.now + timedelta(seconds=900)
    local.clock.now = snapshot.expires_at
    with pytest.raises(CompanionSessionError):
        handle.current_policy()
    with pytest.raises(CompanionSessionError):
        authority.authorize(snapshot)


def test_wrong_account_does_not_consume_ticket(admitted):
    local, _, snapshot, handle = admitted
    ticket = _authenticate(admitted)["auth_ticket"]
    with pytest.raises(CompanionSessionError):
        handle.accept_hello(
            ticket, "another-account", snapshot.pin.agent_installation_id
        )
    assert (
        handle.accept_hello(ticket, local.account, snapshot.pin.agent_installation_id)[
            1
        ]
        == local.account
    )


def test_close_invalidates_unconsumed_credentials_and_fences_reuse(admitted):
    local, _, snapshot, handle = admitted
    handle.challenge()
    handle.close()
    with local.store.database.read() as connection:
        assert (
            connection.execute("SELECT invalidated_at FROM auth_challenges").fetchone()[
                0
            ]
            is not None
        )
    with pytest.raises(CompanionSessionError):
        handle.current_policy()
    with pytest.raises(CompanionSessionError):
        handle.challenge()


def test_operation_serializes_against_revocation_commit(admitted):
    local, _, snapshot, handle = admitted
    _hello(admitted)
    started = Event()

    def revoke():
        started.set()
        local.store.revoke(
            RevocationKey(RevocationScopeType.AGENT_PAIRING, handle.binding.pairing_id)
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with handle.operation() as policy:
            assert policy.identity.creator_account_id == local.account
            future = executor.submit(revoke)
            assert started.wait(1)
            assert not future.done()
        future.result(timeout=3)
    with pytest.raises(CompanionSessionError):
        handle.current_policy()


def test_new_pin_replacement_revokes_existing_session(admitted):
    local, _, _, handle = admitted
    identifier, view = _awaiting(local)
    local.service.confirm(local.policy, view["pairing_id"], view["version"])
    with pytest.raises(CompanionSessionError):
        handle.current_policy()
