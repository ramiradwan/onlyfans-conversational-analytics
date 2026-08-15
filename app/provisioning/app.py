"""ASGI application available before runtime configuration exists."""

from __future__ import annotations

from typing import Annotated, Callable, Protocol

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.background import BackgroundTask

from app.provisioning.session import (
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
)


PROVISIONING_HANDOFF_PATH = "/api/v1/provisioning/handoff"
PROVISIONING_REDEEM_PATH = "/provisioning/handoff"
PROVISIONING_STATUS_PATH = "/api/v1/provisioning/status"
PROVISIONING_CLAIM_PATH = "/api/v1/provisioning/claim"
PROVISIONING_FINALIZE_PATH = "/api/v1/provisioning/finalize"

BoundedIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=200)]

# Transport bound for the pasted field, stated here because this module imports
# no runtime code. It is deliberately looser than the claim package's own
# maximum so that an oversized package is refused by the decoder, which reports
# a nonsecret reason, rather than by request validation.
BoundedClaimPackage = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class ClaimSubmissionBody(BaseModel):
    """Bounded provisioning-page body carrying one pasted claim package."""

    model_config = ConfigDict(extra="forbid")

    package: BoundedClaimPackage


class ClaimSubmission(Protocol):
    """Seam to claim consumption: `None` on success, a nonsecret reason otherwise."""

    def __call__(self, *, package: str) -> str | None: ...


class FinalizationBody(BaseModel):
    """Bounded provisioning-page body addressing one approved association."""

    model_config = ConfigDict(extra="forbid")

    association_request_id: BoundedIdentifier
    detected_creator_account_id: BoundedIdentifier
    reported_platform_creator_id: BoundedIdentifier | None = None


class FinalizeAction(Protocol):
    """Seam to finalization: `None` on success, a nonsecret reason on refusal."""

    def __call__(
        self,
        *,
        association_request_id: str,
        detected_creator_account_id: str,
        reported_platform_creator_id: str | None,
    ) -> str | None: ...


def create_provisioning_app(
    *,
    claim_submission: ClaimSubmission,
    completion_ready: Callable[[], bool],
    finalize_action: FinalizeAction,
    launcher_handoff_token: str | None = None,
    completion_exit: Callable[[], None] | None = None,
    session_manager: ProvisioningSessionManager | None = None,
) -> FastAPI:
    """Build the isolated provisioning surface without importing runtime modules."""
    sessions = session_manager or ProvisioningSessionManager(launcher_handoff_token)
    application = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

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
        return HTMLResponse(
            "<main data-provisioning-csrf=\"%s\">Provisioning</main>"
            % session.csrf_token,
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
        return JSONResponse({"state": "provisioning_ready"})

    @application.post(PROVISIONING_CLAIM_PATH, include_in_schema=False)
    async def submit_claim(request: Request, body: ClaimSubmissionBody) -> JSONResponse:
        sessions.require_mutation(request)
        # Surrounding whitespace is transport, not package: a pasted field can
        # carry it and it cannot change the decoded object. Trimming here keeps
        # the decoder byte-strict and gives every client the same entry point.
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

    @application.post(PROVISIONING_FINALIZE_PATH, include_in_schema=False)
    async def finalize(request: Request, body: FinalizationBody) -> JSONResponse:
        sessions.require_mutation(request)
        refusal = finalize_action(
            association_request_id=body.association_request_id,
            detected_creator_account_id=body.detected_creator_account_id,
            reported_platform_creator_id=body.reported_platform_creator_id,
        )
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
