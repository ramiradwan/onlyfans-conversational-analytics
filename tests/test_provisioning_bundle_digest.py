"""Canonical bundle digest over the verified provisioning grant tuple."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from itertools import permutations
from typing import Any

import pytest

from app.core.first_run import VerifiedGrantBindings
from app.persistence.auth import VerifiedGrantReference
from app.provisioning.finalize import (
    REQUIRED_GRANT_TYPES,
    FinalizationRefused,
    finalize_provisioning,
    grant_bundle_digest,
    verified_grant_bindings,
)


# Stated independently of the module under test so that dropping a type from
# `REQUIRED_GRANT_TYPES` cannot silently delete the case that covers it.
EXPECTED_GRANT_TYPES = (
    "creator_account_binding",
    "installation_grant",
    "license_entitlement",
    "membership_snapshot",
)

# Fixed vectors. The expected bundle values were computed outside this module
# over the canonical document `[["<grant type>","<grant digest>"],...]` with the
# pairs sorted by grant type, so a change in canonicalization appears here as a
# value change rather than only as a structural one.
GRANT_DIGESTS = {
    "creator_account_binding": "aa" * 32,
    "installation_grant": "bb" * 32,
    "license_entitlement": "cc" * 32,
    "membership_snapshot": "dd" * 32,
}
EXPECTED_BUNDLE_DIGEST = (
    "a08895cd60f70c76c5aadc91e313717ffd3df8eb2864dfb89d8cdc667a5cb8b9"
)
OTHER_GRANT_DIGEST = "ee" * 32
EXPECTED_REBOUND_BUNDLE_DIGEST = (
    "0f6794134c1366db9d9eaf29fd809bf5ec131b0c4af95c38ef2783d32b876c33"
)

INSTANT = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)
ISSUER = "https://identity.example"
SUBJECT = "external-subject-1"
INSTALLATION_ID = "installation-1"
ACCOUNT_ID = "creator-account-1"


def reference(
    grant_type: str,
    *,
    grant_digest: str | None = None,
    **overrides: Any,
) -> VerifiedGrantReference:
    values: dict[str, Any] = {
        "reference_id": f"vg1.{grant_type}",
        "grant_identifier": f"jti-{grant_type}",
        "grant_type": grant_type,
        "grant_digest": grant_digest or GRANT_DIGESTS[grant_type],
        "issuer": ISSUER,
        "subject": SUBJECT,
        "installation_id": INSTALLATION_ID,
        "creator_account_id": (
            ACCOUNT_ID if grant_type == "creator_account_binding" else None
        ),
        "valid_from": INSTANT - timedelta(minutes=5),
        "expires_at": INSTANT + timedelta(hours=1),
        "verified_at": INSTANT,
    }
    values.update(overrides)
    return VerifiedGrantReference(**values)


def vector(
    *,
    digests: dict[str, str] | None = None,
    grant_types: tuple[str, ...] = EXPECTED_GRANT_TYPES,
) -> tuple[VerifiedGrantReference, ...]:
    supplied = digests or {}
    return tuple(
        reference(grant_type, grant_digest=supplied.get(grant_type))
        for grant_type in grant_types
    )


def test_the_canonical_grant_types_are_the_four_signed_types() -> None:
    assert REQUIRED_GRANT_TYPES == EXPECTED_GRANT_TYPES


def test_the_four_grant_vector_digests_to_the_fixed_value() -> None:
    assert grant_bundle_digest(vector()) == EXPECTED_BUNDLE_DIGEST


def test_a_changed_creator_account_binding_digests_to_its_own_fixed_value() -> None:
    rebound = vector(digests={"creator_account_binding": OTHER_GRANT_DIGEST})

    assert grant_bundle_digest(rebound) == EXPECTED_REBOUND_BUNDLE_DIGEST


@pytest.mark.parametrize("order", list(permutations(EXPECTED_GRANT_TYPES)))
def test_the_digest_is_independent_of_the_input_order(order: tuple[str, ...]) -> None:
    assert grant_bundle_digest(vector(grant_types=order)) == EXPECTED_BUNDLE_DIGEST


@pytest.mark.parametrize("changed", EXPECTED_GRANT_TYPES)
def test_every_required_grant_digest_enters_the_bundle(changed: str) -> None:
    """Compared with the same run's four-grant value, not with a literal.

    A bundle that dropped one pair would shift the literal too, so only the
    relation between the two vectors shows that the pair was digested.
    """

    replaced = vector(digests={changed: OTHER_GRANT_DIGEST})

    assert grant_bundle_digest(replaced) != grant_bundle_digest(vector())


def test_only_the_type_and_digest_of_a_grant_enter_the_bundle() -> None:
    relabelled = tuple(
        reference(
            grant_type,
            reference_id=f"vg1.other-{grant_type}",
            grant_identifier=f"jti-other-{grant_type}",
            creator_account_id="creator-account-2",
            installation_id="installation-2",
            verified_at=INSTANT + timedelta(days=3),
            expires_at=INSTANT + timedelta(days=30),
        )
        for grant_type in EXPECTED_GRANT_TYPES
    )

    assert grant_bundle_digest(relabelled) == EXPECTED_BUNDLE_DIGEST


@pytest.mark.parametrize("omitted", EXPECTED_GRANT_TYPES)
def test_a_missing_required_grant_is_refused(omitted: str) -> None:
    remaining = tuple(
        grant_type for grant_type in EXPECTED_GRANT_TYPES if grant_type != omitted
    )

    with pytest.raises(FinalizationRefused) as refusal:
        grant_bundle_digest(vector(grant_types=remaining))

    assert refusal.value.reason == "incomplete_grant_set"


def test_an_empty_tuple_is_refused() -> None:
    with pytest.raises(FinalizationRefused) as refusal:
        grant_bundle_digest(())

    assert refusal.value.reason == "incomplete_grant_set"


@pytest.mark.parametrize("repeated", EXPECTED_GRANT_TYPES)
def test_a_second_grant_of_one_required_type_is_refused(repeated: str) -> None:
    duplicated = (
        *vector(),
        reference(repeated, grant_digest=OTHER_GRANT_DIGEST),
    )

    with pytest.raises(FinalizationRefused) as refusal:
        grant_bundle_digest(duplicated)

    assert refusal.value.reason == "ambiguous_grant_set"


def test_a_grant_outside_the_canonical_types_is_refused() -> None:
    """The bundle covers the required set exactly, not a superset."""

    extended = (
        *vector(),
        reference("agent_pairing_grant", grant_digest=OTHER_GRANT_DIGEST),
    )

    with pytest.raises(FinalizationRefused) as refusal:
        grant_bundle_digest(extended)

    assert refusal.value.reason == "incoherent_grant_set"


def test_the_digest_is_accepted_as_a_verified_grant_bundle_binding() -> None:
    digest = grant_bundle_digest(vector())

    bindings = VerifiedGrantBindings(
        principal_id=SUBJECT,
        creator_account_id=ACCOUNT_ID,
        platform_creator_id=ACCOUNT_ID,
        bridge_role="creator",
        grant_bundle_sha256=digest,
    )

    assert bindings.grant_bundle_sha256 == EXPECTED_BUNDLE_DIGEST
    assert len(digest) == 64
    assert digest == digest.lower()


@pytest.mark.parametrize(
    "function", [finalize_provisioning, verified_grant_bindings]
)
def test_the_finalizer_digest_seam_defaults_to_the_canonical_digest(
    function: Any,
) -> None:
    parameter = inspect.signature(function).parameters["bundle_digest"]

    assert parameter.default is grant_bundle_digest
