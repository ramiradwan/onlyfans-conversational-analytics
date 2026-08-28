"""Durable authorized-account bindings and the eligible-account resolver."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    AccountBindingRefusal,
    AccountBindingRefused,
    AuthorizedAccountBinding,
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
    FinalizationRequest,
    authorize_finalized_account,
    finalize_provisioning,
)
from app.security.account_bindings import (
    ACCOUNT_BINDING_GRANT_TYPE,
    MEMBERSHIP_GRANT_TYPE,
    AccountResolutionRefusal,
    AccountResolutionRefused,
    EligibleAccount,
    eligible_account_for_policy,
    eligible_accounts,
    revoke_account_binding,
    sole_eligible_account,
)
from app.security.runtime_policy import AuthContext, RuntimeAuthorizationDenied


# Declared here rather than derived from the modules under test: deleting a
# reason code must fail this agreement instead of deleting its own coverage.
EXPECTED_RESOLUTION_REFUSALS = (
    "no_eligible_account",
    "ambiguous_account_context",
)
EXPECTED_BINDING_REFUSALS = (
    "additional_account_binding_unsupported",
    "account_binding_already_recorded",
)
# The grant types the resolver's coherence check must find in a binding's basis,
# stated independently of the resolver's own constants.
EXPECTED_COHERENCE_GRANT_TYPES = ("creator_account_binding", "membership_snapshot")

INSTANT = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
INSTALLATION_KEY_ID = "installation-key-1"
INSTALLATION_KEY_JKT = "installation-key-thumbprint-1"
ISSUER = "https://identity.example"
SUBJECT = "external-subject-1"
ACCOUNT_A = "creator-account-a"
ACCOUNT_B = "creator-account-b"
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
GRANT_TYPES = (
    "creator_account_binding",
    "installation_grant",
    "license_entitlement",
    "membership_snapshot",
)


def digest_for(account_id: str) -> str:
    return hashlib.sha256(f"bundle-{account_id}".encode()).hexdigest()


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
    created = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    created.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=INSTANT - timedelta(minutes=10),
        )
    )
    created.activate_installation_key(
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
    return created


def grant(
    grant_type: str,
    *,
    account_id: str = ACCOUNT_A,
    binding_account_id: str | None = None,
    allowed_account_ids: tuple[str, ...] | None = None,
    valid_from: datetime = INSTANT - timedelta(hours=2),
    expires_at: datetime = INSTANT + timedelta(hours=2),
) -> VerifiedGrantReference:
    reference_id = f"{grant_type}-{account_id}"
    membership = grant_type == "membership_snapshot"
    binding = grant_type == "creator_account_binding"
    entitlement = grant_type == "license_entitlement"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
        issuer=ISSUER,
        subject=SUBJECT,
        installation_id=INSTALLATION_ID,
        creator_account_id=(
            (binding_account_id or account_id) if binding else None
        ),
        valid_from=valid_from,
        expires_at=expires_at,
        verified_at=INSTANT - timedelta(minutes=5),
        organization_id=ORGANIZATION_ID,
        installation_key_id=INSTALLATION_KEY_ID,
        installation_key_jkt=INSTALLATION_KEY_JKT,
        membership_id=f"membership-{account_id}" if membership else None,
        approval_id=f"approval-{account_id}" if binding else None,
        approval_revision=1 if binding else None,
        entitlement_id=f"entitlement-{account_id}" if entitlement else None,
        product_id="product-1" if entitlement else None,
        allowed_creator_account_ids=(
            (allowed_account_ids if allowed_account_ids is not None else (account_id,))
            if membership
            else None
        ),
        membership_roles=("owner",) if membership else None,
    )


def record_grants(
    store: SQLiteAuthenticationStore,
    *,
    account_id: str = ACCOUNT_A,
    **overrides: object,
) -> tuple[str, ...]:
    """Record one current grant per required type for one account."""

    references: list[str] = []
    for grant_type in GRANT_TYPES:
        reference = grant(grant_type, account_id=account_id, **overrides)  # type: ignore[arg-type]
        store.record_verified_grant(reference)
        references.append(reference.reference_id)
    return tuple(references)


def approve_candidate(
    store: SQLiteAuthenticationStore, *, account_id: str = ACCOUNT_A
) -> str:
    association_request_id = f"association-{account_id}"
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id=f"onboarding-{account_id}",
            organization_id=ORGANIZATION_ID,
            creator_account_id=account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=INSTANT - timedelta(minutes=3),
        )
    )
    store.approve_provisioning_candidate(
        association_request_id, resolved_at=INSTANT - timedelta(minutes=1)
    )
    return association_request_id


def binding_for(
    store: SQLiteAuthenticationStore,
    *,
    account_id: str = ACCOUNT_A,
    **overrides: object,
) -> AuthorizedAccountBinding:
    """Build one recordable binding, with its grants and candidate in place."""

    references = record_grants(store, account_id=account_id, **overrides)
    association_request_id = approve_candidate(store, account_id=account_id)
    return AuthorizedAccountBinding(
        creator_account_id=account_id,
        installation_id=INSTALLATION_ID,
        platform_creator_id=account_id,
        association_request_id=association_request_id,
        grant_bundle_sha256=digest_for(account_id),
        authorized_at=INSTANT - timedelta(minutes=1),
        grant_reference_ids=references,
    )


def authorize(
    store: SQLiteAuthenticationStore,
    *,
    account_id: str = ACCOUNT_A,
    **overrides: object,
) -> AuthorizedAccountBinding:
    binding = binding_for(store, account_id=account_id, **overrides)
    store.record_authorized_account_binding(binding)
    return binding


def force_second_binding(
    store: SQLiteAuthenticationStore, binding: AuthorizedAccountBinding
) -> None:
    """Insert a binding past the writer's pilot guard.

    The resolver must fail closed on an ambiguous set however that set arose,
    so its many-account vectors are built without relying on the writer.
    """

    with store.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO authorized_account_bindings (
                creator_account_id, installation_id, platform_creator_id,
                association_request_id, grant_bundle_sha256, authorized_at,
                revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                binding.creator_account_id,
                binding.installation_id,
                binding.platform_creator_id,
                binding.association_request_id,
                binding.grant_bundle_sha256,
                binding.authorized_at.isoformat(timespec="microseconds"),
            ),
        )
        connection.executemany(
            """
            INSERT INTO authorized_account_binding_grants (
                creator_account_id, grant_reference_id
            ) VALUES (?, ?)
            """,
            (
                (binding.creator_account_id, reference_id)
                for reference_id in binding.grant_reference_ids
            ),
        )


def policy_for(store: SQLiteAuthenticationStore, account_id: str):
    return store.build_runtime_policy(
        AuthContext(
            principal_id=SUBJECT, creator_account_id=account_id, role="creator"
        )
    )


def test_the_resolution_refusal_codes_are_the_declared_pair() -> None:
    assert get_args(AccountResolutionRefusal) == EXPECTED_RESOLUTION_REFUSALS


def test_the_binding_refusal_codes_are_the_declared_pair() -> None:
    assert get_args(AccountBindingRefusal) == EXPECTED_BINDING_REFUSALS


def test_zero_authorized_accounts_resolve_to_an_empty_set(
    store: SQLiteAuthenticationStore,
) -> None:
    """An installation that has authorized nothing is an ordinary state."""

    assert eligible_accounts(store, now=INSTANT) == ()


def test_zero_eligible_accounts_refuse_a_sole_account_request(
    store: SQLiteAuthenticationStore,
) -> None:
    with pytest.raises(AccountResolutionRefused) as refusal:
        sole_eligible_account(store, now=INSTANT)

    assert refusal.value.reason == "no_eligible_account"


def test_one_authorized_account_resolves_with_its_bundle_provenance(
    store: SQLiteAuthenticationStore,
) -> None:
    """The digest finalization produced is readable back from the record."""

    binding = authorize(store)

    resolved = sole_eligible_account(store, now=INSTANT)

    assert resolved == EligibleAccount(
        creator_account_id=ACCOUNT_A,
        platform_creator_id=ACCOUNT_A,
        grant_bundle_sha256=digest_for(ACCOUNT_A),
        grant_reference_ids=tuple(sorted(binding.grant_reference_ids)),
    )
    assert eligible_accounts(store, now=INSTANT) == (resolved,)


def test_many_eligible_accounts_resolve_as_a_set(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(store, account_id=ACCOUNT_A)
    force_second_binding(store, binding_for(store, account_id=ACCOUNT_B))

    resolved = eligible_accounts(store, now=INSTANT)

    assert [account.creator_account_id for account in resolved] == [
        ACCOUNT_A,
        ACCOUNT_B,
    ]


def test_many_eligible_accounts_refuse_a_sole_account_request(
    store: SQLiteAuthenticationStore,
) -> None:
    """The invariant: ambiguity fails rather than defaulting to one account."""

    authorize(store, account_id=ACCOUNT_A)
    force_second_binding(store, binding_for(store, account_id=ACCOUNT_B))

    with pytest.raises(AccountResolutionRefused) as refusal:
        sole_eligible_account(store, now=INSTANT)

    assert refusal.value.reason == "ambiguous_account_context"


def test_a_binding_grant_naming_another_account_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(store, account_id=ACCOUNT_A, binding_account_id=ACCOUNT_B)

    assert eligible_accounts(store, now=INSTANT) == ()


def test_a_membership_excluding_the_account_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(store, account_id=ACCOUNT_A, allowed_account_ids=(ACCOUNT_B,))

    assert eligible_accounts(store, now=INSTANT) == ()


def test_the_coherence_check_names_both_declared_grant_types() -> None:
    """Both halves of the comparison have a vector of their own above."""

    assert (ACCOUNT_BINDING_GRANT_TYPE, MEMBERSHIP_GRANT_TYPE) == (
        EXPECTED_COHERENCE_GRANT_TYPES
    )


def test_an_expired_grant_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    """Expiry alone withdraws eligibility; nothing else about the record moved."""

    authorize(
        store,
        valid_from=INSTANT - timedelta(hours=4),
        expires_at=INSTANT - timedelta(hours=1),
    )

    assert eligible_accounts(store, now=INSTANT) == ()


def test_a_grant_not_yet_valid_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(
        store,
        valid_from=INSTANT + timedelta(hours=1),
        expires_at=INSTANT + timedelta(hours=4),
    )

    assert eligible_accounts(store, now=INSTANT) == ()


def test_an_expiring_grant_stops_resolving_as_time_passes(
    store: SQLiteAuthenticationStore,
) -> None:
    """The same record resolves before its grants expire and not after."""

    authorize(store)

    assert len(eligible_accounts(store, now=INSTANT)) == 1
    assert eligible_accounts(store, now=INSTANT + timedelta(hours=3)) == ()


def test_a_revoked_grant_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    """Revoking one grant of the basis withdraws the account's eligibility."""

    binding = authorize(store)

    store.revoke(
        RevocationKey(
            RevocationScopeType.VERIFIED_GRANT, binding.grant_reference_ids[0]
        )
    )

    assert eligible_accounts(store, now=INSTANT) == ()


