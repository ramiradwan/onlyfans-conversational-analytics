"""Authenticated local CapabilityLicense activation and reissue endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_authenticated_runtime_policy,
    require_creator,
    verify_csrf_token,
    verify_same_origin,
)
from app.security.capability_license_composition import (
    CapabilityLicenseDeliveryReceipt,
    CapabilityLicenseLocalDelivery,
)
from app.security.runtime_policy import RuntimePolicy


router = APIRouter(
    prefix="/api/v1/capability-license",
    tags=["CapabilityLicense"],
    dependencies=[Depends(require_activated_runtime)],
)

_delivery: CapabilityLicenseLocalDelivery | None = None


class CapabilityLicensePackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str = Field(min_length=1, max_length=1400)
    seat_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$",
    )


class CapabilityLicenseDeliveryResponse(BaseModel):
    state: Literal["active"] = "active"
    reference_id: str
    license_id: str
    issuance_id: str


def configure_capability_license_delivery(
    action: CapabilityLicenseLocalDelivery,
) -> None:
    global _delivery
    _delivery = action


def _configured_delivery() -> CapabilityLicenseLocalDelivery:
    if _delivery is None:
        raise HTTPException(
            status_code=503,
            detail="CapabilityLicense delivery is unavailable",
        )
    return _delivery


def _result_response(
    result: CapabilityLicenseDeliveryReceipt | str,
) -> CapabilityLicenseDeliveryResponse:
    if not isinstance(result, CapabilityLicenseDeliveryReceipt):
        status = (
            503
            if result in {"hosted_origin_unavailable", "hosted_unavailable"}
            else 409
        )
        raise HTTPException(status_code=status, detail=result)
    return CapabilityLicenseDeliveryResponse(
        reference_id=result.reference_id,
        license_id=result.license_id,
        issuance_id=result.issuance_id,
    )


async def _deliver(
    body: CapabilityLicensePackageRequest,
    *,
    operation: Literal["activate", "finalize"],
) -> CapabilityLicenseDeliveryResponse:
    action = _configured_delivery()
    result = await run_in_threadpool(
        action.activate if operation == "activate" else action.finalize_reissue,
        package=body.package.strip(),
        seat_id=body.seat_id,
    )
    return _result_response(result)


@router.post("/activate", response_model=CapabilityLicenseDeliveryResponse)
async def activate_capability_license(
    request: Request,
    body: CapabilityLicensePackageRequest,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> CapabilityLicenseDeliveryResponse:
    require_creator(policy)
    verify_same_origin(request)
    verify_csrf_token(policy, csrf)
    return await _deliver(
        body,
        operation="activate",
    )


@router.post("/reissue", response_model=CapabilityLicenseDeliveryResponse)
async def finalize_capability_license_reissue(
    request: Request,
    body: CapabilityLicensePackageRequest,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> CapabilityLicenseDeliveryResponse:
    require_creator(policy)
    verify_same_origin(request)
    verify_csrf_token(policy, csrf)
    return await _deliver(
        body,
        operation="finalize",
    )
