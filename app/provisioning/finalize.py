"""Local finalization of one verified provisioning tuple."""

from __future__ import annotations

from dataclasses import dataclass
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
    InstallationKeyReference,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    VerifiedGrantReference,
)
from app.security.runtime_policy import AuthContext


REQUIRED_GRANT_TYPES = (
    "creator_account_binding",
    "installation_grant",
    "license_entitlement",
    "membership_snapshot",
)

_CREATOR_ROLES = frozenset({"owner"})
_OPERATOR_ROLES = frozenset({"administrator", "creator_operator"})

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

# One digest per required grant type. The canonical implementation is the
# separate digest subunit; finalization only hands it the selected tuple.
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
class FinalizedProvisioning:
    """The bindings that were written and the tuple they were derived from."""

    bindings: VerifiedGrantBindings
    grant_reference_ids: tuple[str, ...]
    configuration: FirstRunResult


def finalize_provisioning(
    *,
    store: AuthenticationStore,
    request: FinalizationRequest,
    extension_id: str,
    bundle_digest: GrantBundleDigest,
    data_directory: str | Path | None = None,
) -> FinalizedProvisioning:
    """Derive verified bindings and create production configuration once.

    `FirstRunConflictError` from the initializer propagates unchanged: a
    different binding or extension ID never overwrites existing configuration.
    """

    bindings, references = verified_grant_bindings(
        store=store, request=request, bundle_digest=bundle_digest
    )
    configuration = initialize_production_configuration(
        bindings=bindings,
        extension_id=extension_id,
        data_directory=data_directory,
    )
    return FinalizedProvisioning(bindings, references, configuration)


def verified_grant_bindings(
    *,
    store: AuthenticationStore,
    request: FinalizationRequest,
    bundle_digest: GrantBundleDigest,
) -> tuple[VerifiedGrantBindings, tuple[str, ...]]:
    """Build the bindings from exactly one verified four-grant tuple.

    Every identity value is read from a signed, locally verified grant. The
    request supplies lookup and comparison values only.
    """

    candidate = _approved_candidate(store, request.association_request_id)
    selected = _one_grant_per_required_type(store.verified_grants())
    key = store.installation_key_reference()
    if key is None:
        raise FinalizationRefused("installation_key_unavailable")

    _require_coherent_tuple(selected, candidate=candidate, key=key)
    binding = selected["creator_account_binding"]
    membership = selected["membership_snapshot"]

    creator_account_id = binding.creator_account_id
    if creator_account_id is None or creator_account_id != candidate.creator_account_id:
        raise FinalizationRefused("unapproved_creator_account")
    if creator_account_id != request.detected_creator_account_id:
        raise FinalizationRefused("detected_account_mismatch")

    bridge_role = _bridge_role(membership.membership_roles)
    references = tuple(
        selected[grant_type].reference_id for grant_type in REQUIRED_GRANT_TYPES
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
        creator_account_id=creator_account_id,
        platform_creator_id=creator_account_id,
        bridge_role=bridge_role,
        grant_bundle_sha256=bundle_digest(
            tuple(selected[grant_type] for grant_type in REQUIRED_GRANT_TYPES)
        ),
    )
    return bindings, references


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
    for grant_type in REQUIRED_GRANT_TYPES:
        matches = [grant for grant in grants if grant.grant_type == grant_type]
        if not matches:
            raise FinalizationRefused("incomplete_grant_set")
        if len(matches) > 1:
            raise FinalizationRefused("ambiguous_grant_set")
        selected[grant_type] = matches[0]
    return selected


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