def test_a_revoked_account_scope_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    """The account revocation scope withdraws eligibility on its own."""

    authorize(store)

    store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_A))

    assert eligible_accounts(store, now=INSTANT) == ()


def test_a_revoked_binding_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(store)

    assert store.revoke_authorized_account_binding(ACCOUNT_A) is True

    assert eligible_accounts(store, now=INSTANT) == ()
    assert [
        binding.revoked_at is not None
        for binding in store.authorized_account_bindings()
    ] == [True]


def test_a_binding_row_marked_revoked_is_not_eligible(
    store: SQLiteAuthenticationStore,
) -> None:
    """The binding's own revocation withdraws eligibility unaided.

    Only the column is set here, so no revocation scope can stand in for it.
    """

    authorize(store)
    with store.database.transaction() as connection:
        connection.execute(
            """
            UPDATE authorized_account_bindings SET revoked_at = ?
            WHERE creator_account_id = ?
            """,
            (INSTANT.isoformat(timespec="microseconds"), ACCOUNT_A),
        )

    assert (
        store.scope_is_revoked(
            RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_A)
        )
        is False
    )
    assert eligible_accounts(store, now=INSTANT) == ()


def test_a_binding_cannot_name_a_grant_the_store_does_not_hold(
    store: SQLiteAuthenticationStore,
) -> None:
    """The basis is a foreign key, so it cannot point at an absent grant."""

    binding = authorize(store)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with store.database.transaction() as connection:
            connection.execute(
                """
                UPDATE authorized_account_binding_grants
                SET grant_reference_id = ?
                WHERE grant_reference_id = ?
                """,
                ("membership_snapshot-absent", binding.grant_reference_ids[3]),
            )

    assert len(eligible_accounts(store, now=INSTANT)) == 1


