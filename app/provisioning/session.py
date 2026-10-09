"""Ephemeral browser-session controls for bounded provisioning."""

from __future__ import annotations

import secrets
import hashlib
import time
import re
from uuid import uuid4
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request, status


PROVISIONING_ORIGIN = "http://bridge.localhost:17871"
PROVISIONING_HOST = "bridge.localhost:17871"
PROVISIONING_SESSION_COOKIE_NAME = "__Host-provisioning_session"
PROVISIONING_CSRF_HEADER = "X-Provisioning-CSRF"
NATIVE_ENTRY_COOKIE_NAME = "__Host-onboarding_native_entry"


@dataclass(frozen=True, slots=True)
class ProvisioningBrowserSession:
    """One purpose-bound browser session held only in process memory."""

    identifier: str
    csrf_token: str
    expires_at: float
    journey_id: str | None = None


@dataclass(slots=True)
class NativeEntry:
    identifier: str
    expires_at: float
    entry_id: str
    journey_id: str | None = None
    session_identifier: str | None = None
    consumed: bool = False


class ProvisioningSessionManager:
    """Issue and validate launcher-to-browser provisioning handoffs."""

    def __init__(
        self,
        launcher_handoff_token: str | None,
        *,
        ttl_seconds: float = 1800.0,
        monotonic: Callable[[], float] = time.monotonic,
        journeys=None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if launcher_handoff_token is not None and len(launcher_handoff_token) < 32:
            raise ValueError("provisioning handoff token is invalid")
        if ttl_seconds <= 0:
            raise ValueError("provisioning session lifetime must be positive")
        self._journeys = journeys
        self._wall_clock = wall_clock
        self._launcher_handoff_token = launcher_handoff_token
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._handoff_codes: dict[str, tuple[float, str | None]] = {}
        self._sessions: dict[str, ProvisioningBrowserSession] = {}
        self._native_codes: dict[str, tuple[float, str | None]] = {}
        self._native_entries: dict[str, NativeEntry] = {}

    def issue_native_entry(self, authorization: str | None, *, journey_id: str | None = None) -> str:
        """Authorize browser entry without choosing or creating a journey."""
        self.require_launcher(authorization)
        now = self._monotonic()
        if journey_id is not None:
            from app.persistence.onboarding import require_journey
            try:
                require_journey(journey_id)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, "Journey is invalid") from None
        self._native_codes = {key: value for key, value in self._native_codes.items() if value[0] > now}
        self._native_entries = {key: entry for key, entry in self._native_entries.items() if entry.expires_at > now}
        if len(self._native_codes) + len(self._native_entries) >= 128:
            raise HTTPException(429, "Onboarding entry limit reached")
        code = secrets.token_urlsafe(32)
        self._native_codes[code] = (now + min(300.0, self._ttl_seconds), journey_id)
        return code

    def redeem_native_entry(self, code: str) -> NativeEntry:
        expiry, journey = self._native_codes.pop(code, (0, None))
        if expiry <= self._monotonic():
            raise HTTPException(401, "Native entry is invalid")
        identifier = secrets.token_urlsafe(32)
        entry = NativeEntry(identifier, expiry, str(uuid4()), journey)
        self._native_entries[identifier] = entry
        return entry

    def require_native_entry(self, request: Request) -> NativeEntry:
        self._require_exact_host(request)
        identifier = request.cookies.get(NATIVE_ENTRY_COOKIE_NAME, "")
        entry = self._native_entries.get(identifier)
        if entry is None or entry.expires_at <= self._monotonic():
            raise HTTPException(401, "Native entry is invalid")
        return entry

    def native_entry_context(self, request: Request) -> dict:
        entry = self.require_native_entry(request)
        if not entry.consumed:
            return {"state": "select_workspace", "csrf_token": _session_csrf(entry.identifier), "entry_id": entry.entry_id}
        try:
            session = self.require_session(request)
        except HTTPException:
            return {"state": "unconfirmed"}
        if session.identifier != entry.session_identifier:
            return {"state": "unconfirmed"}
        self.require_native_entry(request)
        return {"state": "selected", "journey_id": session.journey_id}

    def select_native_workspace(self, request: Request, journey_id: str | None) -> ProvisioningBrowserSession:
        entry = self.require_native_entry(request)
        presented = request.headers.getlist(PROVISIONING_CSRF_HEADER)
        if (request.headers.getlist("origin") != [PROVISIONING_ORIGIN]
                or len(presented) != 1 or re.fullmatch(r"[A-Za-z0-9_-]{43}", presented[0]) is None
                or not secrets.compare_digest(presented[0], _session_csrf(entry.identifier))):
            raise HTTPException(403, "Native entry context is invalid")
        if entry.consumed:
            raise HTTPException(409, "Native entry was used")
        if entry.journey_id is not None and entry.journey_id != journey_id:
            raise HTTPException(409, "Onboarding scope changed")
        if journey_id is not None:
            from app.persistence.onboarding import require_journey
            try:
                require_journey(journey_id)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, "Journey is invalid") from None
        try:
            existing = self.require_session(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
            existing = None
        # A browser that already has setup authority keeps that exact session.
        # Discovery cannot retarget it to a different creator/enrollment draft.
        if existing is not None:
            if journey_id is not None and existing.journey_id != journey_id:
                raise HTTPException(409, "Onboarding scope changed")
            session = existing
            self.require_native_entry(request)
        else:
            session = self._create_session(journey_id, authorize=lambda: self.require_native_entry(request))
        entry.session_identifier = session.identifier
        entry.consumed = True
        return session

    def issue_handoff_code(self, authorization: str | None, *, journey_id: str | None = None) -> str:
        """Exchange the launcher secret for a single browser handoff code."""
        self.require_launcher(authorization)
        if journey_id is not None:
            from app.persistence.onboarding import require_journey
            try:
                require_journey(journey_id)
            except ValueError:
                raise HTTPException(400, "Journey is invalid") from None
        self._handoff_codes = {key: value for key, value in self._handoff_codes.items() if value[0] > self._monotonic()}
        if len(self._handoff_codes) >= 128:
            raise HTTPException(429, "Onboarding handoff limit reached")
        code = secrets.token_urlsafe(32)
        self._handoff_codes[code] = (self._monotonic() + min(300.0, self._ttl_seconds), journey_id)
        return code

    def require_launcher(self, authorization: str | None) -> None:
        expected = self._launcher_handoff_token
        if expected is None or authorization is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "launcher authorization is invalid")
        scheme, separator, presented = authorization.partition(" ")
        if separator != " " or scheme != "Provisioning" or not secrets.compare_digest(
            expected, presented
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "launcher authorization is invalid")

    def launcher_workspace(self, journey_id: str | None):
        return None if self._journeys is None else self._journeys.open(journey_id)

    def redeem_handoff_code(self, code: str) -> ProvisioningBrowserSession:
        """Consume one launcher handoff and create a browser session."""
        handoff = self._handoff_codes.pop(code, None)
        if handoff is None or handoff[0] <= self._monotonic():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning handoff is invalid")
        return self._create_session(handoff[1])

    def _create_session(self, journey_id: str | None, *, authorize=None) -> ProvisioningBrowserSession:
        identifier = secrets.token_urlsafe(32)
        try:
            journey = None if self._journeys is None else self._journeys.open(journey_id)
        except ValueError:
            raise HTTPException(409, "Onboarding scope is unavailable") from None
        if authorize is not None:
            authorize()
        session = ProvisioningBrowserSession(
            identifier=identifier,
            csrf_token=_session_csrf(identifier),
            expires_at=(self._monotonic() if journey is None else self._wall_clock()) + self._ttl_seconds,
            journey_id=None if journey is None else journey["journey_id"],
        )
        if journey is not None:
            self._journeys.record_session(identifier=identifier, csrf=session.csrf_token,
                journey_id=session.journey_id, expires_at=session.expires_at, authorize=authorize)
        self._sessions[session.identifier] = session
        return session

    def require_session(self, request: Request) -> ProvisioningBrowserSession:
        """Require an unexpired bounded provisioning browser session."""
        self._require_exact_host(request)
        identifier = request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME)
        if not isinstance(identifier,str) or re.fullmatch(r"[A-Za-z0-9_-]{43}",identifier) is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning session is invalid")
        session = self._sessions.get(identifier or "")
        if session is None and identifier and self._journeys is not None:
            stored = self._journeys.session(identifier)
            if stored is not None:
                session = ProvisioningBrowserSession(identifier, _session_csrf(identifier), stored["expires_at"], stored["journey_id"])
        now = self._wall_clock() if self._journeys is not None else self._monotonic()
        if session is None or session.expires_at <= now:
            if identifier is not None:
                self._sessions.pop(identifier, None)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning session is invalid")
        return session

    def require_mutation(self, request: Request) -> ProvisioningBrowserSession:
        """Require exact origin, session, and session-bound CSRF for a mutation."""
        session = self.require_session(request)
        if request.headers.getlist("origin") != [PROVISIONING_ORIGIN]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "provisioning origin is invalid")
        presented = request.headers.get(PROVISIONING_CSRF_HEADER)
        if presented is None or re.fullmatch(r"[A-Za-z0-9_-]{43}",presented) is None or request.headers.getlist(PROVISIONING_CSRF_HEADER)!=[presented] or not secrets.compare_digest(session.csrf_token, presented):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "provisioning CSRF is invalid")
        return session

    @staticmethod
    def _require_exact_host(request: Request) -> None:
        if request.headers.get("host") != PROVISIONING_HOST:
            raise HTTPException(status.HTTP_421_MISDIRECTED_REQUEST, "provisioning host is invalid")


def _session_csrf(identifier: str) -> str:
    # HttpOnly opaque cookie is the browser binding; only this domain-separated
    # derivative reaches same-origin script. Persist only its digest.
    import base64
    return base64.urlsafe_b64encode(hashlib.sha256(b"provisioning-csrf-v1\0" + identifier.encode()).digest()).rstrip(b"=").decode()
