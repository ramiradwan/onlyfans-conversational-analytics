"""Fixed-origin HTTP adapters for desktop and narrowly scoped browser admissions."""
from __future__ import annotations

import secrets
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.onboarding.admission import AdmissionRefused, ScopedAdmissions, csrf_token

ORIGIN = "http://bridge.localhost:17871"
HOST = "bridge.localhost:17871"
CSRF = "X-Desktop-CSRF"
GENERATION = "X-Desktop-Generation"
COOKIE_NAMES = {"desktop": "__Host-desktop_presentation", "browser_task": "__Host-browser_task"}
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff"}
ASSETS = Path(__file__).parent


class EntryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class DesktopEntryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    journey_id: str | None = None


class DesktopCurrentBody(DesktopEntryBody):
    generation: str


class DesktopRefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    generation: str


def _host(request):
    if request.headers.getlist("host") != [HOST]:
        raise HTTPException(421, "Local application host is required")


def _browser(request, *, mutation=False):
    _host(request)
    if (request.headers.getlist("origin") not in ([ORIGIN], [] if not mutation else [ORIGIN])
            or request.headers.getlist("sec-fetch-site") not in ([], ["same-origin"], ["none"])):
        raise HTTPException(403, "Local application origin is required")


def authorize(admissions, request, *, role, mutation=False):
    _browser(request, mutation=mutation)
    cookies = [part.strip().partition("=")[2] for line in request.headers.getlist("cookie")
        for part in line.split(";") if part.strip().partition("=")[0] == COOKIE_NAMES[role]]
    if len(cookies) != 1:
        raise HTTPException(401, "Setup admission is unavailable")
    identifiers = cookies[0]
    generations = request.headers.getlist(GENERATION)
    if len(generations) != 1:
        raise HTTPException(409, "Setup presentation changed")
    try:
        session = admissions.require(identifiers, role=role, generation=generations[0])
    except AdmissionRefused:
        raise HTTPException(401, "Setup admission is unavailable") from None
    if mutation:
        values = request.headers.getlist(CSRF)
        if len(values) != 1 or not secrets.compare_digest(values[0].encode(), csrf_token(session.identifier).encode()):
            raise HTTPException(403, "Setup request context is invalid")
    return session


def _public_session(session):
    return {key: value for key, value in asdict(session).items() if key != "identifier"} | {
        "csrf_token": csrf_token(session.identifier)}


def admission_router(admissions: ScopedAdmissions, *, launcher_token: str) -> APIRouter:
    if len(launcher_token) < 32:
        raise ValueError("Desktop launcher token is invalid")
    router = APIRouter()

    def launcher(request):
        _host(request)
        headers = request.headers.getlist("authorization")
        if (request.url.query or len(headers) != 1
                or not secrets.compare_digest(headers[0].encode(), ("Desktop " + launcher_token).encode())
                or request.headers.getlist("origin") or request.headers.getlist("cookie")):
            raise HTTPException(401, "Desktop launcher admission is required")

    @router.post("/api/v1/desktop/presentation/current", include_in_schema=False)
    def current_presentation(request: Request, body: DesktopCurrentBody):
        launcher(request)
        try:
            current = admissions.presentation_current(body.generation, body.journey_id)
        except ValueError:
            current = False
        return JSONResponse({"current": current}, headers=HEADERS)

    @router.post("/api/v1/desktop/entry", include_in_schema=False)
    def prepare_desktop(request: Request, body: DesktopEntryBody):
        launcher(request)
        try:
            code = admissions.prepare_desktop(body.journey_id)
        except (AdmissionRefused, ValueError):
            raise HTTPException(409, "Setup entry is unavailable") from None
        return JSONResponse({"entry_code": code}, headers=HEADERS)

    @router.post("/api/v1/desktop/presentation/refresh", include_in_schema=False)
    def refresh_desktop(request: Request, body: DesktopRefreshBody):
        launcher(request)
        try:
            code = admissions.prepare_desktop(replacing_generation=body.generation)
        except (AdmissionRefused, ValueError):
            raise HTTPException(409, "Setup presentation changed") from None
        return JSONResponse({"entry_code": code}, headers=HEADERS)

    @router.get("/onboarding/entry", include_in_schema=False)
    @router.get("/browser-tasks/entry", include_in_schema=False)
    def landing(request: Request):
        _host(request)
        if request.url.query:
            raise HTTPException(400, "Entry credentials belong in the private link fragment")
        return HTMLResponse((ASSETS / "entry.html").read_text(encoding="utf-8"), headers=HEADERS | {
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})

    @router.get("/onboarding/entry.css", include_in_schema=False)
    def landing_stylesheet(request: Request):
        _host(request)
        return Response((ASSETS / "entry.css").read_text(encoding="utf-8"), media_type="text/css", headers=HEADERS)

    @router.get("/onboarding/entry.js", include_in_schema=False)
    def landing_script(request: Request):
        _host(request)
        return Response((ASSETS / "entry.js").read_text(encoding="utf-8"), media_type="text/javascript", headers=HEADERS)

    def consume(request, body, role):
        _browser(request, mutation=True)
        try:
            session = admissions.consume(body.code, role=role)
        except AdmissionRefused:
            raise HTTPException(401, "Setup entry expired or was already used") from None
        response = JSONResponse(_public_session(session), headers=HEADERS)
        response.set_cookie(COOKIE_NAMES[role], session.identifier, path="/", secure=True, httponly=True,
                            samesite="strict", max_age=max(0, int(session.expires_at - admissions._now())))
        return response

    @router.post("/api/v1/desktop/entry/consume", include_in_schema=False)
    def consume_desktop(request: Request, body: EntryBody):
        return consume(request, body, "desktop")

    @router.post("/api/v1/browser-tasks/entry/consume", include_in_schema=False)
    def consume_browser(request: Request, body: EntryBody):
        return consume(request, body, "browser_task")

    def read_session(request, role):
        session = authorize(admissions, request, role=role)
        return JSONResponse(_public_session(session), headers=HEADERS)

    @router.get("/api/v1/desktop/session", include_in_schema=False)
    def desktop_session(request: Request):
        return read_session(request, "desktop")

    @router.get("/api/v1/browser-tasks/session", include_in_schema=False)
    def browser_session(request: Request):
        return read_session(request, "browser_task")

    @router.get("/api/v1/browser-tasks/current", include_in_schema=False)
    def current_task(request: Request):
        session = authorize(admissions, request, role="browser_task")
        try:
            return JSONResponse(admissions.task(session.identifier, generation=session.generation), headers=HEADERS)
        except AdmissionRefused:
            raise HTTPException(409, "Browser task changed") from None

    @router.post("/api/v1/desktop/browser-tasks/{task_id}/entry", include_in_schema=False)
    def reissue_task(request: Request, task_id: str):
        session = authorize(admissions, request, role="desktop", mutation=True)
        try:
            code = admissions.reissue_task_entry(session.identifier, generation=session.generation, task_id=task_id)
        except (AdmissionRefused, ValueError):
            raise HTTPException(409, "Browser task is unavailable") from None
        return JSONResponse({"entry_code": code}, headers=HEADERS)

    return router
