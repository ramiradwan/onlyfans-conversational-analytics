"""Scoped desktop HTTP and invalidation adapters shared by both boot graphs."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from app.onboarding import timing
from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, authorize
from app.onboarding.contract import DOCUMENT_UTF8_BYTES, SETUP_FRAME_UTF8_BYTES
from app.onboarding.coordinator import CommandRefused, DesktopOnboardingCoordinator
from app.provisioning.events import events
from app.security.account_passkey import AccountPasskeyRefused

# A view offering one of these can start a sign-in from its next click.
SIGN_IN_ACTIONS = frozenset({"account.connect", "auth.select"})
SCOPED_SETUP_EVENT = "setup-frame.v1"
SETUP_EVENT_PROFILE = "X-Desktop-Setup-Events"


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
        if len(raw) + len(chunk) > DOCUMENT_UTF8_BYTES:
            raise HTTPException(413, "Setup request exceeds its limit")
        raw.extend(chunk)
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
    def __init__(self, coordinator, session, *, actor, active, deadline, notices=None, warm=None, scoped=False):
        self.coordinator, self.session = coordinator, session
        self.actor, self.active, self.deadline = actor, active, deadline
        self.notices, self.warm, self.scoped = notices, warm, scoped
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

    def _projection(self, actor):
        if not self.scoped:
            return self.coordinator.read(self.session, actor=actor), None
        # The same read connection resolves the snapshot and its current
        # notice scopes. A change while reading keeps the stream invalidated.
        with self.coordinator.store.database.shared_reads():
            snapshot = self.coordinator.read(self.session, actor=actor)
            notices = [] if self.notices is None else self.notices(self.session, scoped=True)
        pending = {item["operation_id"] for item in snapshot["pending_operations"]}
        return snapshot, [notice for notice in notices if notice["operation_id"] in pending]

    async def frames(self):
        signal = self.listener[1]
        previous = None
        shown = json.dumps({"notices": []}, separators=(",", ":"))
        try:
            while not self.coordinator._closed:
                # Clear before the authoritative read. A commit while reading or
                # sending the frame remains dirty for the following iteration.
                signal.clear()
                actor = self.actor()
                # The read runs off the event loop so sign-in work is not held behind it.
                with timing.phase("snapshot", "read", minimum_ms=timing.SLOW_MS):
                    snapshot, scoped_notices = await asyncio.to_thread(self._projection, actor)
                if self.warm is not None and SIGN_IN_ACTIONS.intersection(snapshot.get("allowed_actions", ())):
                    # Only a connection is opened. Every sign-in write still starts from its click.
                    self.warm()
                event = SCOPED_SETUP_EVENT if self.scoped else "setup"
                projection = {"snapshot": snapshot, "browser_notices": scoped_notices} if self.scoped else snapshot
                encoded = json.dumps(projection, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                if self.scoped and len(encoded.encode("utf-8")) > SETUP_FRAME_UTF8_BYTES:
                    return
                if encoded != previous:
                    yield f"event: {event}\ndata: {encoded}\n\n"
                    previous = encoded
                if self.notices is not None and not self.scoped:
                    # Display-only helper notices. Older pages ignore this event.
                    notices = json.dumps({"notices": self.notices(self.session)}, separators=(",", ":"))
                    if notices != shown:
                        yield "event: browser-notices\ndata: " + notices + "\n\n"
                        shown = notices
                remaining = min(self.session.expires_at - self.coordinator.admissions._now(), self.deadline())
                if remaining <= 0:
                    yield "event: setup-end\ndata: {}\n\n"
                    return
                timed_out = False
                try:
                    await asyncio.wait_for(signal.wait(), min(15, remaining))
                except TimeoutError:
                    timed_out = True
                remaining = min(self.session.expires_at - self.coordinator.admissions._now(), self.deadline())
                if remaining <= 0:
                    yield "event: setup-end\ndata: {}\n\n"
                    return
                if timed_out:
                    yield ": keepalive\n\n"
        except (AdmissionRefused, CommandRefused, HTTPException):
            return


def desktop_router(coordinator: DesktopOnboardingCoordinator, *, actor=lambda request, mutation: None,
                   next_expiry=lambda: 1800.0, notices=None, warm=None) -> APIRouter:
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
            with timing.phase("command", "run"):
                result = await coordinator.command(session, value, actor=actor(request, True))
            return JSONResponse(result, status_code=202, headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.post("/passkey")
    async def account_passkey(request: Request):
        session = admitted(request, mutation=True)
        expected = await body(request)
        try:
            outcome = await coordinator.create_account_passkey(session, expected, actor=actor(request, True))
            return JSONResponse({"outcome": outcome}, headers=HEADERS)
        except AccountPasskeyRefused as refusal:
            return JSONResponse({"outcome": "unavailable", "support_ref": refusal.support_ref}, headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.post("/passkey/{change}")
    async def passkey_pause_or_resume(request: Request, change: Literal["pause", "resume"]):
        session = admitted(request, mutation=True)
        expected = await body(request)
        try:
            outcome = await asyncio.to_thread(
                coordinator.set_passkey_paused, session, expected, paused=change == "pause", actor=actor(request, True),
            )
            return JSONResponse({"outcome": outcome}, headers=HEADERS)
        except (AdmissionRefused, CommandRefused, ValueError) as error:
            return _refused(error)

    @router.post("/timing")
    async def phase_observation(request: Request):
        admitted(request, mutation=True)
        value = await body(request)
        try:
            timing.observation(value, operation="frontend", phases={"click", "command-send", "task-found", "launch"})
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid sign-in timing") from None
        return Response(status_code=204, headers=HEADERS)

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
        profiles = request.headers.getlist(SETUP_EVENT_PROFILE)
        if profiles and profiles != [SCOPED_SETUP_EVENT]:
            raise HTTPException(400, "Invalid setup event profile")
        return DesktopEventResponse(coordinator, session, actor=lambda: actor(request, False),
                                    active=active_streams, deadline=next_expiry, notices=notices, warm=warm,
                                    scoped=bool(profiles))

    return router
