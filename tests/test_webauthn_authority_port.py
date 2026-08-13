from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.auth import (
    InstallationKeyReference,
    InstallationKeyReservation,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.security.runtime_policy import AuthContext
from app.security.webauthn import (
    RegistrationAuthority,
    SessionAuthority,
    WebAuthnAuthorityPort,
    WebAuthnAuthorityResult,
)


INSTANT = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
PRINCIPAL_ID = "principal-1"
ACCOUNT_ID = "creator-1"
ISSUER = "https://identity.example"
SUBJECT = "external-subject-1"
ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
INSTALLATION_KEY_ID = "installation-key-1"
INSTALLATION_KEY_JKT = "installation-key-thumbprint-1"
CREDENTIAL_ID = "credential-1"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(INSTANT)


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    authority_store = SQLiteAuthenticationStore(
        tmp_path / "auth.sqlite3", clock=clock
    )
    authority_store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=INSTANT - timedelta(minutes=10),
        )
    )
    authority_store.activate_installation_key(
        InstallationKeyReference(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            installation_key_id=INSTALLATION_KEY_ID,
            installation_key_jkt=INSTALLATION_KEY_JKT,
            public_key_jwk='{"kty":"EC"}',
            created_at=INSTANT - timedelta(minutes=10),
            activated_at=INSTANT - timedelta(minutes=9),
        )
    )
    return authority_store


@pytest.fixture
def identity() -> AuthContext:
    return AuthContext(
        principal_id=PRINCIPAL_ID,
        creator_account_id=ACCOUNT_ID,
        role="creator",
    )


