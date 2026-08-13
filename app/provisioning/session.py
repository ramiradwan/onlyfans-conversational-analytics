"""Ephemeral browser-session controls for bounded provisioning."""

from __future__ import annotations

import secrets
import time
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


class ProvisioningSessionManager:
    """Issue and validate launcher-to-browser provisioning handoffs."""

    def __init__(
        self,
        launcher_handoff_token: str | None,
        *,
        ttl_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if launcher_handoff_token is not None and len(launcher_handoff_token) < 32:
            raise ValueError("provisioning handoff token is invalid")
        if ttl_seconds <= 0:
            raise ValueError("provisioning session lifetime must be positive")
        self._launcher_handoff_token = launcher_handoff_token
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._handoff_codes: dict[str, float] = {}
        self._sessions: dict[str, ProvisioningBrowserSession] = {}

    def issue_handoff_code(self, authorization: str | None) -> str:
        """Exchange the launcher secret for a single browser handoff code."""
        expected = self._launcher_handoff_token
        if expected is None or authorization is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "launcher authorization is invalid")
        scheme, separator, presented = authorization.partition(" ")
        if separator != " " or scheme != "Provisioning" or not secrets.compare_digest(
            expected, presented
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "launcher authorization is invalid")
        code = secrets.token_urlsafe(32)
        self._handoff_codes[code] = self._monotonic() + self._ttl_seconds
        return code

    def redeem_handoff_code(self, code: str) -> ProvisioningBrowserSession:
        """Consume one launcher handoff and create a browser session."""
        expiry = self._handoff_codes.pop(code, None)
        if expiry is None or expiry <= self._monotonic():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning handoff is invalid")
        session = ProvisioningBrowserSession(
            identifier=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self._monotonic() + self._ttl_seconds,
        )
        self._sessions[session.identifier] = session
        return session

    def require_session(self, request: Request) -> ProvisioningBrowserSession:
        """Require an unexpired bounded provisioning browser session."""
        self._require_exact_host(request)
        identifier = request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME)
        session = self._sessions.get(identifier or "")
        if session is None or session.expires_at <= self._monotonic():
            if identifier is not None:
                self._sessions.pop(identifier, None)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning session is invalid")
        return session

    def require_mutation(self, request: Request) -> ProvisioningBrowserSession:
        """Require exact origin, session, and session-bound CSRF for a mutation."""
        session = self.require_session(request)
        if request.headers.get("origin") != PROVISIONING_ORIGIN:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "provisioning origin is invalid")
        presented = request.headers.get(PROVISIONING_CSRF_HEADER)
        if presented is None or not secrets.compare_digest(session.csrf_token, presented):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "provisioning CSRF is invalid")
        return session

    @staticmethod
    def _require_exact_host(request: Request) -> None:
        if request.headers.get("host") != PROVISIONING_HOST:
            raise HTTPException(status.HTTP_421_MISDIRECTED_REQUEST, "provisioning host is invalid")
