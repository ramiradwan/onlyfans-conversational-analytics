"""The verified grant types, and the authority each required set stands for.

Every required grant-type set is declared once, here. A predicate elsewhere
imports the set it enforces instead of restating its members, so two enforcement
points cannot come to hold different opinions about what a set contains.

The sets are layered by the authority they carry. Installation activation is the
weakest: it reads installation identity and the person it belongs to, and no
account. Acting for one creator account extends that with the binding naming the
account. Provisioning a new authorization extends that in turn with the
entitlement. Each wider layer is built from the narrower one, so an addition
reaches the wider layers structurally rather than by being copied into them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GrantDenialReason = Literal[
    "revoked", "membership_removed", "role_reduced", "approval_revoked", "entitlement_inactive"
]


@dataclass(frozen=True, slots=True)
class VerifiedGrantDenial:
    """Verified denial evidence without the compact signed object."""

    denial_jti: str
    grant_type: str
    revoked_jti: str
    issued_at: int
    expires_at: int
    effective_at: int
    reason_code: GrantDenialReason
    evidence_sha256: str


INSTALLATION_GRANT = "installation_grant"
MEMBERSHIP_SNAPSHOT = "membership_snapshot"
CREATOR_ACCOUNT_BINDING = "creator_account_binding"
LICENSE_ENTITLEMENT = "license_entitlement"

# Shared verifier/retention bound; checked against the companion profile in tests.
MAX_GRANT_CHARACTERS = 16_384

# Installation identity and the person holding it. An installation that has
# authorized no account satisfies this set, so it activates during setup.
ACTIVATION_GRANT_TYPES = (INSTALLATION_GRANT, MEMBERSHIP_SNAPSHOT)

# Authority to act for one creator account: an active installation plus the
# binding that names the account. Activation alone never reaches this set.
ACCOUNT_AUTHORITY_GRANT_TYPES = ACTIVATION_GRANT_TYPES + (CREATOR_ACCOUNT_BINDING,)

# The tuple one provisioning finalization verifies and records as the provenance
# of an account authorization. Sorted so the recorded order is canonical.
PROVISIONING_GRANT_TYPES = tuple(
    sorted(ACCOUNT_AUTHORITY_GRANT_TYPES + (LICENSE_ENTITLEMENT,))
)

# A paired agent acts under installation identity and one account binding. It
# presents no membership snapshot of its own; the pairing carries the person.
AGENT_PAIRING_GRANT_TYPES = (INSTALLATION_GRANT, CREATOR_ACCOUNT_BINDING)

# The grant types one redeemed installation claim delivers. The account binding
# is not among them: it is issued once an account is approved, not at redemption.
HOSTED_CLAIM_GRANT_TYPES = ACTIVATION_GRANT_TYPES + (LICENSE_ENTITLEMENT,)
