"""Seed synthetic post-verification authority for one disposable E2E store.

Writes what local provisioning would have written: the verified grant tuple, the
approved candidate it rests on, and the durable record that the installation
authorized the account. Without the authorization record the installation
authorizes no account, which is a valid state that holds no configuration.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PRODUCT_ROOT))

from app.persistence.auth import (
    AuthorizedAccountBinding,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    InstallationKeyUnavailable,
    WindowsCNGInstallationKeyProvider,
)


ACCOUNT_ID = "dev-creator-account"
INSTALLATION_ID = "e2e-temporary-installation"
ORGANIZATION_ID = "e2e-organization"
ASSOCIATION_REQUEST_ID = "e2e-association-request"
PLATFORM_CREATOR_ID = "e2e-platform-creator"

SYNTHETIC_KEY_PROVIDER_NAME = "E2E Synthetic Installation Key Provider"
SYNTHETIC_KEY_NAME = "e2e-temporary-installation-key"


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


def _real_key_provider_available() -> bool:
    """Return whether this host offers the TPM-backed platform provider.

    Mirrors the detection ``app.main.activate_runtime`` already performs at
    Brain startup: constructing the provider raises
    ``InstallationKeyUnavailable`` on a host with none, which is also why
    Brain starts there unactivated instead of failing.
    """

    try:
        WindowsCNGInstallationKeyProvider()
    except InstallationKeyUnavailable:
        return False
    return True


def _synthetic_installation_key_material(installation_key_id: str) -> tuple[str, str]:
    """Return a syntactically valid (jkt, public_key_jwk) pair backed by no real key.

    Nothing on this harness path re-derives or cryptographically verifies the
    thumbprint; ``app.persistence.auth``'s validators only require every field
    to be non-empty and the two values to be recorded together.
    """

    digest = hashlib.sha256(installation_key_id.encode("utf-8")).digest()
    half = len(digest) // 2
    x = base64.urlsafe_b64encode(digest[:half]).rstrip(b"=").decode("ascii")
    y = base64.urlsafe_b64encode(digest[half:]).rstrip(b"=").decode("ascii")
    jwk = json.dumps({"crv": "P-256", "kty": "EC", "x": x, "y": y}, sort_keys=True)
    jkt = base64.urlsafe_b64encode(
        hashlib.sha256(jwk.encode("utf-8")).digest()
    ).rstrip(b"=").decode("ascii")
    return jkt, jwk


def _ensure_installation_key_active(
    store: SQLiteAuthenticationStore,
) -> InstallationKeyReference | None:
    """Return the active installation key, seeding an e2e-only one if needed.

    Production activates a real TPM-backed key at Brain startup when a
    platform key provider is present (``app.main.initialize_installation_key``).
    A host with no such provider starts Brain there unactivated by design, so
    on that host this drives a synthetic key through the store's real
    reserve/activate transitions instead of writing the reference row
    directly. When a real provider is present but no key was activated, this
    seeds nothing and leaves the failure to the caller: a present provider
    that never activated is a real condition to surface, not to paper over.
    """

    key = store.installation_key_reference()
    if key is not None:
        return key
    if _real_key_provider_available():
        return None

    now = datetime.now(timezone.utc)
    installation_key_id = f"{SYNTHETIC_KEY_NAME}-{uuid4()}"
    installation_key_jkt, public_key_jwk = _synthetic_installation_key_material(
        installation_key_id
    )
    store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name=SYNTHETIC_KEY_PROVIDER_NAME,
            provider_key_name=SYNTHETIC_KEY_NAME,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            created_at=now,
        )
    )
    store.activate_installation_key(
        InstallationKeyReference(
            provider_name=SYNTHETIC_KEY_PROVIDER_NAME,
            provider_key_name=SYNTHETIC_KEY_NAME,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            installation_key_id=installation_key_id,
            installation_key_jkt=installation_key_jkt,
            public_key_jwk=public_key_jwk,
            created_at=now,
            activated_at=now,
        )
    )
    return store.installation_key_reference()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-database", required=True, type=Path)
    parser.add_argument("--extra-current-binding", action="store_true")
    arguments = parser.parse_args()

    store = SQLiteAuthenticationStore(arguments.auth_database)
    key = _ensure_installation_key_active(store)
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
