from __future__ import annotations

import pytest

from app.security.analysis_authorization import require_current_analysis_run
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import (
    AuthContext,
    AuthorizationEpoch,
    CapabilityLicenseAuthority,
    IdentityAccountAuthority,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
)


def _identity_authority() -> IdentityAccountAuthority:
    return IdentityAccountAuthority(
        organization_id="org.acme",
        installation_id="install.primary",
        installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
        installation_key_jkt="A" * 43,
        grant_reference_ids=("installation", "membership", "binding"),
        grant_types=tuple(ACCOUNT_AUTHORITY_GRANT_TYPES),
    )


def _commercial(
    *,
    licensed_major_version: int = 3,
    fallback_major_versions: tuple[int, ...] = (),
) -> CapabilityLicenseAuthority:
    return CapabilityLicenseAuthority(
        reference_id="caplic.ref",
        object_digest="d" * 64,
        license_id="license-001",
        issuance_id="issuance-001",
        subject="organization:org.acme:installation:install.primary:seat:seat.analysis.001:capability:analysis-run",
        organization_id="org.acme",
        installation_id="install.primary",
        installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
        installation_key_jkt="A" * 43,
        seat_id="seat.analysis.001",
        seat_scope="organization-installation-seat",
        capability="analysis-run",
        licensed_major_version=licensed_major_version,
        compatible_artifact_family="analysis-artifact",
        update_rights=False,
        fallback_major_versions=fallback_major_versions,
        signer_kid="bc1.cl.test",
    )


def _policy(
    *,
    identity_authority: bool = True,
    commercial: CapabilityLicenseAuthority | None = None,
    include_commercial: bool = True,
) -> RuntimePolicy:
    return RuntimePolicy(
        identity=AuthContext("principal-1", "creator-1", "agent"),
        authorization_epoch=AuthorizationEpoch(1),
        identity_authority=_identity_authority() if identity_authority else None,
        commercial_authority=(
            (commercial or _commercial()) if include_commercial else None
        ),
    )


def test_new_analysis_denied_without_capability_license() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(_policy(include_commercial=False))


def test_new_analysis_denied_without_identity_account_authority() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(_policy(identity_authority=False))


def test_new_analysis_denied_when_license_does_not_cover_local_major() -> None:
    with pytest.raises(RuntimeAuthorizationDenied):
        require_current_analysis_run(
            _policy(commercial=_commercial(licensed_major_version=4))
        )


def test_new_analysis_allowed_by_explicit_fallback_for_local_major() -> None:
    require_current_analysis_run(
        _policy(
            commercial=_commercial(
                licensed_major_version=4,
                fallback_major_versions=(3,),
            )
        )
    )


def test_new_analysis_allowed_with_both_current_authorities() -> None:
    require_current_analysis_run(_policy())
