"""Local finalization of one verified provisioning tuple."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Mapping

from app.core.first_run import (
    FirstRunResult,
    VerifiedGrantBindings,
    initialize_production_configuration,
)
from app.persistence.auth import (
    AuthenticationStateError,
    AuthenticationStore,
    AuthorizedAccountBinding,
    InstallationKeyReference,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    VerifiedGrantReference,
)
from app.security.grant_types import (
    CREATOR_ACCOUNT_BINDING,
    MEMBERSHIP_SNAPSHOT,
    PROVISIONING_GRANT_TYPES,
)
from app.security.runtime_policy import AuthContext


_CREATOR_ROLES = frozenset({"owner"})
_OPERATOR_ROLES = frozenset({"administrator", "creator_operator"})
_RESERVED_PREFIX = "replace-with-"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

FinalizationRefusal = Literal[
    "account_approval_missing",
    "installation_key_unavailable",
    "incomplete_grant_set",
    "ambiguous_grant_set",
    "incoherent_grant_set",
    "unbound_installation_key",
    "unapproved_creator_account",
    "detected_account_mismatch",
    "membership_role_unavailable",
    "grant_set_not_current",
]

# One digest per required grant type. Finalization hands the seam the selected
# tuple; `grant_bundle_digest` is the canonical implementation.
GrantBundleDigest = Callable[[tuple[VerifiedGrantReference, ...]], str]


class FinalizationRefused(ValueError):
    """Refusal carrying a fixed message and a nonsecret reason code."""

    def __init__(self, reason: FinalizationRefusal) -> None:
        super().__init__("Provisioning finalization is refused")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FinalizationRequest:
    """Untrusted provisioning-page body.

    `association_request_id` addresses durable provisioning state.
    `detected_creator_account_id` is compared with the signed binding.
    `reported_platform_creator_id` is client-reported and is never adopted:
    the local platform identity comes from the signed creator account.
    """

    association_request_id: str
    detected_creator_account_id: str
    reported_platform_creator_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAccountBinding:
    """One verified creator account and the grant bundle it was derived from.

    The account is carried out of finalization for a durable authorized-account
    record. It is never written into installation configuration.
    """

    creator_account_id: str
    platform_creator_id: str
    grant_bundle_sha256: str

    def __post_init__(self) -> None:
        accounts = (self.creator_account_id, self.platform_creator_id)
        if not all(accounts) or any(
            value.startswith(_RESERVED_PREFIX) for value in accounts
        ):
            raise ValueError("a verified account binding must be exact non-placeholder values")
        if _SHA256_PATTERN.fullmatch(self.grant_bundle_sha256) is None:
            raise ValueError("verified grant bundle digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class FinalizedProvisioning:
    """The installation identity written, the account verified, and their tuple."""

    bindings: VerifiedGrantBindings
    account: VerifiedAccountBinding
    grant_reference_ids: tuple[str, ...]
    configuration: FirstRunResult


def grant_bundle_digest(grants: tuple[VerifiedGrantReference, ...]) -> str:
    """Digest exactly one verified grant per required grant type.

    SHA-256 over canonical JSON holding the `(grant_type, grant_digest)` pairs
    sorted by grant type. The value records the provenance of initial
    provisioning. It is independent of the order the grants arrive in and of
    every grant field outside the pair, so an unchanged grant set always
    produces an unchanged digest.
    """

    document = json.dumps(
        [[grant_type, digest] for grant_type, digest in _digested_pairs(grants)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def finalize_provisioning(
    *,
    store: AuthenticationStore,
    request: FinalizationRequest,
    extension_id: str,
    bundle_digest: GrantBundleDigest = grant_bundle_digest,
    data_directory: str | Path | None = None,
) -> FinalizedProvisioning:
    """Derive verified bindings and create production configuration once.

    `FirstRunConflictError` from the initializer propagates unchanged: a
    different binding or extension ID never overwrites existing configuration.
    """

    bindings, account, references = verified_grant_bindings(
        store=store, request=request, bundle_digest=bundle_digest
    )
    configuration = initialize_production_configuration(
        bindings=bindings,
        extension_id=extension_id,
        data_directory=data_directory,
    )
    return FinalizedProvisioning(bindings, account, references, configuration)


def authorize_finalized_account(
    *,
    store: AuthenticationStore,
    request: FinalizationRequest,
    finalized: FinalizedProvisioning,
    authorized_at: datetime,
) -> AuthorizedAccountBinding:
    """Record what finalization verified as a durable account authorization.

    Finalization verifies one account; this states that the installation
    authorized it, and retains the grant tuple and its digest as the provenance
    of that decision. The store applies the pilot cardinality limit.
    """

    candidate = _approved_candidate(store, request.association_request_id)
    if candidate.creator_account_id != finalized.account.creator_account_id:
        raise FinalizationRefused("unapproved_creator_account")
    binding = AuthorizedAccountBinding(
        creator_account_id=finalized.account.creator_account_id,
        installation_id=candidate.installation_id,
        platform_creator_id=finalized.account.platform_creator_id,
        association_request_id=candidate.association_request_id,
        grant_bundle_sha256=finalized.account.grant_bundle_sha256,
        authorized_at=authorized_at,
        grant_reference_ids=finalized.grant_reference_ids,
    )
    store.record_authorized_account_binding(binding)
    return binding


def verified_grant_bindings(
    *,
    store: AuthenticationStore,
    request: FinalizationRequest,
    bundle_digest: GrantBundleDigest = grant_bundle_digest,
) -> tuple[VerifiedGrantBindings, VerifiedAccountBinding, tuple[str, ...]]:
    """Build installation identity and the account from one verified tuple.

    Every identity value is read from a signed, locally verified grant. The
    request supplies lookup and comparison values only.
    """

    candidate = _approved_candidate(store, request.association_request_id)
    selected = _one_grant_per_required_type(store.verified_grants())
    key = store.installation_key_reference()
    if key is None:
        raise FinalizationRefused("installation_key_unavailable")

    _require_coherent_tuple(selected, candidate=candidate, key=key)
    binding = selected[CREATOR_ACCOUNT_BINDING]
    membership = selected[MEMBERSHIP_SNAPSHOT]

    creator_account_id = binding.creator_account_id
    if creator_account_id is None or creator_account_id != candidate.creator_account_id:
        raise FinalizationRefused("unapproved_creator_account")
    if creator_account_id != request.detected_creator_account_id:
        raise FinalizationRefused("detected_account_mismatch")

    bridge_role = _bridge_role(membership.membership_roles)
    references = tuple(
        selected[grant_type].reference_id for grant_type in PROVISIONING_GRANT_TYPES
    )
    identity = AuthContext(
        principal_id=membership.subject,
        creator_account_id=creator_account_id,
        role=bridge_role,
    )
    try:
        store.build_runtime_policy_from_grants(identity, references)
    except AuthenticationStateError:
        raise FinalizationRefused("grant_set_not_current") from None

    bindings = VerifiedGrantBindings(
        principal_id=membership.subject,
        bridge_role=bridge_role,
    )
    account = VerifiedAccountBinding(
        creator_account_id=creator_account_id,
        platform_creator_id=creator_account_id,
        grant_bundle_sha256=bundle_digest(
            tuple(selected[grant_type] for grant_type in PROVISIONING_GRANT_TYPES)
        ),
    )
    return bindings, account, references


def _approved_candidate(
    store: AuthenticationStore, association_request_id: str
) -> ProvisioningCandidate:
    candidate = store.provisioning_candidate(association_request_id)
    if candidate is None or candidate.state is not ProvisioningCandidateState.APPROVED:
        raise FinalizationRefused("account_approval_missing")
    return candidate


def _one_grant_per_required_type(
    grants: tuple[VerifiedGrantReference, ...],
) -> dict[str, VerifiedGrantReference]:
    selected: dict[str, VerifiedGrantReference] = {}
    for grant_type in PROVISIONING_GRANT_TYPES:
        matches = [grant for grant in grants if grant.grant_type == grant_type]
        if not matches:
            raise FinalizationRefused("incomplete_grant_set")
        if len(matches) > 1:
            raise FinalizationRefused("ambiguous_grant_set")
        selected[grant_type] = matches[0]
    return selected


def _digested_pairs(
    grants: tuple[VerifiedGrantReference, ...],
) -> tuple[tuple[str, str], ...]:
    """Select one `(grant_type, grant_digest)` pair per required grant type.

    A grant type outside the required list, a repeated type, or a missing type
    is refused rather than digested: the bundle covers the required set exactly.
    """

    digests: dict[str, str] = {}
    for grant in grants:
        if grant.grant_type not in PROVISIONING_GRANT_TYPES:
            raise FinalizationRefused("incoherent_grant_set")
        if grant.grant_type in digests:
            raise FinalizationRefused("ambiguous_grant_set")
        digests[grant.grant_type] = grant.grant_digest
    if any(grant_type not in digests for grant_type in PROVISIONING_GRANT_TYPES):
        raise FinalizationRefused("incomplete_grant_set")
    return tuple(sorted(digests.items()))


def _require_coherent_tuple(
    selected: Mapping[str, VerifiedGrantReference],
    *,
    candidate: ProvisioningCandidate,
    key: InstallationKeyReference,
) -> None:
    for grant in selected.values():
        if (
            grant.organization_id != candidate.organization_id
            or grant.installation_id != candidate.installation_id
        ):
            raise FinalizationRefused("incoherent_grant_set")
        if (
            grant.installation_key_id != key.installation_key_id
            or grant.installation_key_jkt != key.installation_key_jkt
        ):
            raise FinalizationRefused("unbound_installation_key")
    principals = {(grant.issuer, grant.subject) for grant in selected.values()}
    if len(principals) != 1:
        raise FinalizationRefused("incoherent_grant_set")


def _bridge_role(
    membership_roles: tuple[str, ...] | None,
) -> Literal["creator", "operator"]:
    """Map the verified membership role set to one local Bridge role.

    The mapping is the one the WebAuthn authority already applies
    (`app/security/webauthn.py:678-694`).
    """

    roles = set(membership_roles or ())
    if roles & _CREATOR_ROLES:
        return "creator"
    if roles & _OPERATOR_ROLES:
        return "operator"
    raise FinalizationRefused("membership_role_unavailable")
