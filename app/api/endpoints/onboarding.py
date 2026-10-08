"""Authenticated local snapshot and push adapter for the persistent workspace."""
from __future__ import annotations

from functools import lru_cache
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response

from app.api.security import get_runtime_policy
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


def _require_browser_asset_session(request):
    ProvisioningSessionManager(None, journeys=OnboardingJourneyStore(
        _store(settings.auth_database_path))).require_session(request)


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
