"""Consumption of one pasted installation claim.

The provisioning surface is isolated from the modules claim consumption needs,
so the submission action is built here and injected into it. The decoded claim
lives only inside the request that decodes it: it is released once to the hosted
client and dropped in every exit path, so a refusal cannot leave a reusable
secret behind and no later request can reach one.

Device metadata is built from local values only. The pasted package supplies
none of it, and nothing in it is read back from the hosted response.
"""

from __future__ import annotations

import platform
from typing import Callable, Literal, Mapping

from app.persistence.auth import AuthenticationStore
from app.provisioning.claim_package import (
    ClaimPackageError,
    DecodedClaimPackage,
    decode_claim_package,
    local_device_metadata,
)
from app.security.hosted_grants import (
    GrantVerificationRefused,
    HostedGrantClient,
    HostedGrantUnavailable,
    HostedTransport,
    HTTPXHostedTransport,
    InstallationClaimRefused,
    InstallationClaimReplay,
    InstallationProofAuthority,
)
from app.security.installation_key import (
    InstallationKeyAuthority,
    InstallationKeyError,
    WindowsCNGInstallationKeyProvider,
)


# The version reported to the hosted plane as device metadata. It is stated
# here because provisioning runs before runtime configuration exists and so
# cannot read application settings; `tests/test_provisioning_claim_submission.py`
# pins it to the application version.
PRODUCT_VERSION = "0.7.5"

ClaimSubmissionRefusal = Literal[
    "claim_already_consumed",
    "claim_refused",
    "grant_verification_refused",
    "hosted_origin_unavailable",
    "hosted_unavailable",
    "installation_key_unavailable",
]


def hosted_transport(hosted_origin: str) -> HostedTransport:
    """Open the outbound HTTPS transport for the pinned hosted origin."""
    return HTTPXHostedTransport(hosted_origin)


def installation_proof_authority(
    store: AuthenticationStore,
) -> InstallationProofAuthority:
    """Bind possession proofs to the TPM-backed installation key."""
    return InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider())


def durable_claim_submission(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: Callable[[str], HostedTransport] = hosted_transport,
    proof_authority_factory: Callable[
        [AuthenticationStore], InstallationProofAuthority
    ] = installation_proof_authority,
    device_display_name: Callable[[], str] = platform.node,
    trust_set: Mapping[str, object] | None = None,
) -> Callable[..., str | None]:
    """Build the action that consumes one pasted claim into verified grants.

    Returns `None` once the hosted plane has consumed the claim and every grant
    it returned has been verified and stored, and a nonsecret refusal reason
    otherwise. The decoder's own reason codes pass through unchanged; the codes
    added here name outcomes the decoder cannot see.

    An unusable hosted origin, an unusable installation key, and an
    unauthoritative hosted result are refusals. A packaged trust set that fails
    its integrity check is not: it is a defect in the installed artifact rather
    than a state the customer can act on, and it must not be reported as one.
    """

    client: HostedGrantClient | None = None

    def hosted_client() -> HostedGrantClient:
        nonlocal client
        if client is None:
            store = open_store()
            transport = transport_factory(hosted_origin)
            client = HostedGrantClient(
                transport,
                proof_authority_factory(store),
                store,
                trust_set=trust_set,
            )
        return client

    def consume(decoded: DecodedClaimPackage) -> str | None:
        try:
            device = local_device_metadata(
                product_version=PRODUCT_VERSION,
                display_name=device_display_name(),
            )
        except ClaimPackageError as refusal:
            return refusal.reason
        try:
            client = hosted_client()
        except ValueError:
            # The hosted origin is the only outside value parsed here.
            return "hosted_origin_unavailable"
        except InstallationKeyError:
            return "installation_key_unavailable"
        try:
            client.consume_claim(decoded.release_claim(), device)
        except InstallationKeyError:
            return "installation_key_unavailable"
        except InstallationClaimReplay:
            return "claim_already_consumed"
        except InstallationClaimRefused:
            return "claim_refused"
        except GrantVerificationRefused:
            return "grant_verification_refused"
        except HostedGrantUnavailable:
            return "hosted_unavailable"
        return None

    def submit_claim(*, package: str) -> str | None:
        try:
            decoded = decode_claim_package(package)
        except ClaimPackageError as refusal:
            return refusal.reason
        try:
            return consume(decoded)
        finally:
            decoded.clear()

    return submit_claim
