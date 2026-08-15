"""Seed synthetic post-verification authority for one disposable E2E store.

Writes what local provisioning would have written: the verified grant tuple, the
approved candidate it rests on, and the durable record that the installation
authorized the account. Without the authorization record the installation
authorizes no account, which is a valid state that holds no configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PRODUCT_ROOT))

from app.persistence.auth import (
    AuthorizedAccountBinding,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)


ACCOUNT_ID = "dev-creator-account"
INSTALLATION_ID = "e2e-temporary-installation"
ORGANIZATION_ID = "e2e-organization"
ASSOCIATION_REQUEST_ID = "e2e-association-request"
PLATFORM_CREATOR_ID = "e2e-platform-creator"


def _grant(
    grant_type: str,
    *,
    installation_key_id: str,
    installation_key_jkt: str,
    reference_suffix: str = "",
) -> VerifiedGrantReference:
    now = datetime.now(timezone.utc)
    account_id = ACCOUNT_ID
    reference_id = f"e2e-{grant_type}{reference_suffix}"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode("utf-8")).hexdigest(),
        issuer="https://e2e.invalid/verified-grant-store",
        subject="e2e-local-principal",
        installation_id=INSTALLATION_ID,
        creator_account_id=(
            account_id if grant_type == "creator_account_binding" else None
        ),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        verified_at=now,
        organization_id=ORGANIZATION_ID,
        installation_key_id=installation_key_id,
        installation_key_jkt=installation_key_jkt,
        membership_id=("e2e-membership" if grant_type == "membership_snapshot" else None),
        allowed_creator_account_ids=(
            (account_id,) if grant_type == "membership_snapshot" else None
        ),
        membership_roles=(
            ("owner",) if grant_type == "membership_snapshot" else None
        ),
    )


def _authorize_account(
    store: SQLiteAuthenticationStore, grant_reference_ids: tuple[str, ...]
) -> bool:
    """Record the approved candidate and the account authorization resting on it.

    Returns False when the installation already authorizes an account: the
    store records at most one, and re-recording is refused rather than
    overwriting the provenance of the first.
    """

    if store.authorized_account_bindings():
        return False
    now = datetime.now(timezone.utc)
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=ASSOCIATION_REQUEST_ID,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id="e2e-onboarding-transaction",
            organization_id=ORGANIZATION_ID,
            creator_account_id=ACCOUNT_ID,
            state=ProvisioningCandidateState.PENDING,
            requested_at=now,
        )
    )
    store.approve_provisioning_candidate(ASSOCIATION_REQUEST_ID, resolved_at=now)
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=ACCOUNT_ID,
            installation_id=INSTALLATION_ID,
            platform_creator_id=PLATFORM_CREATOR_ID,
            association_request_id=ASSOCIATION_REQUEST_ID,
            grant_bundle_sha256=hashlib.sha256(
                "\n".join(sorted(grant_reference_ids)).encode("utf-8")
            ).hexdigest(),
            authorized_at=now,
            grant_reference_ids=grant_reference_ids,
        )
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-database", required=True, type=Path)
    parser.add_argument("--extra-current-binding", action="store_true")
    arguments = parser.parse_args()

    store = SQLiteAuthenticationStore(arguments.auth_database)
    key = store.installation_key_reference()
    if key is None:
        raise RuntimeError("Temporary installation key is not active")
    grants = [
        _grant(
            grant_type,
            installation_key_id=key.installation_key_id,
            installation_key_jkt=key.installation_key_jkt,
        )
        for grant_type in (
            "installation_grant",
            "membership_snapshot",
            "creator_account_binding",
        )
    ]
    if arguments.extra_current_binding:
        grants.append(
            _grant(
                "creator_account_binding",
                installation_key_id=key.installation_key_id,
                installation_key_jkt=key.installation_key_jkt,
                reference_suffix="-ambiguous",
            )
        )
    store.record_verified_grants(tuple(grants))
    # The authorization rests on one grant per required type, so the ambiguous
    # extra binding stays out of it and keeps falsifying only the ceremony.
    authorized = _authorize_account(
        store,
        tuple(
            grant.reference_id
            for grant in grants[:3]
        ),
    )
    print(json.dumps({
        "authorized_creator_account_id": ACCOUNT_ID if authorized else None,
        "installation_key_id": key.installation_key_id,
        "installation_key_jkt": key.installation_key_jkt,
        "grant_reference_ids": [grant.reference_id for grant in grants],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
