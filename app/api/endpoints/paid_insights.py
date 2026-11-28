"""Read the private paid-insights pilot status for the signed-in account."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.api.activation import require_activated_runtime
from app.api.dependencies import account_bound_to_session, get_authenticated_account_session
from app.api.security import require_creator
from app.security.runtime_policy import RuntimeAuthorizationDenied


_HEADERS = {"Cache-Control": "no-store"}


class _PrivateStatusRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            try:
                return await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **_HEADERS}
                raise

        return protected


router = APIRouter(prefix="/api/v1/nlp", dependencies=[Depends(require_activated_runtime)],
                   route_class=_PrivateStatusRoute)


class PaidInsightsStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["paid-insights-pilot.v1"] = "paid-insights-pilot.v1"
    profile_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    can_acquire_preview: bool = False
    can_preview: bool = False
    can_purchase: bool = False
    can_process: bool = False


def pilot_status(policy):
    from app.services.nlp_setup import load_execution_release

    release = load_execution_release()
    if release is None:
        return None

    from app.services.installed_text_analysis import installed_text_analysis_authorities
    from app.services.text_analysis_native_acquisition import native_text_analysis_acquisition

    return _status_for(release, policy, installed_text_analysis_authorities(), native_text_analysis_acquisition())


def _status_for(release, policy, authorities, acquisition):
    # Each explicit action verifies its authority again at its own write boundary.
    passive = PaidInsightsStatus(profile_sha256=release.profile_sha256)
    if authorities is None:
        return passive
    try:
        preview_state = authorities.preview_state(policy)
        licensed = authorities.read_current(policy, kind="license") is not None
        offer_preview = (preview_state is None and acquisition is not None
                         and acquisition.status(policy, "preview")["eligible"] is True)
        offer_purchase = (not licensed and acquisition is not None
                          and acquisition.status(policy, "paid")["eligible"] is True)
    except (OSError, ValueError):
        # Existing reports have their own authenticated local read and erasure checks.
        # A refused or unavailable commercial read closes new actions on this screen.
        return passive
    return PaidInsightsStatus(
        profile_sha256=release.profile_sha256,
        can_acquire_preview=offer_preview,
        can_preview=preview_state == "available",
        can_purchase=offer_purchase,
        can_process=licensed,
    )


@router.get("/paid-insights", response_model=PaidInsightsStatus)
def status(request: Request, response: Response, policy=Depends(get_authenticated_account_session)):
    response.headers.update(_HEADERS)
    account_bound_to_session(policy, None)
    require_creator(policy)
    if request.query_params:
        raise HTTPException(422, detail={"code": "paid_insights_invalid"}, headers=_HEADERS)
    try:
        result = pilot_status(policy)
    except RuntimeAuthorizationDenied:
        raise HTTPException(403, detail={"code": "paid_insights_account_unavailable"}, headers=_HEADERS) from None
    except (OSError, ValueError):
        raise HTTPException(503, detail={"code": "paid_insights_unavailable"}, headers=_HEADERS) from None
    if result is None:
        raise HTTPException(404, detail={"code": "paid_insights_unavailable"}, headers=_HEADERS)
    return result
