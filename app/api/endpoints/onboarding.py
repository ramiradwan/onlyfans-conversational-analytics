"""Authenticated local snapshot and push adapter for the persistent workspace."""
from __future__ import annotations

from functools import lru_cache
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response

from app.api.security import get_runtime_policy
from app.api.activation import require_activated_runtime
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyStore, require_journey
from app.provisioning.events import events, CAPABILITIES
from app.provisioning.session import ProvisioningSessionManager, PROVISIONING_HOST
from app.provisioning.state import brain_snapshot, next_authority_expiry

router = APIRouter(prefix="/api/v1/onboarding", tags=["Onboarding"])
resume_router = APIRouter(tags=["Onboarding"])
_enrollment = None


@lru_cache(maxsize=1)
def _store(path):
    return SQLiteAuthenticationStore(path)


def _resume_hosted(journey_id):
    global _enrollment
    if settings.environment.lower() == "test":
        return
    if _enrollment is None:
        from app.core.customer_release import resolve_hosted_api_origin, resolve_hosted_onboarding_start
        from app.provisioning.initial_handoff import InitialInstallationEnrollment
        origin = resolve_hosted_api_origin()
        hosted_start = resolve_hosted_onboarding_start()
        if not origin or not hosted_start:
            return
        _enrollment = InitialInstallationEnrollment(_store(settings.auth_database_path), hosted_origin=origin,
            hosted_start_url=hosted_start)
    _enrollment.resume(journey_id)


async def stop_hosted():
    global _enrollment
    if _enrollment is not None:
        await _enrollment.stop()
        _enrollment = None


def _scope(request: Request):
    if request.headers.get("host") != PROVISIONING_HOST:
        raise HTTPException(421, "Local host is required")
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise HTTPException(403, "Local origin is required")
    store = _store(settings.auth_database_path)
    journeys = OnboardingJourneyStore(store)
    policy = get_runtime_policy(request)
    if policy.identity is None:
        session = ProvisioningSessionManager(None, journeys=journeys).require_session(request)
        supplied_journey = request.headers.get("X-Onboarding-Journey")
        if supplied_journey is not None and supplied_journey != session.journey_id:
            raise HTTPException(409, "Onboarding scope changed")
        return store, session.journey_id, None
    try:
        journey_id = require_journey(request.headers.get("X-Onboarding-Journey", ""))
    except ValueError:
        raise HTTPException(400, "Journey is required") from None
    if journeys.get(journey_id) is None:
        # A correlated receiving workspace is a new draft, never inherited access.
        journeys.open(journey_id)
    return store, journey_id, policy.identity


@router.get("/state")
def state(request: Request):
    store, journey_id, identity = _scope(request)
    return JSONResponse(brain_snapshot(store, journey_id, identity), headers={"Cache-Control": "no-store",
                        "X-Onboarding-Capabilities": CAPABILITIES})


@router.get("/events")
async def stream(request: Request):
    store, journey_id, identity = _scope(request)
    _resume_hosted(journey_id)
    def authorize():
        _, current_journey, current_identity = _scope(request)
        if current_journey != journey_id or current_identity != identity:
            raise HTTPException(401, "Onboarding scope changed")
    return StreamingResponse(events.stream(lambda: brain_snapshot(store, journey_id, identity), authorize,
        journey_id=journey_id, expires_in=lambda: next_authority_expiry(store)),
        media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
        "X-Onboarding-Capabilities": CAPABILITIES})


@resume_router.get("/provisioning", include_in_schema=False)
def resume(request: Request):
    # Only the persisted provisioning browser can follow this restart path. A
    # locator or ordinary visit does not mint a local runtime session.
    try:
        session = ProvisioningSessionManager(None, journeys=OnboardingJourneyStore(
            _store(settings.auth_database_path))).require_session(request)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        from app.provisioning.resume import resume_shell
        return resume_shell()
    return RedirectResponse("/#journey=" + session.journey_id, status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@resume_router.get("/provisioning/resume.js", include_in_schema=False)
def browser_resume_script(request: Request):
    from app.provisioning.resume import resume_script
    return resume_script(request)


@resume_router.get("/provisioning/native-return", include_in_schema=False)
def native_return(request: Request):
    from app.provisioning.native_return import native_return_shell
    return native_return_shell(request, settings.extension_id)


@resume_router.get("/provisioning/native-return.js", include_in_schema=False)
@resume_router.get("/provisioning/native-json.mjs", include_in_schema=False)
def native_script(request: Request):
    from app.provisioning.native_return import native_return_script
    return native_return_script(request)


def _require_browser_asset_session(request):
    try:
        ProvisioningSessionManager(None, journeys=OnboardingJourneyStore(
            _store(settings.auth_database_path))).require_session(request)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        # Static validation modules are also used by the native return of an
        # existing passkey-authenticated browser; they grant no setup authority.
        from app.api.security import get_authenticated_runtime_policy
        get_authenticated_runtime_policy(get_runtime_policy(request))


@resume_router.get("/provisioning/provisioning.js", include_in_schema=False)
def provisioning_module(request: Request):
    _require_browser_asset_session(request)
    from app.core.resource_paths import resource_path
    return Response(resource_path("app/provisioning/provisioning.js").read_bytes(),
                    media_type="application/javascript", headers={"Cache-Control": "no-store"})


@resume_router.get("/provisioning/onboarding/{name}.mjs", include_in_schema=False)
def onboarding_module(request: Request, name: str):
    _require_browser_asset_session(request)
    if name not in {"client", "projection", "json", "sse"}:
        raise HTTPException(404, "Not found")
    from app.core.resource_paths import resource_path
    return Response(resource_path("shared/onboarding/" + name + ".mjs").read_bytes(),
                    media_type="application/javascript", headers={"Cache-Control": "no-store"})


@lru_cache(maxsize=1)
def _activation_return():
    from app.api.activation import require_activated_runtime
    from app.api.security import get_authenticated_runtime_policy, require_creator, verify_csrf_token, verify_same_origin
    from app.api.endpoints.capability_license import _configured_redemption
    from app.core.customer_release import load_customer_release_config
    from app.provisioning.activation_return import ActivationReturn

    def authorize(request, journey, mutation):
        require_activated_runtime()
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise HTTPException(403, "Local origin is required")
        origins = request.headers.getlist("origin")
        if origins != ["http://" + PROVISIONING_HOST] and (mutation or origins):
            raise HTTPException(403, "Local origin is required")
        policy = get_authenticated_runtime_policy(get_runtime_policy(request))
        require_creator(policy)
        if mutation:
            verify_same_origin(request)
            values = request.headers.getlist("x-csrf-token")
            verify_csrf_token(policy, values[0] if len(values) == 1 else None)
        return policy.identity.creator_account_id

    return ActivationReturn(_store(settings.auth_database_path),
        hosted_url=load_customer_release_config().hosted_onboarding_url,
        redeem=lambda **kwargs: _configured_redemption().redeem(**kwargs), authorize=authorize, runtime=True)


from app.provisioning.activation_return import install_routes as install_activation_return_routes
install_activation_return_routes(resume_router, _activation_return, dependencies=[Depends(require_activated_runtime)])
