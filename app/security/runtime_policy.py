"""Immutable inputs and decisions for local runtime authorization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES


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
class IdentityAccountAuthority:
    """Verified identity/account authority kept distinct from commercial authority."""

    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    grant_reference_ids: tuple[str, ...]
    grant_types: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_reference_ids", tuple(self.grant_reference_ids))
        object.__setattr__(self, "grant_types", tuple(self.grant_types))


@dataclass(frozen=True, slots=True)
class CapabilityLicenseAuthority:
    """Verified local commercial authority for one licensed capability."""

    reference_id: str
    object_digest: str
    license_id: str
    issuance_id: str
    subject: str
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    seat_id: str
    seat_scope: str
    capability: str
    licensed_major_version: int
    compatible_artifact_family: str
    update_rights: bool
    fallback_major_versions: tuple[int, ...]
    signer_kid: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fallback_major_versions", tuple(self.fallback_major_versions)
        )


@dataclass(frozen=True, slots=True)
class AnalysisRunContext:
    """Independently trusted local selection for a new licensed analysis run."""

    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    seat_id: str
    seat_scope: str
    capability: str
    selected_major_version: int
    artifact_family: str
    requires_update_rights: bool = False


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """One immutable snapshot for a local authorization decision."""

    identity: AuthContext | None
    authorization_epoch: AuthorizationEpoch
    signed_object_digests: tuple[str, ...] = ()
    signed_object_reference_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    revocations: tuple[RevocationObservation, ...] = ()
    identity_authority: IdentityAccountAuthority | None = None
    commercial_authority: CapabilityLicenseAuthority | None = None

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


def require_analysis_run(policy: RuntimePolicy, context: AnalysisRunContext) -> None:
    """Authorize a new analysis run from separate identity and commercial predicates."""

    require_identity(policy)
    identity_authority = policy.identity_authority
    if identity_authority is None:
        raise RuntimeAuthorizationDenied("Current identity/account authority is required")
    if not set(ACCOUNT_AUTHORITY_GRANT_TYPES).issubset(identity_authority.grant_types):
        raise RuntimeAuthorizationDenied("Current identity/account authority is incomplete")
    if (
        identity_authority.organization_id != context.organization_id
        or identity_authority.installation_id != context.installation_id
        or identity_authority.installation_key_id != context.installation_key_id
        or identity_authority.installation_key_jkt != context.installation_key_jkt
    ):
        raise RuntimeAuthorizationDenied("Identity/account authority does not match local runtime")

    commercial = policy.commercial_authority
    if commercial is None:
        raise RuntimeAuthorizationDenied("CapabilityLicense authority is required")
    if (
        commercial.organization_id != context.organization_id
        or commercial.installation_id != context.installation_id
        or commercial.installation_key_id != context.installation_key_id
        or commercial.installation_key_jkt != context.installation_key_jkt
        or commercial.seat_id != context.seat_id
        or commercial.seat_scope != context.seat_scope
        or commercial.capability != context.capability
        or commercial.compatible_artifact_family != context.artifact_family
    ):
        raise RuntimeAuthorizationDenied("CapabilityLicense does not match local runtime")
    if context.selected_major_version != commercial.licensed_major_version and (
        context.selected_major_version not in commercial.fallback_major_versions
    ):
        raise RuntimeAuthorizationDenied("CapabilityLicense does not authorize selected major version")
    if context.requires_update_rights and not commercial.update_rights:
        raise RuntimeAuthorizationDenied("CapabilityLicense does not authorize update mode")
