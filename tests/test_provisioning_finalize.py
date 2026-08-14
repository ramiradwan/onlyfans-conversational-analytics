"""Finalization of one verified provisioning tuple into runtime configuration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest
from dotenv import dotenv_values

from app.core.first_run import FirstRunConflictError, VerifiedGrantBindings
from app.persistence.auth import (
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.provisioning.finalize import (
    FinalizationRefused,
    FinalizationRequest,
    VerifiedAccountBinding,
    finalize_provisioning,
)
from app.security.grant_types import PROVISIONING_GRANT_TYPES


# Stated independently of the module under test so that dropping a required
# type from `PROVISIONING_GRANT_TYPES` cannot silently drop the case that covers it.
EXPECTED_GRANT_TYPES = (
    "creator_account_binding",
    "installation_grant",
    "license_entitlement",
    "membership_snapshot",
)
INSTANT = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
INSTALLATION_KEY_ID = "installation-key-1"
INSTALLATION_KEY_JKT = "installation-key-thumbprint-1"
ISSUER = "https://identity.example"
SUBJECT = "external-subject-1"
ACCOUNT_ID = "creator-account-1"
OTHER_ACCOUNT_ID = "creator-account-2"
ASSOCIATION_REQUEST_ID = "association-1"
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
BUNDLE_DIGEST = "b" * 64
REPORTED_PLATFORM_ID = "platform-creator-9999"


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class RecordingDigest:
    """Digest seam standing in for the separate bundle-digest subunit."""

    def __init__(self, value: str = BUNDLE_DIGEST) -> None:
        self.value = value
        self.calls: list[tuple[VerifiedGrantReference, ...]] = []

    def __call__(self, grants: tuple[VerifiedGrantReference, ...]) -> str:
        self.calls.append(grants)
        return self.value


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(INSTANT)


@pytest.fixture
def unkeyed_store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)


@pytest.fixture
def store(unkeyed_store: SQLiteAuthenticationStore) -> SQLiteAuthenticationStore:
    unkeyed_store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=INSTANT - timedelta(minutes=10),
        )
    )
    unkeyed_store.activate_installation_key(
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
    return unkeyed_store


@pytest.fixture
def data_directory(tmp_path: Path) -> Path:
    return tmp_path / "runtime-data"


def grant(
    grant_type: str,
    *,
    reference_suffix: str = "current",
    creator_account_id: str = ACCOUNT_ID,
    organization_id: str = ORGANIZATION_ID,
    installation_id: str = INSTALLATION_ID,
    installation_key_id: str = INSTALLATION_KEY_ID,
    installation_key_jkt: str = INSTALLATION_KEY_JKT,
    issuer: str = ISSUER,
    subject: str = SUBJECT,
    allowed_creator_account_ids: tuple[str, ...] = (ACCOUNT_ID,),
    membership_roles: tuple[str, ...] | None = ("owner",),
) -> VerifiedGrantReference:
    reference_id = f"{grant_type}-{reference_suffix}"
    membership = grant_type == "membership_snapshot"
    binding = grant_type == "creator_account_binding"
    entitlement = grant_type == "license_entitlement"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
        issuer=issuer,
        subject=subject,
        installation_id=installation_id,
        creator_account_id=creator_account_id if binding else None,
        valid_from=INSTANT - timedelta(hours=2),
        expires_at=INSTANT + timedelta(hours=2),
        verified_at=INSTANT - timedelta(minutes=5),
        organization_id=organization_id,
        installation_key_id=installation_key_id,
        installation_key_jkt=installation_key_jkt,
        membership_id="membership-1" if membership else None,
        approval_id="approval-1" if binding else None,
        approval_revision=1 if binding else None,
        entitlement_id="entitlement-1" if entitlement else None,
        product_id="product-1" if entitlement else None,
        allowed_creator_account_ids=(
            allowed_creator_account_ids if membership else None
        ),
        membership_roles=membership_roles if membership else None,
    )


def record_verified_tuple(
    store: SQLiteAuthenticationStore,
    *,
    omitted: str | None = None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[str, ...]:
    """Record one verified grant per required type, in the selection order."""

    fields = overrides or {}
    references: list[str] = []
    for grant_type in EXPECTED_GRANT_TYPES:
        if grant_type == omitted:
            continue
        reference = grant(grant_type, **fields.get(grant_type, {}))
        store.record_verified_grant(reference)
        references.append(reference.reference_id)
    return tuple(references)


def approve_candidate(
    store: SQLiteAuthenticationStore,
    *,
    association_request_id: str = ASSOCIATION_REQUEST_ID,
    creator_account_id: str = ACCOUNT_ID,
    approved: bool = True,
) -> None:
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id="onboarding-1",
            organization_id=ORGANIZATION_ID,
            creator_account_id=creator_account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=INSTANT - timedelta(minutes=3),
        )
    )
    if approved:
        store.approve_provisioning_candidate(
            association_request_id, resolved_at=INSTANT - timedelta(minutes=1)
        )


def request(
    *,
    association_request_id: str = ASSOCIATION_REQUEST_ID,
    detected_creator_account_id: str = ACCOUNT_ID,
    reported_platform_creator_id: str | None = None,
) -> FinalizationRequest:
    return FinalizationRequest(
        association_request_id=association_request_id,
        detected_creator_account_id=detected_creator_account_id,
        reported_platform_creator_id=reported_platform_creator_id,
    )


def finalize(
    store: SQLiteAuthenticationStore,
    data_directory: Path,
    *,
    body: FinalizationRequest | None = None,
    digest: RecordingDigest | None = None,
):
    return finalize_provisioning(
        store=store,
        request=body or request(),
        extension_id=EXTENSION_ID,
        bundle_digest=digest or RecordingDigest(),
        data_directory=data_directory,
    )


def test_the_required_tuple_is_the_four_signed_grant_types() -> None:
    assert PROVISIONING_GRANT_TYPES == EXPECTED_GRANT_TYPES


def test_finalization_writes_bindings_from_the_verified_four_grant_tuple(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    references = record_verified_tuple(store)
    approve_candidate(store)
    digest = RecordingDigest()

    finalized = finalize(store, data_directory, digest=digest)

    assert finalized.bindings == VerifiedGrantBindings(
        principal_id=SUBJECT,
        bridge_role="creator",
    )
    assert finalized.account == VerifiedAccountBinding(
        creator_account_id=ACCOUNT_ID,
        platform_creator_id=ACCOUNT_ID,
        grant_bundle_sha256=BUNDLE_DIGEST,
    )
    assert finalized.grant_reference_ids == references
    assert finalized.configuration.created is True
    assert (data_directory / "runtime.env").exists()
    assert [
        tuple(reference.grant_type for reference in call) for call in digest.calls
    ] == [EXPECTED_GRANT_TYPES]
    values = dotenv_values(finalized.configuration.configuration_file, interpolate=False)
    assert values["LOCAL_PRINCIPAL_ID"] == SUBJECT
    assert values["LOCAL_BRIDGE_ROLE"] == "creator"
    assert values["IDENTITY_BINDING_SOURCE"] == "verified_grants"
    # The verified account is carried out for a durable binding record and is
    # never pinned into installation configuration.
    assert ACCOUNT_ID not in (
        finalized.configuration.configuration_file.read_text(encoding="utf-8")
    )


def test_reported_platform_identity_is_never_adopted_as_a_binding(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    """The local platform identity comes from the signed creator account."""

    record_verified_tuple(store)
    approve_candidate(store)

    finalized = finalize(
        store,
        data_directory,
        body=request(reported_platform_creator_id=REPORTED_PLATFORM_ID),
    )

    assert finalized.account.platform_creator_id == ACCOUNT_ID
    assert REPORTED_PLATFORM_ID not in (
        finalized.configuration.configuration_file.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("omitted", EXPECTED_GRANT_TYPES)
def test_an_incomplete_grant_tuple_refuses_finalization(
    store: SQLiteAuthenticationStore, data_directory: Path, omitted: str
) -> None:
    record_verified_tuple(store, omitted=omitted)
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "incomplete_grant_set"
    assert not (data_directory / "runtime.env").exists()


def test_a_second_current_grant_of_one_type_is_refused_rather_than_selected(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    """No first, newest, or last-seen selection resolves a competing tuple."""

    record_verified_tuple(store)
    store.record_verified_grant(
        grant(
            "creator_account_binding",
            reference_suffix="second",
            creator_account_id=OTHER_ACCOUNT_ID,
        )
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "ambiguous_grant_set"
    assert not (data_directory / "runtime.env").exists()


def test_a_grant_bound_to_another_installation_key_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(
        store,
        overrides={
            "license_entitlement": {"installation_key_jkt": "other-thumbprint"}
        },
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "unbound_installation_key"


def test_finalization_without_a_local_installation_key_is_refused(
    unkeyed_store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    """Key identity is read from the local store, never from the grants."""

    record_verified_tuple(unkeyed_store)
    approve_candidate(unkeyed_store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(unkeyed_store, data_directory)

    assert refusal.value.reason == "installation_key_unavailable"
    assert not (data_directory / "runtime.env").exists()


def test_a_grant_naming_a_second_external_principal_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(
        store,
        overrides={"license_entitlement": {"subject": "external-subject-2"}},
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "incoherent_grant_set"


def test_a_grant_naming_another_organization_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(
        store,
        overrides={"installation_grant": {"organization_id": "organization-2"}},
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "incoherent_grant_set"


def test_a_detected_account_other_than_the_signed_account_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(store)
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(
            store,
            data_directory,
            body=request(detected_creator_account_id=OTHER_ACCOUNT_ID),
        )

    assert refusal.value.reason == "detected_account_mismatch"
    assert not (data_directory / "runtime.env").exists()


def test_a_binding_for_an_unapproved_account_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(store)
    approve_candidate(store, creator_account_id=OTHER_ACCOUNT_ID)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "unapproved_creator_account"


def test_finalization_without_an_approved_candidate_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(store)
    approve_candidate(store, approved=False)

    with pytest.raises(FinalizationRefused) as pending:
        finalize(store, data_directory)
    with pytest.raises(FinalizationRefused) as unknown:
        finalize(
            store,
            data_directory,
            body=request(association_request_id="association-unknown"),
        )

    assert pending.value.reason == "account_approval_missing"
    assert unknown.value.reason == "account_approval_missing"


def test_an_expired_grant_tuple_is_refused_by_store_validation(
    store: SQLiteAuthenticationStore, data_directory: Path, clock: MutableClock
) -> None:
    record_verified_tuple(store)
    approve_candidate(store)
    clock.value = INSTANT + timedelta(hours=3)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "grant_set_not_current"
    assert not (data_directory / "runtime.env").exists()


def test_a_revoked_grant_leaves_the_tuple_incomplete(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    references = record_verified_tuple(store)
    approve_candidate(store)
    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, references[0]),
        reason="test",
    )

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "incomplete_grant_set"


def test_a_membership_that_disallows_the_account_is_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(
        store,
        overrides={
            "membership_snapshot": {
                "allowed_creator_account_ids": (OTHER_ACCOUNT_ID,)
            }
        },
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "grant_set_not_current"


@pytest.mark.parametrize(
    ("membership_roles", "expected_role"),
    [(("owner",), "creator"), (("creator_operator",), "operator")],
)
def test_the_bridge_role_comes_from_the_verified_membership(
    store: SQLiteAuthenticationStore,
    data_directory: Path,
    membership_roles: tuple[str, ...],
    expected_role: str,
) -> None:
    record_verified_tuple(
        store,
        overrides={"membership_snapshot": {"membership_roles": membership_roles}},
    )
    approve_candidate(store)

    finalized = finalize(store, data_directory)

    assert finalized.bindings.bridge_role == expected_role
    values = dotenv_values(finalized.configuration.configuration_file, interpolate=False)
    assert values["LOCAL_BRIDGE_ROLE"] == expected_role


def test_membership_roles_without_a_local_bridge_role_are_refused(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(
        store,
        overrides={
            "membership_snapshot": {"membership_roles": ("delegated_installer",)}
        },
    )
    approve_candidate(store)

    with pytest.raises(FinalizationRefused) as refusal:
        finalize(store, data_directory)

    assert refusal.value.reason == "membership_role_unavailable"


def test_matching_configuration_resumes_without_rewriting_it(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    record_verified_tuple(store)
    approve_candidate(store)

    first = finalize(store, data_directory)
    written = first.configuration.configuration_file.read_bytes()
    second = finalize(store, data_directory)

    assert first.configuration.created is True
    assert second.configuration.created is False
    assert second.bindings == first.bindings
    assert second.configuration.configuration_file.read_bytes() == written


def test_another_verified_account_leaves_installation_configuration_unchanged(
    store: SQLiteAuthenticationStore, data_directory: Path
) -> None:
    references = record_verified_tuple(
        store,
        overrides={
            "membership_snapshot": {
                "allowed_creator_account_ids": (ACCOUNT_ID, OTHER_ACCOUNT_ID)
            }
        },
    )
    approve_candidate(store)
    first = finalize(store, data_directory)
    written = first.configuration.configuration_file.read_bytes()

    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, references[0]),
        reason="test",
    )
    store.record_verified_grant(
        grant(
            "creator_account_binding",
            reference_suffix="second",
            creator_account_id=OTHER_ACCOUNT_ID,
        )
    )
    approve_candidate(
        store,
        association_request_id="association-2",
        creator_account_id=OTHER_ACCOUNT_ID,
    )

    second = finalize(
        store,
        data_directory,
        body=request(
            association_request_id="association-2",
            detected_creator_account_id=OTHER_ACCOUNT_ID,
        ),
    )

    # Installation identity is unchanged, so the second account reuses the
    # existing configuration and is carried out for a durable binding record.
    assert second.configuration.created is False
    assert second.account.creator_account_id == OTHER_ACCOUNT_ID
    assert first.account.creator_account_id == ACCOUNT_ID
    assert first.configuration.configuration_file.read_bytes() == written
    assert OTHER_ACCOUNT_ID not in (
        first.configuration.configuration_file.read_text(encoding="utf-8")
    )
