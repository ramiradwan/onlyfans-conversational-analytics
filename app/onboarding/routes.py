"""Scoped desktop HTTP and invalidation adapters shared by both boot graphs."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, authorize
from app.onboarding.coordinator import CommandRefused, DesktopOnboardingCoordinator
from app.provisioning.events import events


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("Duplicate field")
        value[name] = item
    return value


def _invalid_number(value):
    raise ValueError("Invalid number")


async def body(request: Request, *, empty=False):
    if request.headers.getlist("content-type") != ["application/json"] or request.url.query:
        raise HTTPException(400, "Invalid setup request")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16_384:
            raise HTTPException(413, "Setup request exceeds its limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_number)
        if not isinstance(value, dict) or (empty and value != {}):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise HTTPException(400, "Invalid setup request") from None


def _refused(error):
    if isinstance(error, AdmissionRefused):
        raise HTTPException(401, "Setup admission is unavailable") from None
    if isinstance(error, CommandRefused):
        return JSONResponse({"reason": error.reason}, status_code=409, headers=HEADERS)
    raise HTTPException(400, "Invalid setup request") from None


class DesktopEventResponse(StreamingResponse):
    """The response owns its subscription, even if no body is ever consumed."""
    def __init__(self, coordinator, session, *, actor, active, deadline):
        self.coordinator, self.session = coordinator, session
        self.actor, self.active, self.deadline = actor, active, deadline
        super().__init__(self.frames(), media_type="text/event-stream", headers=HEADERS | {"X-Accel-Buffering": "no"})
        self.listener = None

    async def __call__(self, scope, receive, send):
        if self.session.generation in self.active or len(self.active) >= 8:
            await JSONResponse({"reason": "stream_limit_reached"}, status_code=429, headers=HEADERS)(scope, receive, send)
            return
        self.active.add(self.session.generation)
        self.listener = events.subscribe(self.session.journey_id)
        try:
            await super().__call__(scope, receive, send)
        finally:
            events.unsubscribe(self.listener)
            self.active.discard(self.session.generation)

    async def frames(self):
        signal = self.listener[1]
        previous = None
        try:
            while True:
                # Clear before the authoritative read. A commit while reading or
                # sending the frame remains dirty for the following iteration.
                signal.clear()
                snapshot = self.coordinator.read(self.session, actor=self.actor())
                encoded = json.dumps(snapshot, separators=(",", ":"))
                if encoded != previous:
                    yield "event: setup\ndata: " + encoded + "\n\n"
                    previous = encoded
                remaining = min(self.session.expires_at - self.coordinator.admissions._now(), self.deadline())
                if remaining <= 0:
                    return
                try:
                    await asyncio.wait_for(signal.wait(), min(15, remaining))
                except TimeoutError:
                    yield ": keepalive\n\n"
        except (AdmissionRefused, CommandRefused, HTTPException):
            return


def desktop_router(coordinator: DesktopOnboardingCoordinator, *, actor=lambda request, mutation: None,
                   next_expiry=lambda: 1800.0) -> APIRouter:
    router = APIRouter(prefix="/api/v1/desktop/setup")
    active_streams = set()

    def admitted(request, *, mutation=False):
        if request.url.query:
            raise HTTPException(400, "Invalid setup request")
        return authorize(coordinator.admissions, request, role="desktop", mutation=mutation)

    @router.get("")
    def snapshot(request: Request):
        session = admitted(request)
        try:
            return JSONResponse(coordinator.read(session, actor=actor(request, False)), headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.post("/commands")
    async def command(request: Request):
        session = admitted(request, mutation=True)
        value = await body(request)
        try:
            result = await coordinator.command(session, value, actor=actor(request, True))
            return JSONResponse(result, status_code=202, headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.get("/operations/{operation_id}")
    def receipt(request: Request, operation_id: str):
        session = admitted(request)
        actor(request, False)
        try:
            return JSONResponse(coordinator.receipt(session, operation_id), headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.post("/operations/{operation_id}/reconcile")
    async def reconcile(request: Request, operation_id: str):
        session = admitted(request, mutation=True)
        await body(request, empty=True)
        try:
            result = await coordinator.reconcile(session, operation_id, actor=actor(request, True))
            return JSONResponse(result, status_code=202, headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.get("/events")
    def stream(request: Request):
        session = admitted(request)
        actor(request, False)
        return DesktopEventResponse(coordinator, session, actor=lambda: actor(request, False),
                                    active=active_streams, deadline=next_expiry)

    return router
