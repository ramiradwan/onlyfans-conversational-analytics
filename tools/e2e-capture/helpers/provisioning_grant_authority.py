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
from app.provisioning.binding_acquisition import (
    durable_creator_account_binding_acquisition,
)
from app.provisioning.claim_package import CLAIM_PACKAGE_PROFILE
from app.provisioning.claim_submission import durable_claim_submission
from app.provisioning.completion import (
    durable_authentication_store,
    durable_completion_reader,
    durable_finalize_action,
)
from app.provisioning.creator_association import (
    durable_creator_association_initiation,
)
from app.security.hosted_grants import HostedGrantClient, InstallationClaim
from tests.test_hosted_grants import (
    FakeProofAuthority,
    SignedBundle,
    StoredClaimTransport,
    signed_bundle,
    signed_claim,
    signed_creator_account_ids,
)


# No request leaves this process: every hosted path is answered in memory. The
# origin is still required to be a bare origin by the actions that carry it.
HOSTED_ORIGIN = "https://control.invalid"

BIND_HOST = "127.0.0.1"
BIND_PORT = 17871

# Seconds subtracted from the wall clock when the grants are minted, so `nbf`
# is already in the past when the first verification runs.
MINTING_BACKDATE_SECONDS = 60


def claim_package(claim: InstallationClaim) -> str:
    """Encode one claim as the canonical base64url package the page accepts."""

    document = {
        "profile": CLAIM_PACKAGE_PROFILE,
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


def activate_minted_installation_key(
    store: AuthenticationStore, reference: InstallationKeyReference
) -> InstallationKeyReference:
    """Drive the minted key through the store's reserve and activate steps.

    The provisioning stage never opens a platform key provider, so the key the
    grants are bound to is installed through the same durable transitions a
    provider-backed key uses rather than by writing the reference row.
    """

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
    transport = StoredClaimTransport(bundle, claim)
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
        finalize_action=durable_finalize_action(
            open_store,
            extension_id=extension_id,
            data_directory=data_directory,
        ),
        extension_id=extension_id,
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
    claim = signed_claim()
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
