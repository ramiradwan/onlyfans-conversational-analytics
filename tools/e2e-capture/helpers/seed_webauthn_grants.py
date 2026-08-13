"""Seed synthetic post-verification grant references for one disposable E2E store."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PRODUCT_ROOT))

from app.persistence.auth import SQLiteAuthenticationStore, VerifiedGrantReference


def _grant(
    grant_type: str,
    *,
    installation_key_id: str,
    installation_key_jkt: str,
    reference_suffix: str = "",
) -> VerifiedGrantReference:
    now = datetime.now(timezone.utc)
    account_id = "dev-creator-account"
    reference_id = f"e2e-{grant_type}{reference_suffix}"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode("utf-8")).hexdigest(),
        issuer="https://e2e.invalid/verified-grant-store",
        subject="e2e-local-principal",
        installation_id="e2e-temporary-installation",
        creator_account_id=(
            account_id if grant_type == "creator_account_binding" else None
        ),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        verified_at=now,
        organization_id="e2e-organization",
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
    print(json.dumps({
        "installation_key_id": key.installation_key_id,
        "installation_key_jkt": key.installation_key_jkt,
        "grant_reference_ids": [grant.reference_id for grant in grants],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
