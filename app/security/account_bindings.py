"""Resolution of the accounts an installation may currently act for.

An installation authorizes zero, one, or many creator accounts. Eligibility is
never stored: it is resolved from a durable authorization record and the current
state of the grant tuple that record rests on, so an expired or revoked grant
stops resolving without anything having to write to the binding.

No function here answers "which account does this installation have". Callers
that need exactly one account ask for the sole eligible account and are refused
when the set does not hold exactly one, and callers holding a session ask only
about the account that session is already scoped to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

from app.persistence.auth import (
    AuthenticationStore,
    AuthorizedAccountBinding,
    RevocationKey,
    RevocationScopeType,
    VerifiedGrantReference,
)
from app.security.grant_types import CREATOR_ACCOUNT_BINDING, MEMBERSHIP_SNAPSHOT
from app.security.runtime_policy import (
    RuntimePolicy,
    authorized_account,
    require_identity,
)


ACCOUNT_BINDING_GRANT_TYPE = CREATOR_ACCOUNT_BINDING
MEMBERSHIP_GRANT_TYPE = MEMBERSHIP_SNAPSHOT

AccountResolutionRefusal = Literal[
    "no_eligible_account",
    "ambiguous_account_context",
]


class AccountResolutionRefused(ValueError):
    """Refusal carrying a fixed message and a nonsecret reason code."""

    def __init__(self, reason: AccountResolutionRefusal) -> None:
        super().__init__("No single eligible account resolves")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EligibleAccount:
    """One account an installation may currently act for."""

    creator_account_id: str
    platform_creator_id: str
    grant_bundle_sha256: str
    grant_reference_ids: tuple[str, ...]


def eligible_accounts(
    store: AuthenticationStore, *, now: datetime
) -> tuple[EligibleAccount, ...]:
    """Return every currently eligible account, in account order.

    The result is empty for an installation that has authorized no account and
    for one whose every authorization has lapsed. Both are ordinary states.
    """

    resolved = [
        EligibleAccount(
            creator_account_id=binding.creator_account_id,
            platform_creator_id=binding.platform_creator_id,
            grant_bundle_sha256=binding.grant_bundle_sha256,
            grant_reference_ids=binding.grant_reference_ids,
        )
        for binding in store.authorized_account_bindings()
        if _binding_is_eligible(store, binding, now=now)
    ]
    return tuple(sorted(resolved, key=lambda account: account.creator_account_id))


def sole_eligible_account(
    store: AuthenticationStore, *, now: datetime
) -> EligibleAccount:
    """Return the one eligible account, refusing zero and refusing several.

    Several eligible accounts is an ambiguous account context. It is refused
    rather than resolved by picking the newest, the first, or the only one that
    happens to sort first.
    """

    accounts = eligible_accounts(store, now=now)
    if not accounts:
        raise AccountResolutionRefused("no_eligible_account")
    if len(accounts) > 1:
        raise AccountResolutionRefused("ambiguous_account_context")
    return accounts[0]


def eligible_account_for_policy(
    store: AuthenticationStore, policy: RuntimePolicy, *, now: datetime
) -> EligibleAccount:
    """Return the eligible account one authenticated session is scoped to.

    The account comes from the session, never from the installation. A policy
    carrying no identity has no account scope and is refused by
    `require_identity` before any binding is read.
    """

    identity = require_identity(policy)
    for account in eligible_accounts(store, now=now):
        if account.creator_account_id == identity.creator_account_id:
            return account
    raise AccountResolutionRefused("no_eligible_account")


def revoke_account_binding(
    store: AuthenticationStore, policy: RuntimePolicy, creator_account_id: str
) -> bool:
    """Revoke one authorized account binding on behalf of a scoped session.

    `authorized_account` refuses a session that names an account other than its
    own, so a session scoped to one account cannot revoke another's authority.
    """

    return store.revoke_authorized_account_binding(
        authorized_account(policy, creator_account_id)
    )


def _binding_is_eligible(
    store: AuthenticationStore,
    binding: AuthorizedAccountBinding,
    *,
    now: datetime,
) -> bool:
    if binding.revoked_at is not None:
        return False
    if store.scope_is_revoked(
        RevocationKey(
            RevocationScopeType.CREATOR_ACCOUNT, binding.creator_account_id
        )
    ):
        return False
    basis = _current_basis(store, binding, now=now)
    if basis is None:
        return False
    return _basis_is_coherent(basis, account_id=binding.creator_account_id)


def _current_basis(
    store: AuthenticationStore,
    binding: AuthorizedAccountBinding,
    *,
    now: datetime,
) -> dict[str, VerifiedGrantReference] | None:
    """Return the binding's grant tuple when every member is still current."""

    basis: dict[str, VerifiedGrantReference] = {}
    for reference_id in binding.grant_reference_ids:
        grant = store.verified_grant(reference_id)
        if grant is None:
            return None
        if not grant.valid_from <= now < grant.expires_at:
            return None
        if store.scope_is_revoked(
            RevocationKey(RevocationScopeType.VERIFIED_GRANT, reference_id)
        ):
            return None
        if grant.grant_type in basis:
            return None
        basis[grant.grant_type] = grant
    return basis


def _basis_is_coherent(
    basis: Mapping[str, VerifiedGrantReference], *, account_id: str
) -> bool:
    """Check the grant tuple still names the account it was recorded for."""

    account_binding = basis.get(ACCOUNT_BINDING_GRANT_TYPE)
    if account_binding is None or account_binding.creator_account_id != account_id:
        return False
    membership = basis.get(MEMBERSHIP_GRANT_TYPE)
    if membership is None:
        return False
    allowed = membership.allowed_creator_account_ids
    return allowed is not None and account_id in allowed
