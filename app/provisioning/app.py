"""ASGI application available before runtime configuration exists."""

from __future__ import annotations

import asyncio
import html
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Protocol

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.provisioning.session import (
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
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
    launcher_handoff_token: str | None = None,
    completion_exit: Callable[[], None] | None = None,
    session_manager: ProvisioningSessionManager | None = None,
    shutdown_action: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    sessions = session_manager or ProvisioningSessionManager(launcher_handoff_token)

    @asynccontextmanager
    async def lifecycle(application):
        try:
            yield
        finally:
            if shutdown_action is not None:
                await shutdown_action()

    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifecycle)

    def provisioned_extension_id() -> str:
        if extension_id is None or _EXTENSION_ID_PATTERN.fullmatch(extension_id) is None:
            return ""
        return extension_id

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(PROVISIONING_HANDOFF_PATH, include_in_schema=False)
    async def issue_handoff(authorization: str | None = Header(default=None)) -> JSONResponse:
        code = sessions.issue_handoff_code(authorization)
        response = JSONResponse({"handoff_code": code})
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get(PROVISIONING_REDEEM_PATH, include_in_schema=False)
    async def redeem_handoff(code: str) -> RedirectResponse:
        session = sessions.redeem_handoff_code(code)
        response = RedirectResponse("/provisioning", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            PROVISIONING_SESSION_COOKIE_NAME,
            session.identifier,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return response

    @application.get("/provisioning", include_in_schema=False)
    async def shell(request: Request) -> HTMLResponse:
        session = sessions.require_session(request)
        document = _SHELL_TEMPLATE.read_text(encoding="utf-8")
        document = document.replace(
            "{{PROVISIONING_CSRF}}", html.escape(session.csrf_token, quote=True)
        ).replace(
            "{{PROVISIONING_EXTENSION_ID}}",
            html.escape(provisioned_extension_id(), quote=True),
        )
        return HTMLResponse(document, headers={"Cache-Control": "no-store"})

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

    def request_completion_exit() -> None:
        application.state.completion_requested = True
        if completion_exit is not None:
            completion_exit()

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
            {"state": "configured_restart"}, headers={"Cache-Control": "no-store"}
        )

    @application.post("/api/v1/provisioning/retry", include_in_schema=False)
    async def retry(request: Request) -> JSONResponse:
        sessions.require_mutation(request)
        return JSONResponse({"state": "provisioning_ready"}, status_code=409)

    return application
