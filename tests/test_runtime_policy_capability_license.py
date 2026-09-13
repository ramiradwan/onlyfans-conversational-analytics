from __future__ import annotations

from dataclasses import replace

import pytest

from app.security.runtime_policy import (
    AnalysisRunContext,
    AuthContext,
    AuthorizationEpoch,
    CapabilityLicenseAuthority,
    IdentityAccountAuthority,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
    authorized_account,
    require_analysis_run,
)

IDENTITY = AuthContext("principal-1", "creator-1", "creator")
IDENTITY_AUTHORITY = IdentityAccountAuthority(
    organization_id="org.acme",
    installation_id="installation-1",
    installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
    installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    grant_reference_ids=("install", "membership", "binding"),
    grant_types=("installation_grant", "membership_snapshot", "creator_account_binding"),
)
COMMERCIAL = CapabilityLicenseAuthority(
    reference_id="caplic.digest",
    object_digest="a" * 64,
    license_id="0198a1b2-c3d4-7000-8000-000000000001",
    issuance_id="0198a1b2-c3d4-7000-8000-000000000002",
    subject="organization:org.acme:installation:installation-1:seat:seat-1:capability:analysis-run",
    organization_id="org.acme",
    installation_id="installation-1",
    installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
    installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    seat_id="seat-1",
    seat_scope="organization-installation-seat",
    capability="analysis-run",
    licensed_major_version=3,
    compatible_artifact_family="analysis-artifact",
    update_rights=True,
    fallback_major_versions=(2,),
    signer_kid="capability-license-key",
)
CONTEXT = AnalysisRunContext(
    organization_id="org.acme",
    installation_id="installation-1",
    installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
    installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    seat_id="seat-1",
    seat_scope="organization-installation-seat",
    capability="analysis-run",
    selected_major_version=3,
    artifact_family="analysis-artifact",
    requires_update_rights=True,
)


def _policy() -> RuntimePolicy:
    return RuntimePolicy(
        identity=IDENTITY,
        authorization_epoch=AuthorizationEpoch(1),
        identity_authority=IDENTITY_AUTHORITY,
        commercial_authority=COMMERCIAL,
    )


def test_analysis_run_requires_both_separate_authority_predicates() -> None:
    require_analysis_run(_policy(), CONTEXT)

    with pytest.raises(RuntimeAuthorizationDenied, match="CapabilityLicense"):
        require_analysis_run(replace(_policy(), commercial_authority=None), CONTEXT)
    with pytest.raises(RuntimeAuthorizationDenied, match="identity/account"):
        require_analysis_run(replace(_policy(), identity_authority=None), CONTEXT)


def test_legacy_license_entitlement_never_substitutes_for_commercial_authority() -> None:
    legacy = replace(
        _policy(),
        commercial_authority=None,
        signed_object_reference_ids=("license_entitlement-current",),
        signed_object_digests=("b" * 64,),
    )
    with pytest.raises(RuntimeAuthorizationDenied, match="CapabilityLicense"):
        require_analysis_run(legacy, CONTEXT)


def test_fallback_and_update_rights_are_explicit_commercial_predicates() -> None:
    require_analysis_run(
        _policy(),
        replace(CONTEXT, selected_major_version=2, requires_update_rights=False),
    )
    with pytest.raises(RuntimeAuthorizationDenied, match="selected major"):
        require_analysis_run(_policy(), replace(CONTEXT, selected_major_version=1))
    with pytest.raises(RuntimeAuthorizationDenied, match="update mode"):
        require_analysis_run(
            replace(_policy(), commercial_authority=replace(COMMERCIAL, update_rights=False)),
            CONTEXT,
        )


def test_existing_account_data_remains_authorized_without_commercial_authority() -> None:
    policy = replace(_policy(), commercial_authority=None)
    assert authorized_account(policy, "creator-1") == "creator-1"
