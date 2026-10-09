"""ASGI application available before runtime configuration exists."""

from __future__ import annotations

import asyncio
import html
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Literal, Protocol

from fastapi import FastAPI, Header, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.provisioning.session import (
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
    NATIVE_ENTRY_COOKIE_NAME,
)


PROVISIONING_HANDOFF_PATH = "/api/v1/provisioning/handoff"
PROVISIONING_REDEEM_PATH = "/provisioning/handoff"
PROVISIONING_STATUS_PATH = "/api/v1/provisioning/status"
PROVISIONING_CLAIM_PATH = "/api/v1/provisioning/claim"
PROVISIONING_CREATOR_ASSOCIATION_PATH = "/api/v1/provisioning/creator-association"
PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH = (
    "/api/v1/provisioning/creator-association/acquire"
)
PROVISIONING_FINALIZE_PATH = "/api/v1/provisioning/finalize"
PROVISIONING_CAPABILITY_LICENSE_ACTIVATE_PATH = (
    "/api/v1/provisioning/capability-license/activate"
)
PROVISIONING_CAPABILITY_LICENSE_REISSUE_PATH = (
    "/api/v1/provisioning/capability-license/reissue"
)
PROVISIONING_DISCLOSURE_PATH = (
    "/provisioning/creator-platform-data-risk-disclosure.html"
)
PROVISIONING_SCRIPT_PATH = "/provisioning/provisioning.js"

_MODULE_DIRECTORY = Path(__file__).parent
_SHELL_TEMPLATE = _MODULE_DIRECTORY / "provisioning.html"
_DISCLOSURE_ASSET = _MODULE_DIRECTORY / "creator-platform-data-risk-disclosure.html"
_SCRIPT_ASSET = _MODULE_DIRECTORY / "provisioning.js"
_EXTENSION_ID_PATTERN = re.compile(r"[a-p]{32}")
_PROGRESS_STAGES = {
    "registration_required",
    "creator_confirmation_required",
    "creator_approval_pending",
    "finalization_ready",
    "recovery_required",
}

BoundedIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]
BoundedClaimPackage = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
BoundedCapabilityLicensePackage = Annotated[
    str, StringConstraints(min_length=1, max_length=1400)
]
BoundedSeatIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$",
    ),
]


class ClaimSubmissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: BoundedClaimPackage


class ClaimSubmission(Protocol):
    def __call__(self, *, package: str) -> str | None: ...


class CapabilityLicenseDeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: BoundedCapabilityLicensePackage
    seat_id: BoundedSeatIdentifier


class CapabilityLicenseDelivery(Protocol):
    def activate(self, *, package: str, seat_id: str) -> object: ...
    def finalize_reissue(self, *, package: str, seat_id: str) -> object: ...


class CreatorAssociationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detected_creator_account_id: BoundedIdentifier
    onboarding_transaction_id: BoundedIdentifier | None = None
    organization_id: BoundedIdentifier | None = None
    installation_id: BoundedIdentifier | None = None


class CreatorAssociationInitiation(Protocol):
    def __call__(
        self,
        *,
        detected_creator_account_id: str,
        onboarding_transaction_id: str | None = None,
        organization_id: str | None = None,
        installation_id: str | None = None,
    ) -> object: ...


class CreatorBindingAcquisitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatorBindingAcquisition(Protocol):
    def __call__(self) -> object: ...


class FinalizationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    association_request_id: BoundedIdentifier
    detected_creator_account_id: BoundedIdentifier
    reported_platform_creator_id: BoundedIdentifier | None = None


class FinalizeAction(Protocol):
    def __call__(
        self,
        *,
        association_request_id: str,
        detected_creator_account_id: str,
        reported_platform_creator_id: str | None,
    ) -> str | None: ...


class InitialInstallationAdmission(Protocol):
    """Local browser sees only bounded progress and a nonauthorizing locator."""
    async def prepare(self, journey_id: str) -> dict[str, object]: ...
    async def read_browser_entry(self, journey_id: str) -> dict[str, object]: ...
    def resume(self, journey_id: str) -> None: ...
    async def stop(self) -> None: ...