def test_port_derives_registration_and_session_authority_from_verified_state(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    references = record_required_grants(store)
    store.register_webauthn_credential(
        WebAuthnCredential(
            credential_id=CREDENTIAL_ID,
            principal_id=PRINCIPAL_ID,
            external_issuer=ISSUER,
            external_subject=SUBJECT,
            installation_id=INSTALLATION_ID,
            public_key=b"public-key-material",
            signature_count=0,
            enrolled_at=INSTANT - timedelta(minutes=1),
        )
    )
    policy = store.build_runtime_policy(identity)
    port = WebAuthnAuthorityPort(store, clock=clock)

    registration = port.registration_authority(policy)
    session = port.session_authority(policy)

    assert registration.result is WebAuthnAuthorityResult.AUTHORIZED
    assert registration.authority == RegistrationAuthority(
        principal_id=PRINCIPAL_ID,
        external_issuer=ISSUER,
        external_subject=SUBJECT,
        installation_id=INSTALLATION_ID,
    )
    assert session.result is WebAuthnAuthorityResult.AUTHORIZED
    assert session.authority == SessionAuthority(
        principal_id=PRINCIPAL_ID,
        credential_id=CREDENTIAL_ID,
        creator_account_id=ACCOUNT_ID,
        role="creator",
        grant_reference_ids=references,
    )


def test_port_derives_login_account_and_role_from_verified_grants_without_identity(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
) -> None:
    references = record_required_grants(store)
    store.register_webauthn_credential(
        WebAuthnCredential(
            credential_id=CREDENTIAL_ID,
            principal_id=PRINCIPAL_ID,
            external_issuer=ISSUER,
            external_subject=SUBJECT,
            installation_id=INSTALLATION_ID,
            public_key=b"public-key-material",
            signature_count=0,
            enrolled_at=INSTANT - timedelta(minutes=1),
        )
    )

    decision = WebAuthnAuthorityPort(store, clock=clock).session_authority(
        store.build_runtime_policy()
    )

    assert decision.result is WebAuthnAuthorityResult.AUTHORIZED
    assert decision.authority == SessionAuthority(
        principal_id=PRINCIPAL_ID,
        credential_id=CREDENTIAL_ID,
        creator_account_id=ACCOUNT_ID,
        role="creator",
        grant_reference_ids=references,
    )


def test_port_refuses_identity_account_that_differs_from_binding_grant(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    record_required_grants(store)
    mismatched_identity = replace(identity, creator_account_id="other-account")

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(
        store.build_runtime_policy(mismatched_identity)
    )

    assert decision.result is WebAuthnAuthorityResult.IDENTITY_GRANT_MISMATCH
    assert decision.authority is None


def test_port_refuses_ambiguous_current_bindings_without_identity(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
) -> None:
    record_required_grants(store)
    store.record_verified_grant(
        replace(
            required_grant("creator_account_binding", reference_suffix="other"),
            creator_account_id="other-account",
        )
    )

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(
        store.build_runtime_policy()
    )

    assert decision.result is WebAuthnAuthorityResult.ACCOUNT_BINDING_AMBIGUOUS
    assert decision.authority is None


def test_port_refuses_with_required_grant_missing(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    record_required_grants(store, omitted="membership_snapshot")
    policy = store.build_runtime_policy(identity)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.REQUIRED_GRANT_MISSING
    assert decision.authority is None


def test_port_refuses_with_required_grant_expired(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    record_required_grants(store, expired="membership_snapshot")
    policy = store.build_runtime_policy(identity)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED
    assert decision.authority is None


def test_port_refuses_with_required_grant_revoked(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    references = record_required_grants(store)
    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, references[1]),
        reason="membership withdrawn",
    )
    policy = store.build_runtime_policy(identity)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.REQUIRED_GRANT_REVOKED
    assert decision.authority is None


def test_port_refuses_a_policy_superseded_after_authentication(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    record_required_grants(store)
    policy = store.build_runtime_policy(identity)
    store.record_verified_grant(
        required_grant(
            "membership_snapshot",
            reference_suffix="replacement-candidate",
        )
    )

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.POLICY_NOT_CURRENT
    assert decision.authority is None


def test_port_uses_exact_grant_references_already_carried_by_policy(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    references = record_required_grants(store)
    unrelated = replace(
        required_grant("membership_snapshot", reference_suffix="unrelated"),
        organization_id="other-organization",
        verified_at=INSTANT - timedelta(minutes=1),
    )
    store.record_verified_grant(unrelated)
    policy = store.build_runtime_policy_from_grants(identity, references)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.AUTHORIZED
    assert decision.authority == RegistrationAuthority(
        principal_id=PRINCIPAL_ID,
        external_issuer=ISSUER,
        external_subject=SUBJECT,
        installation_id=INSTALLATION_ID,
    )


@pytest.mark.parametrize(
    ("grant_type", "changes", "expected"),
    [
        (
            "membership_snapshot",
            {"allowed_creator_account_ids": ("other-account",)},
            WebAuthnAuthorityResult.MEMBERSHIP_ACCOUNT_MISMATCH,
        ),
        (
            "creator_account_binding",
            {"creator_account_id": "other-account"},
            WebAuthnAuthorityResult.MEMBERSHIP_ACCOUNT_MISMATCH,
        ),
        (
            "membership_snapshot",
            {"organization_id": "other-organization"},
            WebAuthnAuthorityResult.ORGANIZATION_MISMATCH,
        ),
        (
            "membership_snapshot",
            {"installation_key_jkt": "other-thumbprint"},
            WebAuthnAuthorityResult.INSTALLATION_KEY_MISMATCH,
        ),
        (
            "membership_snapshot",
            {"subject": "other-external-subject"},
            WebAuthnAuthorityResult.EXTERNAL_PRINCIPAL_MISMATCH,
        ),
    ],
)
def test_port_refuses_each_cross_grant_authority_mismatch(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
    grant_type: str,
    changes: dict[str, object],
    expected: WebAuthnAuthorityResult,
) -> None:
    for required_type in (
        "installation_grant",
        "membership_snapshot",
        "creator_account_binding",
    ):
        grant = required_grant(required_type)
        if required_type == grant_type:
            grant = replace(grant, **changes)
        store.record_verified_grant(grant)
    policy = store.build_runtime_policy(identity)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is expected
    assert decision.authority is None


def test_port_refuses_revoked_principal_authority(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    identity: AuthContext,
) -> None:
    record_required_grants(store)
    store.revoke(
        RevocationKey(RevocationScopeType.PRINCIPAL, PRINCIPAL_ID),
        reason="principal disabled",
    )
    policy = store.build_runtime_policy(identity)

    decision = WebAuthnAuthorityPort(store, clock=clock).registration_authority(policy)

    assert decision.result is WebAuthnAuthorityResult.AUTHORITY_REVOKED
    assert decision.authority is None


def record_required_grants(
    store: SQLiteAuthenticationStore,
    *,
    omitted: str | None = None,
    expired: str | None = None,
) -> tuple[str, ...]:
    references: list[str] = []
    for grant_type in (
        "installation_grant",
        "membership_snapshot",
        "creator_account_binding",
    ):
        if grant_type == omitted:
            continue
        grant = required_grant(grant_type, expired=grant_type == expired)
        store.record_verified_grant(grant)
        references.append(grant.reference_id)
    return tuple(references)


def required_grant(
    grant_type: str,
    *,
    expired: bool = False,
    reference_suffix: str = "current",
) -> VerifiedGrantReference:
    reference_id = f"{grant_type}-{reference_suffix}"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
        issuer=ISSUER,
        subject=SUBJECT,
        installation_id=INSTALLATION_ID,
        creator_account_id=(ACCOUNT_ID if grant_type == "creator_account_binding" else None),
        valid_from=INSTANT - timedelta(hours=2),
        expires_at=(
            INSTANT - timedelta(minutes=1)
            if expired
            else INSTANT + timedelta(hours=2)
        ),
        verified_at=INSTANT - timedelta(minutes=5),
        organization_id=ORGANIZATION_ID,
        installation_key_id=INSTALLATION_KEY_ID,
        installation_key_jkt=INSTALLATION_KEY_JKT,
        membership_id=("membership-1" if grant_type == "membership_snapshot" else None),
        allowed_creator_account_ids=(
            (ACCOUNT_ID,) if grant_type == "membership_snapshot" else None
        ),
        membership_roles=(
            ("owner",) if grant_type == "membership_snapshot" else None
        ),
    )
