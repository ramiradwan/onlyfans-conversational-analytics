"""Acquisition of a hosted-approved creator-account binding.

The page provides no association coordinates.  This action reconstructs the
hosted request entirely from the pending local candidate and names the
membership reference from verified local grant state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Protocol

from app.persistence.auth import (
    AuthenticationStateError,
    AuthenticationStore,
    ProvisioningCandidate,
    VerifiedGrantReference,
)
from app.provisioning.claim_submission import (
    hosted_transport,
    installation_proof_authority,
)
from app.security.hosted_grants import (
    CreatorAssociationRefused,
    CreatorAssociationRequest,
    GrantVerificationRefused,
    HostedGrantClient,
    HostedGrantUnavailable,
    HostedTransport,
    InstallationProofAuthority,
)
from app.security.installation_key import InstallationKeyError


BindingAcquisitionRefusal = Literal[
    "binding_acquisition_unavailable",
    "candidate_resolution_conflict",
    "grant_verification_refused",
    "hosted_origin_unavailable",
    "hosted_unavailable",
    "installation_key_unavailable",
    "membership_reference_unavailable",
]


@dataclass(frozen=True, slots=True)
class BindingAcquisitionStatus:
    """The nonsecret local result of one successful acquisition."""

    association_request_id: str
    status: Literal["approved"] = "approved"


class CreatorBindingClient(Protocol):
    """Narrow client seam for acquiring one approved association."""

    def acquire_creator_account_binding(
        self,
        association: CreatorAssociationRequest,
        *,
        membership_reference_id: str,
    ) -> VerifiedGrantReference: ...


def acquire_creator_account_binding(
    *,
    store: AuthenticationStore,
    client: CreatorBindingClient,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> BindingAcquisitionStatus | BindingAcquisitionRefusal:
    """Acquire and durably resolve the single pending local candidate.

    The membership reference identifies the enrolled installation.  The
    candidate read for that installation is the independent source of the
    expected creator account passed to the hosted client.
    """

    membership = _membership_reference(store)
    if membership is None:
        return "membership_reference_unavailable"
    candidate = store.pending_provisioning_candidate(membership.installation_id)
    if candidate is None:
        return "binding_acquisition_unavailable"
    association = _association_request(candidate)
    try:
        client.acquire_creator_account_binding(
            association,
            membership_reference_id=membership.reference_id,
        )
    except GrantVerificationRefused:
        return "grant_verification_refused"
    except CreatorAssociationRefused:
        return "binding_acquisition_unavailable"
    except HostedGrantUnavailable:
        return "hosted_unavailable"
    except AuthenticationStateError:
        return "membership_reference_unavailable"
    except InstallationKeyError:
        return "installation_key_unavailable"
    if not store.approve_provisioning_candidate(
        candidate.association_request_id, resolved_at=now()
    ):
        return "candidate_resolution_conflict"
    return BindingAcquisitionStatus(candidate.association_request_id)


def durable_creator_account_binding_acquisition(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: Callable[[str], HostedTransport] = hosted_transport,
    proof_authority_factory: Callable[
        [AuthenticationStore], InstallationProofAuthority
    ] = installation_proof_authority,
    client_factory: Callable[[AuthenticationStore], CreatorBindingClient] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[..., BindingAcquisitionStatus | BindingAcquisitionRefusal]:
    """Build the isolated route action over the hosted grant client."""

    client: CreatorBindingClient | None = None

    def hosted_client(store: AuthenticationStore) -> CreatorBindingClient:
        nonlocal client
        if client is None:
            client = (
                client_factory(store)
                if client_factory is not None
                else HostedGrantClient(
                    transport_factory(hosted_origin),
                    proof_authority_factory(store),
                    store,
                )
            )
        return client

    def acquire() -> BindingAcquisitionStatus | BindingAcquisitionRefusal:
        store = open_store()
        try:
            client_for_acquisition = hosted_client(store)
        except ValueError:
            return "hosted_origin_unavailable"
        except InstallationKeyError:
            return "installation_key_unavailable"
        return acquire_creator_account_binding(
            store=store,
            client=client_for_acquisition,
            now=now,
        )

    return acquire


def _membership_reference(store: AuthenticationStore) -> VerifiedGrantReference | None:
    """Return the only current local membership reference, if it is unique."""

    memberships = tuple(
        grant for grant in store.verified_grants() if grant.grant_type == "membership_snapshot"
    )
    return memberships[0] if len(memberships) == 1 else None


def _association_request(candidate: ProvisioningCandidate) -> CreatorAssociationRequest:
    return CreatorAssociationRequest(
        association_request_id=candidate.association_request_id,
        onboarding_transaction_id=candidate.onboarding_transaction_id,
        organization_id=candidate.organization_id,
        installation_id=candidate.installation_id,
        creator_account_id=candidate.creator_account_id,
    )
