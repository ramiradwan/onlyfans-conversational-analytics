"""Load the account passkey protocol and signer trust for the selected release."""
from __future__ import annotations

from app.core.customer_release import AccountPasskeyRelease
from app.security.account_passkey import AccountPasskeySchemas
from app.security.account_passkey_records import AccountPasskeyRecordVerifier
from app.security.signed_objects import SignedObjectRejected
from contracts.loader import _APPROVED_SOURCE_COMMIT


def passkey_inputs(
    release: AccountPasskeyRelease,
) -> tuple[AccountPasskeySchemas, AccountPasskeyRecordVerifier]:
    if release.contract_revision != _APPROVED_SOURCE_COMMIT:
        raise SignedObjectRejected()
    schemas = AccountPasskeySchemas.from_release()
    # Check the minimum protocol in the installed release before offering enrollment.
    schemas.validate("native-error", {"error": "proof_invalid"})
    verifier = AccountPasskeyRecordVerifier(
        schemas,
        release.trust.encode("utf-8"),
        pinned_sha256=release.trust_sha256,
        environment=release.environment,
        minimum_sequence=release.trust_sequence,
    )
    if verifier.sequence != release.trust_sequence:
        raise SignedObjectRejected()
    return schemas, verifier
