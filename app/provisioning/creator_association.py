"""Initiation of one hosted creator-account approval request.

The isolated provisioning route injects this action because it needs durable
claim state, the installation-key proof authority, and the hosted client.  The
request coordinates come exclusively from the locally recorded consumed claim;
the page can supply only the locally detected creator account.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Protocol

from app.persistence.auth import (
    AuthenticationStore,
    ClaimSubmission,
    ProvisioningCandidate,
    ProvisioningCandidateState,
)
from app.provisioning.claim_submission import (
    hosted_transport,
    installation_proof_authority,
)
from app.security.hosted_grants import (
    CreatorAssociationPending,
    CreatorAssociationRefused,
    CreatorAssociationRequest,
    CreatorAssociationStatus,
    HostedGrantClient,
    HostedGrantUnavailable,
    HostedTransport,
    InstallationProofAuthority,
)
from app.security.installation_key import InstallationKeyError


CreatorAssociationRefusal = Literal[
    "association_pending",
    "association_refused",
    "claim_coordinates_ambiguous",
    "claim_coordinates_from_caller",
    "claim_coordinates_unavailable",
    "hosted_origin_unavailable",
    "hosted_unavailable",
    "installation_key_unavailable",
]


@dataclass(frozen=True, slots=True)
class AssociationInitiationStatus:
    """The safe local view of a newly pending hosted association."""

    association_request_id: str
    updated_at: str
    status: Literal["pending"] = "pending"


class CreatorAssociationClient(Protocol):
    """Narrow client seam for starting one hosted association."""

    def request_creator_association(
        self, association: CreatorAssociationRequest
    ) -> CreatorAssociationStatus: ...


def new_association_request_id(
    *,
    now: float | None = None,
    entropy: bytes | None = None,
) -> str:
    """Render a lowercase RFC 9562 UUIDv7 without an additional dependency."""

    milliseconds = int((time.time() if now is None else now) * 1000)
    raw = bytearray(milliseconds.to_bytes(6, "big") + (entropy or os.urandom(10)))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    digits = raw.hex()
    return f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"


def initiate_creator_association(
    *,
    store: AuthenticationStore,
    client: CreatorAssociationClient,
    detected_creator_account_id: str,
    request_id_factory: Callable[[], str] = new_association_request_id,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> AssociationInitiationStatus | CreatorAssociationRefusal:
    """Start approval from exactly one locally enrolled claim tuple.

    A provisioning-stage authentication database represents one installation.
    More than one consumed claim therefore cannot select a safe outbound tuple
    and is refused before any hosted request.
    """

    association_request_id = request_id_factory()
    claims = store.consumed_claim_submissions()
    if not claims:
        return "claim_coordinates_unavailable"
    if len(claims) != 1:
        return "claim_coordinates_ambiguous"
    claim = claims[0]
    association = _association_request(
        association_request_id, claim, detected_creator_account_id
    )
    try:
        hosted_status = client.request_creator_association(association)
    except CreatorAssociationPending:
        return "association_pending"
    except CreatorAssociationRefused:
        return "association_refused"
    except HostedGrantUnavailable:
        return "hosted_unavailable"
    except InstallationKeyError:
        return "installation_key_unavailable"
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association.association_request_id,
            installation_id=association.installation_id,
            onboarding_transaction_id=association.onboarding_transaction_id,
            organization_id=association.organization_id,
            creator_account_id=association.creator_account_id,
            state=ProvisioningCandidateState.PENDING,
            requested_at=now(),
        )
    )
    return AssociationInitiationStatus(
        association_request_id=association.association_request_id,
        updated_at=hosted_status.updated_at,
    )


def durable_creator_association_initiation(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: Callable[[str], HostedTransport] = hosted_transport,
    proof_authority_factory: Callable[
        [AuthenticationStore], InstallationProofAuthority
    ] = installation_proof_authority,
    client_factory: Callable[[AuthenticationStore], CreatorAssociationClient] | None = None,
    request_id_factory: Callable[[], str] = new_association_request_id,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[..., AssociationInitiationStatus | CreatorAssociationRefusal]:
    """Build the isolated route action over the existing hosted grant client."""

    client: CreatorAssociationClient | None = None

    def hosted_client(store: AuthenticationStore) -> CreatorAssociationClient:
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

    def initiate(
        *,
        detected_creator_account_id: str,
        onboarding_transaction_id: str | None = None,
        organization_id: str | None = None,
        installation_id: str | None = None,
    ) -> AssociationInitiationStatus | CreatorAssociationRefusal:
        # Coordinates are not route inputs.  Refuse their presence before the
        # hosted client is constructed, let alone asked to sign or send them.
        if any(
            value is not None
            for value in (
                onboarding_transaction_id,
                organization_id,
                installation_id,
            )
        ):
            return "claim_coordinates_from_caller"
        store = open_store()
        try:
            client_for_request = hosted_client(store)
        except ValueError:
            return "hosted_origin_unavailable"
        except InstallationKeyError:
            return "installation_key_unavailable"
        return initiate_creator_association(
            store=store,
            client=client_for_request,
            detected_creator_account_id=detected_creator_account_id,
            request_id_factory=request_id_factory,
            now=now,
        )

    return initiate


def _association_request(
    association_request_id: str,
    claim: ClaimSubmission,
    detected_creator_account_id: str,
) -> CreatorAssociationRequest:
    return CreatorAssociationRequest(
        association_request_id=association_request_id,
        onboarding_transaction_id=claim.onboarding_transaction_id,
        organization_id=claim.organization_id,
        installation_id=claim.installation_id,
        creator_account_id=detected_creator_account_id,
    )
