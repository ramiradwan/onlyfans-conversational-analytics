"""Explicit receiving admission, subordinate to the current native presentation.

Browser credentials are bounded, transient and transfer-only. A backend restart
requires a new native entry; the transfer owner keeps the original durable intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import secrets
import re
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.onboarding.admission import AdmissionRefused, csrf_token
from app.onboarding.admission_routes import HEADERS, _browser, authorize
from app.onboarding.routes import body
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.persistence.continuation_selection import ContinuationSelectionUnavailable
from app.security.installation_key import InstallationKeyError

COOKIE = "__Host-setup_receiver"


@dataclass(frozen=True)
class ReceivingSession:
    identifier: str
    csrf_token: str
    journey_id: str
    generation: str
    expires_at: float
    workspace_journey_id: str


class ReceivingSessions:
    def __init__(self, coordinator, transfer):
        self.coordinator, self.transfer = coordinator, transfer
        self.store = transfer.store
        self._entries = {}
        self._sessions = {}
        self._lock = RLock()

    def _prune(self):
        now = self.store._now().timestamp()
        self._entries = {
            key: item for key, item in self._entries.items() if item[0] > now
        }
        self._sessions = {
            key: item for key, item in self._sessions.items() if item.expires_at > now
        }

    def issue(self, native):
        """Only a deliberate, admitted native action may create receiving entry."""
        owner = self.coordinator.owner
        with self._lock, self.store.database.transaction() as connection:
            self._prune()
            _, parent = self.coordinator.admissions._require(
                connection,
                native.identifier,
                role="desktop",
                generation=native.generation,
            )
            if parent["stage"] != "setup" or self.coordinator._closed:
                raise ValueError("Receiving is unavailable")
            if connection.execute(
                "SELECT 1 FROM desktop_setup_operations WHERE state IN ('accepted','waiting','outcome_unknown')"
            ).fetchone():
                raise ValueError("Setup action is pending")
            roots = connection.execute(
                "SELECT * FROM desktop_setup_journeys LIMIT 2"
            ).fetchall()
            if len(roots) > 1 or (
                roots and parent["journey_id"] not in (None, roots[0]["journey_id"])
            ):
                raise ValueError("Setup changed")
            workspace = roots[0]["journey_id"] if roots else str(uuid4())
            source = owner.contexts.admit_desktop_context(
                connection,
                journey_id=roots[0]["source_journey_id"] if roots else None,
                reopen_key=owner.key.reopen_existing,
            )
            journey = connection.execute(
                "SELECT * FROM onboarding_journeys WHERE journey_id=?", (source,)
            ).fetchone()
            if (
                journey["kind"] != "initial-enrollment"
                or journey["scope_json"] is not None
                or journey["state"] == "completed"
            ):
                raise ValueError("Initial setup is unavailable")
            now = self.store._now().timestamp()
            expires = min(
                parent["expires_at"],
                datetime.fromisoformat(journey["expires_at"]).timestamp(),
            )
            if expires <= now:
                raise ValueError("Setup expired")
            connection.execute(
                "INSERT OR IGNORE INTO desktop_setup_journeys (journey_id,created_at,source_journey_id) VALUES (?,?,?)",
                (workspace, now, source),
            )
            connection.execute(
                "UPDATE desktop_setup_journeys SET source_journey_id=? WHERE journey_id=?",
                (source, workspace),
            )
            connection.execute(
                "UPDATE desktop_presentations SET journey_id=? WHERE generation=?",
                (workspace, native.generation),
            )
            # One receiving page per presentation. Reopening retires every former
            # code and session before another browser profile can be admitted.
            self._entries.clear()
            self._sessions.clear()
            code = secrets.token_urlsafe(32)
            self._entries[code] = (
                min(now + 60, expires),
                native.generation,
                source,
                expires,
                workspace,
            )
            return code

    def _current(self, session):
        with self.store.database.read() as connection:
            parent = connection.execute(
                "SELECT * FROM desktop_presentations WHERE generation=?",
                (session.generation,),
            ).fetchone()
            selected = (
                None
                if parent is None
                else connection.execute(
                    "SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?",
                    (parent["journey_id"],),
                ).fetchone()
            )
            if (
                self.coordinator._closed
                or parent is None
                or parent["state"] != "active"
                or parent["stage"] != "setup"
                or parent["expires_at"] <= self.store._now().timestamp()
                or selected is None
                or parent["journey_id"] != session.workspace_journey_id
                or selected["source_journey_id"] != session.journey_id
            ):
                raise HTTPException(401, "Receiving session is unavailable")
        try:
            self.transfer.journeys.require_current(session.journey_id)
        except OnboardingJourneyUnavailable:
            raise HTTPException(401, "Receiving session expired") from None
        return session

    def consume(self, code):
        with self._lock:
            self._prune()
            item = self._entries.pop(code, None)
            if item is None:
                raise HTTPException(401, "Receiving entry expired or was already used")
            identifier = secrets.token_urlsafe(32)
            session = self._current(
                ReceivingSession(
                    identifier,
                    csrf_token(identifier),
                    item[2],
                    item[1],
                    item[3],
                    item[4],
                )
            )
            self._sessions[identifier] = session
            return session

    def require_session(self, request):
        _browser(request)
        if request.url.query:
            raise HTTPException(400, "Invalid receiving request")
        values = [
            part.strip().partition("=")[2]
            for line in request.headers.getlist("cookie")
            for part in line.split(";")
            if part.strip().partition("=")[0] == COOKIE
        ]
        with self._lock:
            self._prune()
            session = self._sessions.get(values[0]) if len(values) == 1 else None
            if session is None:
                raise HTTPException(401, "Receiving session is unavailable")
            if request.headers.getlist("x-onboarding-journey") not in (
                [],
                [session.journey_id],
            ):
                raise HTTPException(409, "Receiving journey changed")
            try:
                return self._current(session)
            except OnboardingJourneyUnavailable:
                raise HTTPException(401, "Receiving session expired") from None

    def require_mutation(self, request):
        _browser(request, mutation=True)
        session = self.require_session(request)
        if request.headers.getlist("x-csrf-token") != [session.csrf_token]:
            raise HTTPException(403, "Receiving request context is invalid")
        return session

    def require_initial_context(self, session):
        row = self.transfer.journeys.require_current(session.journey_id)
        if row["kind"] != "initial-enrollment":
            raise HTTPException(409, "Initial setup is unavailable")


def install_receiving(application, coordinator, transfer):
    from app.provisioning.setup_transfer import install_routes

    sessions = ReceivingSessions(coordinator, transfer)
    application.state.receiving_sessions = sessions
    install_routes(application, sessions, transfer)

    @application.post("/api/v1/desktop/setup-transfer/entry", include_in_schema=False)
    async def prepare_entry(request: Request):
        native = authorize(
            coordinator.admissions, request, role="desktop", mutation=True
        )
        await body(request, empty=True)
        try:
            code = sessions.issue(native)
        except (
            ValueError,
            AdmissionRefused,
            OnboardingJourneyUnavailable,
            ContinuationSelectionUnavailable,
            InstallationKeyError,
        ):
            raise HTTPException(409, "Setup-code receiving is unavailable") from None
        return JSONResponse({"entry_code": code}, headers=HEADERS)

    @application.post("/api/v1/setup-code/consume", include_in_schema=False)
    async def consume(request: Request):
        _browser(request, mutation=True)
        value = await body(request)
        if (
            set(value) != {"code"}
            or not isinstance(value["code"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", value["code"]) is None
        ):
            raise HTTPException(400, "Invalid receiving entry")
        session = sessions.consume(value["code"])
        response = JSONResponse({"journey_id": session.journey_id}, headers=HEADERS)
        response.set_cookie(
            COOKIE,
            session.identifier,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=max(0, int(session.expires_at - sessions.store._now().timestamp())),
        )
        return response

    @application.get("/api/v1/setup-code/session", include_in_schema=False)
    def read_session(request: Request):
        session = sessions.require_session(request)
        return JSONResponse(
            {
                "journey_id": session.journey_id,
                "expires_at": session.expires_at,
                "workspace_journey_id": session.workspace_journey_id,
                "csrf_token": session.csrf_token,
                "hosted_url": transfer.return_url,
            },
            headers=HEADERS,
        )

    @application.get("/setup-code", include_in_schema=False)
    @application.get("/setup-code/entry", include_in_schema=False)
    def shell(request: Request):
        from app.onboarding.surface import setup_shell

        return setup_shell(request, receiving_origin=transfer.hosted_origin)
