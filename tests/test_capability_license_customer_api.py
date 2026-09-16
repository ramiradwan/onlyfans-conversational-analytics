from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.endpoints.capability_license import (
    CapabilityLicenseDeliveryResponse,
    CapabilityLicenseReadinessResponse,
    CapabilityLicenseRedemptionResponse,
    _redemption_response,
    _result_response,
)
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt


def test_customer_redemption_success_contains_only_checking_state() -> None:
    result = _redemption_response(
        CapabilityLicenseDeliveryReceipt(
            reference_id="caplic.internal-reference",
            license_id="license.internal",
            issuance_id="issuance.internal",
        )
    )

    assert isinstance(result, CapabilityLicenseRedemptionResponse)
    assert result.model_dump() == {"state": "checking"}
    serialized = result.model_dump_json()
    for protected in (
        "reference_id",
        "license_id",
        "issuance_id",
        "seat_id",
        "package",
        "organization_id",
        "installation_id",
        "installation_key_id",
        "proof",
        "signature",
        "capability_license",
    ):
        assert protected not in serialized
    assert "active" not in serialized


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("redemption_expired", 410),
        ("redemption_invalid", 404),
        ("redemption_unauthorized", 403),
        ("redemption_mismatch", 409),
        ("reissue_authorization_required", 409),
        ("hosted_origin_unavailable", 503),
        ("hosted_unavailable", 503),
    ],
)
def test_customer_redemption_preserves_authoritative_failure_mapping(
    result: str,
    status: int,
) -> None:
    with pytest.raises(HTTPException) as error:
        _redemption_response(result)
    assert error.value.status_code == status
    assert error.value.detail == result


def test_internal_delivery_response_contract_is_unchanged() -> None:
    result = _result_response(
        CapabilityLicenseDeliveryReceipt(
            reference_id="caplic.ref",
            license_id="license-1",
            issuance_id="issuance-1",
        )
    )

    assert isinstance(result, CapabilityLicenseDeliveryResponse)
    assert result.model_dump() == {
        "state": "active",
        "reference_id": "caplic.ref",
        "license_id": "license-1",
        "issuance_id": "issuance-1",
    }


def test_customer_readiness_contract_is_closed_and_contains_no_authority_coordinates() -> None:
    result = CapabilityLicenseReadinessResponse(
        commercial_authority="active",
        analysis_admission="blocked",
    )

    assert result.model_dump() == {
        "schema": "ofca-analysis-readiness/v1",
        "commercial_authority": "active",
        "analysis_admission": "blocked",
    }
    assert set(CapabilityLicenseReadinessResponse.model_fields) == {
        "schema",
        "commercial_authority",
        "analysis_admission",
    }