def test_a_second_account_binding_is_refused_with_its_own_reason_code(
    store: SQLiteAuthenticationStore,
) -> None:
    """Pilot cardinality: the second account is refused, not silently chosen."""

    authorize(store, account_id=ACCOUNT_A)
    second = binding_for(store, account_id=ACCOUNT_B)

    with pytest.raises(AccountBindingRefused) as refusal:
        store.record_authorized_account_binding(second)

    assert refusal.value.reason == "additional_account_binding_unsupported"
    assert [
        binding.creator_account_id for binding in store.authorized_account_bindings()
    ] == [ACCOUNT_A]


def test_recording_the_same_account_twice_is_refused(
    store: SQLiteAuthenticationStore,
) -> None:
    """A repeat never overwrites the provenance of the first authorization."""

    binding = authorize(store)
    repeat = AuthorizedAccountBinding(
        creator_account_id=binding.creator_account_id,
        installation_id=binding.installation_id,
        platform_creator_id=binding.platform_creator_id,
        association_request_id=binding.association_request_id,
        grant_bundle_sha256="c" * 64,
        authorized_at=INSTANT,
        grant_reference_ids=binding.grant_reference_ids,
    )

    with pytest.raises(AccountBindingRefused) as refusal:
        store.record_authorized_account_binding(repeat)

    assert refusal.value.reason == "account_binding_already_recorded"
    assert store.authorized_account_bindings()[0].grant_bundle_sha256 == digest_for(
        ACCOUNT_A
    )


