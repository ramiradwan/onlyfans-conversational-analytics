"""Serve the provisioning surface against a locally minted grant authority.

Builds the shipped provisioning application with an offline hosted plane in
place of its outbound transport, so one pasted claim package travels the real
decoder, ``HostedGrantClient``, ``verify_grant``, and the durable store. The
trust set, the grant tuple it verifies, and the transport that answers hosted
requests all come from ``tests/test_hosted_grants.py``; nothing here mints a
second kind of claim or trust set.

The trust set injected here is generated per run, so agreement between the
grants and the verifier is agreement of this process with itself. It says
nothing about the production trust set, the production grant signer, or any
control plane.

Depends on ``app`` and ships with nothing;
``tests/test_architecture_e2e_helper_boundary.py`` keeps that direction.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PRODUCT_ROOT))

from app.persistence.auth import (
    AuthenticationStore,
    InstallationKeyReference,
    InstallationKeyReservation,
)
from app.provisioning.app import create_provisioning_app
from app.provisioning.binding_acquisition import durable_creator_account_binding_acquisition
from app.provisioning.claim_package import CLAIM_PACKAGE_PROFILE_V2
from app.provisioning.claim_submission import durable_claim_submission
from app.provisioning.completion import (
    durable_authentication_store,
    durable_completion_reader,
    durable_finalize_action,
)
from app.provisioning.creator_association import durable_creator_association_initiation
from app.provisioning.progress import durable_provisioning_progress
from app.security.hosted_grants import (
    CLAIM_PROFILE_V2,
    HostedGrantClient,
    InstallationClaim,
    TransportResponse,
)
from tests.test_hosted_grants import (
    FakeProofAuthority,
    SignedBundle,
    StoredClaimTransport,
    signed_bundle,
    signed_claim,
    signed_creator_account_ids,
)


# No request leaves this process: every hosted API path is answered in memory.
# The customer continuation URL below is intercepted by Playwright and is
# deliberately an invalid test-only host, never a release/customer default.
HOSTED_ORIGIN = "https://control.invalid"
HOSTED_ONBOARDING_URL = "https://secure-setup.e2e.invalid/start"

BIND_HOST = "127.0.0.1"
BIND_PORT = 17871

# Seconds subtracted from the wall clock when the grants are minted, so `nbf`
# is already in the past when the first verification runs.
MINTING_BACKDATE_SECONDS = 60

V2_BOOTSTRAP_GRANT_TYPES = ("installation_grant", "membership_snapshot")


def v2_claim() -> InstallationClaim:
    """Promote the shared claim coordinates onto the production v2 profile."""

    legacy = signed_claim()
    return InstallationClaim(
        claim_id=legacy.claim_id,
        claim_secret=legacy.claim_secret,
        challenge=legacy.challenge,
        onboarding_transaction_id=legacy.onboarding_transaction_id,
        organization_id=legacy.organization_id,
        installation_id=legacy.installation_id,
        consume_path=legacy.consume_path,
        claim_profile=CLAIM_PROFILE_V2,
    )


def claim_package(claim: InstallationClaim) -> str:
    """Encode one production-v2 claim as the canonical pasted package."""

    document = {
        "profile": CLAIM_PACKAGE_PROFILE_V2,
        "claim_profile": CLAIM_PROFILE_V2,
        "claim_id": claim.claim_id,
        "claim_secret": claim.claim_secret,
        "challenge": claim.challenge,
        "onboarding_transaction_id": claim.onboarding_transaction_id,
        "organization_id": claim.organization_id,
        "installation_id": claim.installation_id,
        "consume_path": claim.consume_path,
    }
    raw = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class V2StoredClaimTransport(StoredClaimTransport):
    """Adapt the shared hosted fixture to the production v2 bootstrap contract."""

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object],
    ) -> TransportResponse:
        response = super().request(method, path, json_body=json_body)
        if path != self.claim.consume_path or response.status_code != 200:
            return response
        document = json.loads(response.body)
        document["profile"] = CLAIM_PROFILE_V2
        document["grants"] = {
            grant_type: self.bundle.tokens[grant_type]
            for grant_type in V2_BOOTSTRAP_GRANT_TYPES
        }
        return TransportResponse(
            response.status_code,
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            "application/json",
        )


def activate_minted_installation_key(
    store: AuthenticationStore, reference: InstallationKeyReference
) -> InstallationKeyReference:
    """Drive the minted key through the store's reserve and activate steps."""

    reserved = store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name=reference.provider_name,
            provider_key_name=reference.provider_key_name,
            algorithm=reference.algorithm,
            created_at=reference.created_at,
        )
    )
    if (
        reserved.provider_name != reference.provider_name
        or reserved.provider_key_name != reference.provider_key_name
        or reserved.algorithm != reference.algorithm
    ):
        raise RuntimeError(
            "Installation key reservation is held by "
            f"{reserved.provider_name}, not the minted authority"
        )
    store.activate_installation_key(reference)
    active = store.installation_key_reference()
    if active is None:
        raise RuntimeError("Installation key did not activate")
    return active