def _validated_progress(value: object) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != {
        "stage", "association_request_id", "creator_account_id"
    }:
        raise RuntimeError("invalid provisioning progress")
    stage = value["stage"]
    association_request_id = value["association_request_id"]
    creator_account_id = value["creator_account_id"]
    if stage not in _PROGRESS_STAGES:
        raise RuntimeError("invalid provisioning progress")
    coordinates_required = stage in {"creator_approval_pending", "finalization_ready"}
    if coordinates_required:
        if not isinstance(association_request_id, str) or not 1 <= len(association_request_id) <= 200:
            raise RuntimeError("invalid provisioning progress")
        if not isinstance(creator_account_id, str) or not 1 <= len(creator_account_id) <= 200:
            raise RuntimeError("invalid provisioning progress")
    elif association_request_id is not None or creator_account_id is not None:
        raise RuntimeError("invalid provisioning progress")
    return {
        "stage": stage,
        "association_request_id": association_request_id,
        "creator_account_id": creator_account_id,
}


class NativeWorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    journey_id: str | None


class NativeWorkspaceRecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    journey_id: str
    recover: Literal[True]

    @field_validator("recover", mode="before")
    @classmethod
    def literal_recovery(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Recovery must be explicitly true")
        return value


def create_provisioning_app(
    *,
    claim_submission: ClaimSubmission,
    creator_association_initiation: CreatorAssociationInitiation,
    creator_binding_acquisition: CreatorBindingAcquisition,
    completion_ready: Callable[[], bool],
    finalize_action: FinalizeAction,
    capability_license_delivery: CapabilityLicenseDelivery | None = None,
    provisioning_progress: Callable[[], dict[str, str | None]] | None = None,
    extension_id: str | None = None,
    hosted_onboarding_url: str = "",
    launcher_handoff_token: str | None = None,
    completion_exit: Callable[[], None] | None = None,
    session_manager: ProvisioningSessionManager | None = None,
    shutdown_action: Callable[[], Awaitable[None]] | None = None,
    initial_enrollment: InitialInstallationAdmission | None = None,
    onboarding_snapshot: Callable[[str], dict[str, object]] | None = None,
    onboarding_expiry: Callable[[], float | None] | None = None,
) -> FastAPI:
    sessions = session_manager or ProvisioningSessionManager(launcher_handoff_token)
    from app.provisioning.events import events, CAPABILITIES

    @asynccontextmanager
    async def lifecycle(application):
        try:
            yield
        finally:
            if shutdown_action is not None:
                await shutdown_action()

    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifecycle)

    if initial_enrollment is not None and hasattr(initial_enrollment, "store"):
        from app.provisioning.setup_transfer import DesktopSetupTransfer, install_routes
        install_routes(application, sessions, DesktopSetupTransfer(initial_enrollment))

    def provisioned_extension_id() -> str:
        if extension_id is None or _EXTENSION_ID_PATTERN.fullmatch(extension_id) is None:
            return ""
        return extension_id

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(PROVISIONING_HANDOFF_PATH, include_in_schema=False)
    async def issue_handoff(authorization: str | None = Header(default=None),
                            x_onboarding_journey: str | None = Header(default=None),
                            x_onboarding_native_entry: str | None = Header(default=None),
                            x_onboarding_reopen: str | None = Header(default=None)) -> JSONResponse:
        sessions.require_launcher(authorization)
        if x_onboarding_native_entry is not None:
            if (x_onboarding_native_entry not in {"discover", "targeted"}
                    or (x_onboarding_native_entry == "discover" and x_onboarding_journey is not None)
                    or (x_onboarding_native_entry == "targeted" and x_onboarding_journey is None)):
                raise HTTPException(400, "Native entry is invalid")
            focused = events.request_focus(x_onboarding_journey)
            if focused is not None:
                return JSONResponse({"workspace_active": True, "journey_id": focused}, headers={"Cache-Control": "no-store"})
            return JSONResponse({"handoff_code": sessions.issue_native_entry(authorization, journey_id=x_onboarding_journey)}, headers={"Cache-Control": "no-store"})
        try:
            workspace = sessions.launcher_workspace(x_onboarding_journey)
        except ValueError:
            raise HTTPException(400, "Journey is invalid") from None
        if workspace is not None:
            x_onboarding_journey = workspace["journey_id"]
            if events.request_focus(x_onboarding_journey) is not None:
                return JSONResponse({"workspace_active": True, "journey_id": x_onboarding_journey}, headers={"Cache-Control": "no-store"})
            if workspace["state"] in {"preparing", "prepare-unknown", "waiting", "completing", "unknown"} and x_onboarding_reopen != "explicit":
                return JSONResponse({"workspace_uncertain": True, "journey_id": x_onboarding_journey}, headers={"Cache-Control": "no-store"})
        code = sessions.issue_handoff_code(authorization, journey_id=x_onboarding_journey)
        response = JSONResponse({"handoff_code": code})
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get(PROVISIONING_REDEEM_PATH, include_in_schema=False)
    async def redeem_handoff(code: str) -> RedirectResponse:
        session = sessions.redeem_handoff_code(code)
        response = RedirectResponse("/provisioning" + ("#journey=" + session.journey_id if session.journey_id else ""), status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.set_cookie(
            PROVISIONING_SESSION_COOKIE_NAME,
            session.identifier,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return response

    @application.get("/provisioning/native-return", include_in_schema=False)
    async def native_return(request: Request) -> HTMLResponse:
        from app.provisioning.native_return import native_return_shell
        return native_return_shell(request, provisioned_extension_id())

    @application.get("/provisioning/native-return.js", include_in_schema=False)
    @application.get("/provisioning/native-json.mjs", include_in_schema=False)
    @application.get("/provisioning/native-workspace.mjs", include_in_schema=False)
    async def native_script(request: Request) -> Response:
        from app.provisioning.native_return import native_return_script
        return native_return_script(request)

    @application.get("/provisioning/native-entry", include_in_schema=False)
    async def redeem_native_entry(request: Request, code: str) -> RedirectResponse:
        sessions._require_exact_host(request)
        if set(request.query_params) != {"code"} or len(request.query_params.getlist("code")) != 1:
            raise HTTPException(400, "Native entry is invalid")
        entry = sessions.redeem_native_entry(code)
        response = RedirectResponse("/provisioning/native-return" + ("#journey=" + entry.journey_id if entry.journey_id else ""), status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(NATIVE_ENTRY_COOKIE_NAME, entry.identifier,
            httponly=True, secure=True, samesite="strict", path="/")
        return response

    @application.get("/api/v1/provisioning/native-entry", include_in_schema=False)
    async def native_entry_context(request: Request) -> JSONResponse:
        return JSONResponse(sessions.native_entry_context(request), headers={"Cache-Control": "no-store"})

    @application.post("/api/v1/provisioning/native-entry", include_in_schema=False)
    async def select_native_workspace(request: Request, body: NativeWorkspaceBody | NativeWorkspaceRecoveryBody) -> JSONResponse:
        recover = isinstance(body, NativeWorkspaceRecoveryBody)
        session = sessions.select_native_workspace(request, body.journey_id, recover=recover)
        response = JSONResponse({"state": "selected", "journey_id": session.journey_id,
            **({"previous_journey_id": body.journey_id} if recover else {})},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.set_cookie(PROVISIONING_SESSION_COOKIE_NAME, session.identifier,
            httponly=True, secure=True, samesite="strict", path="/")
        return response

    @application.get("/provisioning", include_in_schema=False)
    async def shell(request: Request) -> HTMLResponse:
        try:
            session = sessions.require_session(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            from app.provisioning.resume import resume_shell
            return resume_shell()
        document = _SHELL_TEMPLATE.read_text(encoding="utf-8")
        document = document.replace(
            "{{PROVISIONING_CSRF}}", html.escape(session.csrf_token, quote=True)
        ).replace(
            "{{PROVISIONING_EXTENSION_ID}}",
            html.escape(provisioned_extension_id(), quote=True),
        ).replace(
            "{{HOSTED_ONBOARDING_URL}}",
            html.escape(hosted_onboarding_url, quote=True),
        ).replace(
            "{{HOSTED_ONBOARDING_VISIBILITY}}",
            "" if hosted_onboarding_url else "hidden",
        )
        return HTMLResponse(document, headers={"Cache-Control": "no-store"})

    @application.get("/provisioning/resume.js", include_in_schema=False)
    async def browser_resume_script(request: Request) -> Response:
        from app.provisioning.resume import resume_script
        return resume_script(request)

    @application.get(PROVISIONING_DISCLOSURE_PATH, include_in_schema=False)
    async def disclosure(request: Request) -> HTMLResponse:
        sessions.require_session(request)
        return HTMLResponse(
            _DISCLOSURE_ASSET.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @application.get(PROVISIONING_SCRIPT_PATH, include_in_schema=False)
    async def script(request: Request) -> Response:
        sessions.require_session(request)
        return Response(
            _SCRIPT_ASSET.read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/provisioning/onboarding/{name}.mjs", include_in_schema=False)
    async def onboarding_module(request: Request, name: str):
        sessions.require_session(request)
        if name not in {"client", "projection", "json", "sse"}:
            raise HTTPException(404, "Not found")
        from app.core.resource_paths import resource_path
        return Response(resource_path("shared/onboarding/" + name + ".mjs").read_bytes(),
                        media_type="application/javascript", headers={"Cache-Control": "no-store"})

    def request_completion_exit() -> None:
        application.state.completion_requested = True
        events.publish()
        if completion_exit is not None:
            completion_exit()

    @application.get("/api/v1/provisioning/events", include_in_schema=False)
    async def onboarding_events(request: Request):
        session = sessions.require_session(request)
        if onboarding_snapshot is None or session.journey_id is None:
            return JSONResponse({"reason": "capability_unavailable"}, status_code=409)
        if initial_enrollment is not None:
            initial_enrollment.resume(session.journey_id)
        def next_expiry():
            remaining = sessions.session_remaining(request)
            authority = None if onboarding_expiry is None else onboarding_expiry()
            return remaining if authority is None else min(remaining, authority)
        return StreamingResponse(events.stream(
            lambda: onboarding_snapshot(session.journey_id), lambda: sessions.require_session(request),
            journey_id=session.journey_id, expires_in=next_expiry),
            media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
            "X-Onboarding-Capabilities": CAPABILITIES})

    @application.get("/api/v1/provisioning/state", include_in_schema=False)
    async def onboarding_state(request: Request):
        session = sessions.require_session(request)
        if onboarding_snapshot is None or session.journey_id is None:
            return JSONResponse({"reason": "capability_unavailable"}, status_code=409)
        return JSONResponse(onboarding_snapshot(session.journey_id), headers={"Cache-Control": "no-store",
                            "X-Onboarding-Capabilities": CAPABILITIES})

    @application.get("/api/v1/provisioning/initial-handoff", include_in_schema=False)
    async def read_initial_handoff(request: Request):
        session = sessions.require_session(request)
        if initial_enrollment is None or session.journey_id is None:
            return JSONResponse({"reason": "capability_unavailable"}, status_code=409)
        csrf_values = request.headers.getlist("X-Provisioning-CSRF")
        origin_values = request.headers.getlist("Origin")
        if (request.query_params or request.headers.getlist("X-Onboarding-Journey") != [session.journey_id]
                or len(csrf_values) != 1 or re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf_values[0]) is None
                or not secrets.compare_digest(csrf_values[0], session.csrf_token)
                or (origin_values and origin_values != [PROVISIONING_ORIGIN])):
            raise HTTPException(403, "provisioning context is invalid")
        result = await initial_enrollment.read_browser_entry(session.journey_id)
        # Recheck after the owner lock; waiting must not extend session authority.
        current = sessions.require_session(request)
        if (current.journey_id != session.journey_id
                or not secrets.compare_digest(current.csrf_token, session.csrf_token)):
            raise HTTPException(403, "provisioning context is invalid")
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @application.post("/api/v1/provisioning/initial-handoff", include_in_schema=False)
    async def prepare_initial_handoff(request: Request, body: CreatorBindingAcquisitionBody):
        del body
        session = sessions.require_mutation(request)
        if initial_enrollment is None or session.journey_id is None:
            return JSONResponse({"reason": "capability_unavailable"}, status_code=409)
        from app.security.initial_handoff import InitialHandoffRefused
        from app.security.hosted_grants import HostedGrantUnavailable
        try:
            result = await initial_enrollment.prepare(session.journey_id)
        except InitialHandoffRefused as error:
            return JSONResponse({"state": "unconfirmed", "reason": error.code}, status_code=409,
                                headers={"Cache-Control": "no-store"})
        except HostedGrantUnavailable:
            return JSONResponse({"state": "unconfirmed"}, status_code=503, headers={"Cache-Control": "no-store"})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @application.get(PROVISIONING_STATUS_PATH, include_in_schema=False)
    async def status(request: Request) -> JSONResponse:
        sessions.require_session(request)
        if completion_ready():
            return JSONResponse(
                {"state": "configured_restart"},
                background=BackgroundTask(request_completion_exit),
            )
        if provisioning_progress is None:
            return JSONResponse({"state": "provisioning_ready"})
        progress = _validated_progress(provisioning_progress())
        return JSONResponse(
            {"state": "provisioning_ready", **progress},
            headers={"Cache-Control": "no-store"},
        )

    @application.post(PROVISIONING_CLAIM_PATH, include_in_schema=False)
    async def submit_claim(request: Request, body: ClaimSubmissionBody) -> JSONResponse:
        sessions.require_mutation(request)
        refusal = claim_submission(package=body.package.strip())
        events.publish()
        if refusal is not None:
            return JSONResponse(
                {"state": "provisioning_ready", "reason": refusal},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"state": "installation_registered"}, headers={"Cache-Control": "no-store"}
        )

    if capability_license_delivery is not None:
        async def capability_license_response(
            request: Request,
            body: CapabilityLicenseDeliveryBody,
            *,
            reissue: bool,
        ) -> JSONResponse:
            sessions.require_mutation(request)
            try:
                result = await asyncio.wait_for(
                    run_in_threadpool(
                        capability_license_delivery.finalize_reissue
                        if reissue else capability_license_delivery.activate,
                        package=body.package.strip(),
                        seat_id=body.seat_id,
                    ),
                    timeout=30.0,
                )
            except TimeoutError:
                result = "hosted_unavailable"
            if isinstance(result, str):
                status_code = 503 if result in {
                    "hosted_origin_unavailable", "hosted_unavailable"
                } else 409
                return JSONResponse(
                    {"state": "provisioning_ready", "reason": result},
                    status_code=status_code,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {
                    "state": "capability_license_active",
                    "reference_id": result.reference_id,
                    "license_id": result.license_id,
                    "issuance_id": result.issuance_id,
                },
                headers={"Cache-Control": "no-store"},
            )

        @application.post(PROVISIONING_CAPABILITY_LICENSE_ACTIVATE_PATH, include_in_schema=False)
        async def activate_capability_license(
            request: Request, body: CapabilityLicenseDeliveryBody
        ) -> JSONResponse:
            return await capability_license_response(request, body, reissue=False)

        @application.post(PROVISIONING_CAPABILITY_LICENSE_REISSUE_PATH, include_in_schema=False)
        async def finalize_capability_license_reissue(
            request: Request, body: CapabilityLicenseDeliveryBody
        ) -> JSONResponse:
            return await capability_license_response(request, body, reissue=True)

    @application.post(PROVISIONING_CREATOR_ASSOCIATION_PATH, include_in_schema=False)
    async def initiate_creator_association(
        request: Request, body: CreatorAssociationBody
    ) -> JSONResponse:
        sessions.require_mutation(request)
        outcome = creator_association_initiation(
            detected_creator_account_id=body.detected_creator_account_id,
            onboarding_transaction_id=body.onboarding_transaction_id,
            organization_id=body.organization_id,
            installation_id=body.installation_id,
        )
        if isinstance(outcome, str):
            return JSONResponse(
                {"state": "provisioning_ready", "reason": outcome},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "association_request_id": outcome.association_request_id,
                "status": outcome.status,
                "updated_at": outcome.updated_at,
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.post(PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH, include_in_schema=False)
    async def acquire_creator_binding(
        request: Request, body: CreatorBindingAcquisitionBody
    ) -> JSONResponse:
        del body
        sessions.require_mutation(request)
        outcome = creator_binding_acquisition()
        events.publish()
        if isinstance(outcome, str):
            return JSONResponse(
                {"state": "provisioning_ready", "reason": outcome},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "association_request_id": outcome.association_request_id,
                "status": outcome.status,
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.post(PROVISIONING_FINALIZE_PATH, include_in_schema=False)
    async def finalize(request: Request, body: FinalizationBody) -> JSONResponse:
        sessions.require_mutation(request)
        try:
            refusal = await asyncio.wait_for(run_in_threadpool(
                finalize_action,
                association_request_id=body.association_request_id,
                detected_creator_account_id=body.detected_creator_account_id,
                reported_platform_creator_id=body.reported_platform_creator_id,
            ), timeout=30.0)
        except TimeoutError:
            refusal = "membership_refresh_unavailable"
        if refusal is not None:
            return JSONResponse(
                {"state": "provisioning_ready", "reason": refusal},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"state": "configured_restart"},
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(request_completion_exit),
        )

    @application.post("/api/v1/provisioning/retry", include_in_schema=False)
    async def retry(request: Request) -> JSONResponse:
        sessions.require_mutation(request)
        return JSONResponse({"state": "provisioning_ready"}, status_code=409)

    return application
