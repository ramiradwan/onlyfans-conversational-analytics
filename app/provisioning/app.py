"""ASGI application available before runtime configuration exists."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from app.provisioning.session import (
    PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
)


PROVISIONING_HANDOFF_PATH = "/api/v1/provisioning/handoff"
PROVISIONING_REDEEM_PATH = "/provisioning/handoff"
PROVISIONING_STATUS_PATH = "/api/v1/provisioning/status"


def coherent_grant_tuple_exists() -> bool:
    """Fail closed until the later durable grant resolver is installed."""
    return False


def create_provisioning_app(
    *,
    launcher_handoff_token: str | None = None,
    completion_ready: Callable[[], bool] = coherent_grant_tuple_exists,
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

    @application.post("/api/v1/provisioning/retry", include_in_schema=False)
    async def retry(request: Request) -> JSONResponse:
        sessions.require_mutation(request)
        return JSONResponse({"state": "provisioning_ready"}, status_code=409)

    return application