def test_revoking_one_account_does_not_open_a_slot_for_another(
    store: SQLiteAuthenticationStore,
) -> None:
    authorize(store, account_id=ACCOUNT_A)
    store.revoke_authorized_account_binding(ACCOUNT_A)
    second = binding_for(store, account_id=ACCOUNT_B)

    with pytest.raises(AccountBindingRefused) as refusal:
        store.record_authorized_account_binding(second)

    assert refusal.value.reason == "additional_account_binding_unsupported"


def test_a_binding_with_an_empty_account_is_refused(
    store: SQLiteAuthenticationStore,
) -> None:
    """A record with no account scope cannot be created."""

    binding = binding_for(store)
    empty = AuthorizedAccountBinding(
        creator_account_id="",
        installation_id=binding.installation_id,
        platform_creator_id=binding.platform_creator_id,
        association_request_id=binding.association_request_id,
        grant_bundle_sha256=binding.grant_bundle_sha256,
        authorized_at=binding.authorized_at,
        grant_reference_ids=binding.grant_reference_ids,
    )

    with pytest.raises(ValueError, match="must not be empty"):
        store.record_authorized_account_binding(empty)


def test_a_session_scoped_to_one_account_cannot_read_another_accounts_binding(
    store: SQLiteAuthenticationStore,
) -> None:
    """Account A's session resolves A and is refused B, which is eligible."""

    authorize(store, account_id=ACCOUNT_A)
    force_second_binding(store, binding_for(store, account_id=ACCOUNT_B))

    own = eligible_account_for_policy(
        store, policy_for(store, ACCOUNT_A), now=INSTANT
    )
    assert own.creator_account_id == ACCOUNT_A

    with pytest.raises(AccountResolutionRefused) as refusal:
        eligible_account_for_policy(
            store, policy_for(store, "creator-account-unauthorized"), now=INSTANT
        )
    assert refusal.value.reason == "no_eligible_account"


