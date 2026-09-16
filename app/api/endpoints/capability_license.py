"""Authenticated local CapabilityLicense delivery endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_authenticated_runtime_policy,
    require_creator,
    verify_csrf_token,
    verify_same_origin,
)
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.analysis_authorization import current_analysis_readiness
from app.security.capability_license_composition import (
    CapabilityLicenseDeliveryReceipt,
    CapabilityLicenseLocalDelivery,
)
from app.security.capability_license_redemption import CapabilityLicenseOpaqueRedemption
from app.security.runtime_policy import AuthContext, RuntimePolicy

router = APIRouter(
    prefix="/api/v1/capability-license",
    tags=["CapabilityLicense"],
    dependencies=[Depends(require_activated_runtime)],
)

_delivery: CapabilityLicenseLocalDelivery | None = None
_redemption: CapabilityLicenseOpaqueRedemption | None = None


class CapabilityLicensePackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: str = Field(min_length=1, max_length=1400)
    seat_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$",
    )


class CapabilityLicenseContinuationRequest(BaseModel):
    """Customer-safe handoff: no package, seat, organization, or installation input."""

    model_config = ConfigDict(extra="forbid")

    continuation: str = Field(
        min_length=48,
        max_length=48,
        pattern=r"^clr1\.[A-Za-z0-9_-]{43}$",
    )


class CapabilityLicenseDeliveryResponse(BaseModel):
    state: Literal["active"] = "active"
    reference_id: str
    license_id: str
    issuance_id: str


class CapabilityLicenseRedemptionResponse(BaseModel):
    """Customer-safe submission result; canonical readiness must be read separately."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["checking"] = "checking"


class CapabilityLicenseReadinessResponse(BaseModel):
    """Closed customer view reconstructed from current durable local authority."""

    model_config = ConfigDict(extra="forbid")

    schema: Literal["ofca-analysis-readiness/v1"] = "ofca-analysis-readiness/v1"
    commercial_authority: Literal["required", "active", "unavailable"]
    analysis_admission: Literal["blocked", "admitted"]


def configure_capability_license_delivery(
    action: CapabilityLicenseLocalDelivery,
) -> None:
    global _delivery
    _delivery = action


def configure_capability_license_redemption(
    action: CapabilityLicenseOpaqueRedemption,
) -> None:
    global _redemption
    _redemption = action


def _configured_delivery() -> CapabilityLicenseLocalDelivery:
    if _delivery is None:
        raise HTTPException(
            status_code=503,
            detail="CapabilityLicense delivery is unavailable",
        )
    return _delivery


def _configured_redemption() -> CapabilityLicenseOpaqueRedemption:
    if _redemption is None:
        raise HTTPException(
            status_code=503,
            detail="CapabilityLicense redemption is unavailable",
        )
    return _redemption


def _result_response(
    result: CapabilityLicenseDeliveryReceipt | str,
) -> CapabilityLicenseDeliveryResponse:
    if not isinstance(result, CapabilityLicenseDeliveryReceipt):
        if result in {"hosted_origin_unavailable", "hosted_unavailable"}:
            status = 503
        elif result == "redemption_expired":
            status = 410
        elif result == "redemption_invalid":
            status = 404
        elif result == "redemption_unauthorized":
            status = 403
        else:
            status = 409
        raise HTTPException(status_code=status, detail=result)
    return CapabilityLicenseDeliveryResponse(
        reference_id=result.reference_id,
        license_id=result.license_id,
        issuance_id=result.issuance_id,
    )


def _redemption_response(
    result: CapabilityLicenseDeliveryReceipt | str,
) -> CapabilityLicenseRedemptionResponse:
    if not isinstance(result, CapabilityLicenseDeliveryReceipt):
        _result_response(result)
        raise AssertionError("unreachable")
    return CapabilityLicenseRedemptionResponse()


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


def _authorize_local_caller(
    request: Request,
    policy: RuntimePolicy,
) -> None:
    require_creator(policy)
    verify_same_origin(request)


def _customer_analysis_readiness(policy: RuntimePolicy):
    identity = policy.identity
    if identity is None:
        raise RuntimeError("authenticated customer identity is required")
    # The canonical readiness evaluator is defined for the local analysis agent.
    # This read-only projection scopes that evaluation to the authenticated creator's
    # account; it does not mint agent credentials, cache admission, or mutate authority.
    analysis_identity = AuthContext(
        identity.principal_id,
        identity.creator_account_id,
        "agent",
    )
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    return current_analysis_readiness(store, analysis_identity)


@router.get("/readiness", response_model=CapabilityLicenseReadinessResponse)
async def capability_license_readiness(
    response: Response,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
) -> CapabilityLicenseReadinessResponse:
    """Return the canonical closed readiness document for the signed-in customer."""

    require_creator(policy)
    readiness = await run_in_threadpool(_customer_analysis_readiness, policy)
    response.headers["Cache-Control"] = "no-store"
    return CapabilityLicenseReadinessResponse(
        commercial_authority=readiness.commercial_authority,
        analysis_admission=readiness.analysis_admission,
    )


@router.post("/redeem", response_model=CapabilityLicenseRedemptionResponse)
async def redeem_capability_license_continuation(
    request: Request,
    body: CapabilityLicenseContinuationRequest,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> CapabilityLicenseRedemptionResponse:
    """Redeem one opaque Hosted authorization, then require a readiness re-read."""

    _authorize_local_caller(request, policy)
    verify_csrf_token(policy, csrf)
    result = await run_in_threadpool(
        _configured_redemption().redeem,
        continuation=body.continuation,
    )
    return _redemption_response(result)


@router.post("/activate", response_model=CapabilityLicenseDeliveryResponse)
async def activate_capability_license(
    request: Request,
    body: CapabilityLicensePackageRequest,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> CapabilityLicenseDeliveryResponse:
    _authorize_local_caller(request, policy)
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
    _authorize_local_caller(request, policy)
    verify_csrf_token(policy, csrf)
    return await _deliver(
        body,
        operation="finalize",
    )