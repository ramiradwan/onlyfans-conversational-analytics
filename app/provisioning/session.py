"""Ephemeral browser-session controls for bounded provisioning."""

from __future__ import annotations

import secrets
import hashlib
import time
import re
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException, Request, status


PROVISIONING_ORIGIN = "http://bridge.localhost:17871"
PROVISIONING_HOST = "bridge.localhost:17871"
PROVISIONING_SESSION_COOKIE_NAME = "__Host-provisioning_session"
PROVISIONING_CSRF_HEADER = "X-Provisioning-CSRF"


@dataclass(frozen=True, slots=True)
class ProvisioningBrowserSession:
    """One purpose-bound browser session held only in process memory."""

    identifier: str
    csrf_token: str
    expires_at: float
    journey_id: str | None = None


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
        identifier = secrets.token_urlsafe(32)
        journey = None if self._journeys is None else self._journeys.open(handoff[1])
        session = ProvisioningBrowserSession(
            identifier=identifier,
            csrf_token=_session_csrf(identifier),
            expires_at=(self._monotonic() if journey is None else self._wall_clock()) + self._ttl_seconds,
            journey_id=None if journey is None else journey["journey_id"],
        )
        if journey is not None:
            self._journeys.record_session(identifier=identifier, csrf=session.csrf_token,
                journey_id=session.journey_id, expires_at=session.expires_at)
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