def test_a_session_scoped_to_one_account_cannot_mutate_another_accounts_binding(
    store: SQLiteAuthenticationStore,
) -> None:
    """Revocation is refused across accounts, and B keeps its authority."""

    authorize(store, account_id=ACCOUNT_A)
    force_second_binding(store, binding_for(store, account_id=ACCOUNT_B))
    policy = policy_for(store, ACCOUNT_A)

    with pytest.raises(RuntimeAuthorizationDenied):
        revoke_account_binding(store, policy, ACCOUNT_B)

    assert [
        account.creator_account_id
        for account in eligible_accounts(store, now=INSTANT)
    ] == [ACCOUNT_A, ACCOUNT_B]
    assert revoke_account_binding(store, policy, ACCOUNT_A) is True
    assert [
        account.creator_account_id
        for account in eligible_accounts(store, now=INSTANT)
    ] == [ACCOUNT_B]


def test_a_policy_with_no_account_scope_fails_closed(
    store: SQLiteAuthenticationStore,
) -> None:
    """An unscoped policy is refused before any binding is consulted."""

    authorize(store, account_id=ACCOUNT_A)
    unscoped = store.build_runtime_policy()

    with pytest.raises(RuntimeAuthorizationDenied):
        eligible_account_for_policy(store, unscoped, now=INSTANT)

    with pytest.raises(RuntimeAuthorizationDenied):
        revoke_account_binding(store, unscoped, ACCOUNT_A)


def test_finalization_lands_its_account_and_bundle_digest_durably(
    store: SQLiteAuthenticationStore, tmp_path: Path
) -> None:
    """The provenance A23 left finalization carrying now has a home."""

    record_grants(store, account_id=ACCOUNT_A)
    association_request_id = approve_candidate(store, account_id=ACCOUNT_A)
    request = FinalizationRequest(
        association_request_id=association_request_id,
        detected_creator_account_id=ACCOUNT_A,
    )
    finalized = finalize_provisioning(
        store=store,
        request=request,
        extension_id=EXTENSION_ID,
        data_directory=tmp_path / "runtime-data",
    )

    recorded = authorize_finalized_account(
        store=store,
        request=request,
        finalized=finalized,
        authorized_at=INSTANT,
    )

    assert recorded.grant_bundle_sha256 == finalized.account.grant_bundle_sha256
    assert recorded.creator_account_id == finalized.account.creator_account_id
    assert recorded.installation_id == INSTALLATION_ID
    resolved = sole_eligible_account(store, now=INSTANT)
    assert resolved.grant_bundle_sha256 == finalized.account.grant_bundle_sha256
    assert resolved.grant_reference_ids == tuple(sorted(finalized.grant_reference_ids))


def test_the_durable_record_holds_state_no_grant_reference_carries(
    store: SQLiteAuthenticationStore,
) -> None:
    """The bundle digest is a property of the tuple, not of any one grant.

    Recording the tuple's digest on any single grant reference would attribute
    a four-grant value to one grant, so no existing column can hold it.
    """

    binding = authorize(store)

    digests = {
        reference_id: store.verified_grant(reference_id).grant_digest
        for reference_id in binding.grant_reference_ids
    }
    assert binding.grant_bundle_sha256 not in set(digests.values())
    assert len(binding.grant_reference_ids) == len(GRANT_TYPES)
