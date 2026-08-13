"""Immutable inputs and decisions for local runtime authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


RuntimeRole = Literal["creator", "operator", "agent"]


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Identity fields carried by a runtime policy."""

    principal_id: str
    creator_account_id: str
    role: RuntimeRole
    platform_creator_id: str | None = None
    session_id: str | None = None
    session_expires_at: int | None = None


@dataclass(frozen=True, slots=True, order=True)
class AuthorizationEpoch:
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("authorization epoch must be non-negative")


@dataclass(frozen=True, slots=True)
class RevocationObservation:
    scope_type: str
    scope_id: str
    version: int


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """One immutable snapshot for a local authorization decision."""

    identity: AuthContext | None
    authorization_epoch: AuthorizationEpoch
    signed_object_digests: tuple[str, ...] = ()
    signed_object_reference_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    revocations: tuple[RevocationObservation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "signed_object_digests", tuple(self.signed_object_digests))
        object.__setattr__(
            self,
            "signed_object_reference_ids",
            tuple(self.signed_object_reference_ids),
        )
        object.__setattr__(self, "revocations", tuple(self.revocations))


class RuntimeAuthorizationDenied(ValueError):
    """Raised when a runtime policy does not authorize an operation."""


class StaleRuntimePolicyError(RuntimeAuthorizationDenied):
    """Raised when durable authorization state supersedes a policy."""


def require_identity(policy: RuntimePolicy) -> AuthContext:
    """Return the verified identity subset required by account-scoped work."""

    if policy.identity is None:
        raise RuntimeAuthorizationDenied("Authenticated session is required")
    return policy.identity


def require_role(policy: RuntimePolicy, role: RuntimeRole) -> None:
    if require_identity(policy).role != role:
        raise RuntimeAuthorizationDenied(f"{role.capitalize()} authority is required")


def authorized_account(
    policy: RuntimePolicy, requested_account_id: str | None
) -> str:
    account_id = require_identity(policy).creator_account_id
    if requested_account_id is not None and requested_account_id != account_id:
        raise RuntimeAuthorizationDenied(
            "The runtime policy cannot access the requested account"
        )
    return account_id