def build_application(
    *,
    data_directory: Path,
    extension_id: str,
    handoff_token: str,
    bundle: SignedBundle,
    claim: InstallationClaim,
):
    """Build the provisioning application over the offline hosted plane."""

    open_store = durable_authentication_store(data_directory)
    activate_minted_installation_key(open_store(), bundle.installation_key)
    transport = V2StoredClaimTransport(bundle, claim)
    proof_authority = FakeProofAuthority(bundle.installation_key)

    def hosted_client(store: AuthenticationStore) -> HostedGrantClient:
        return HostedGrantClient(
            transport, proof_authority, store, trust_set=bundle.trust_set
        )

    return create_provisioning_app(
        claim_submission=durable_claim_submission(
            open_store,
            hosted_origin=HOSTED_ORIGIN,
            transport_factory=lambda origin: transport,
            proof_authority_factory=lambda store: proof_authority,
            trust_set=bundle.trust_set,
        ),
        creator_association_initiation=durable_creator_association_initiation(
            open_store,
            hosted_origin=HOSTED_ORIGIN,
            client_factory=hosted_client,
        ),
        creator_binding_acquisition=durable_creator_account_binding_acquisition(
            open_store,
            hosted_origin=HOSTED_ORIGIN,
            client_factory=hosted_client,
        ),
        completion_ready=durable_completion_reader(
            open_store, data_directory=data_directory
        ),
        provisioning_progress=durable_provisioning_progress(open_store),
        finalize_action=durable_finalize_action(
            open_store,
            extension_id=extension_id,
            data_directory=data_directory,
        ),
        extension_id=extension_id,
        hosted_onboarding_url=HOSTED_ONBOARDING_URL,
        launcher_handoff_token=handoff_token,
    )


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-directory", required=True, type=Path)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("--handoff-token", required=True)
    arguments = parser.parse_args()

    minted_at = int(datetime.now(timezone.utc).timestamp()) - MINTING_BACKDATE_SECONDS
    bundle = signed_bundle(iat=minted_at)
    claim = v2_claim()
    application = build_application(
        data_directory=arguments.data_directory,
        extension_id=arguments.extension_id,
        handoff_token=arguments.handoff_token,
        bundle=bundle,
        claim=claim,
    )
    creator_account_id, _ = signed_creator_account_ids()
    print(
        json.dumps(
            {
                "claim_package": claim_package(claim),
                "creator_account_id": creator_account_id,
                "hosted_onboarding_url": HOSTED_ONBOARDING_URL,
                "installation_id": claim.installation_id,
                "installation_key_id": bundle.installation_key.installation_key_id,
                "organization_id": claim.organization_id,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    uvicorn.Server(
        uvicorn.Config(
            application,
            host=BIND_HOST,
            port=BIND_PORT,
            workers=1,
            access_log=False,
            log_level="warning",
        )
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
