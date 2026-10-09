"""Ephemeral browser-session controls for bounded provisioning."""

from __future__ import annotations

import secrets
import hashlib
import time
import re
from threading import RLock
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
    selected_journey_id: str | None = None
    previous_journey_id: str | None = None
    continuation_target: object | None = None
    continuation_checked: bool = False


class ProvisioningSessionManager:
    """Issue and validate launcher-to-browser provisioning handoffs."""

    def __init__(
        self,
        launcher_handoff_token: str | None,
        *,
        ttl_seconds: float = 1800.0,
        monotonic: Callable[[], float] = time.monotonic,
        journeys=None,
        wall_clock: Callable[[], float] | None = None,
        continuation_key=None,
    ) -> None:
        if launcher_handoff_token is not None and len(launcher_handoff_token) < 32:
            raise ValueError("provisioning handoff token is invalid")
        if ttl_seconds <= 0:
            raise ValueError("provisioning session lifetime must be positive")
        self._journeys = journeys
        self._continuation_key = continuation_key
        if journeys is not None:
            from app.persistence.installation_continuation import InstallationContinuationStore
            self._continuations = InstallationContinuationStore(journeys.authentication)
        else:
            self._continuations = None
        self._wall_clock = wall_clock or (
            (lambda: journeys.authentication._now().timestamp()) if journeys is not None else time.time)
        self._launcher_handoff_token = launcher_handoff_token
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._handoff_codes: dict[str, tuple[float, str | None]] = {}
        self._sessions: dict[str, ProvisioningBrowserSession] = {}
        self._native_codes: dict[str, tuple[float, str | None]] = {}
        self._native_entries: dict[str, NativeEntry] = {}
        self._selection_lock = RLock()

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
        if entry is None and self._continuations is not None:
            receipt = self._continuations.native_receipt(identifier)
            if receipt is not None:
                selected_cookie = request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME, "")
                selected_identifier = (selected_cookie if hashlib.sha256(selected_cookie.encode()).hexdigest()
                    == receipt["session_digest"] else None)
                entry = NativeEntry(identifier,
                    self._monotonic() + max(0, receipt["expires_at"] - self._wall_clock()),
                    receipt["entry_id"], session_identifier=selected_identifier, consumed=True,
                    selected_journey_id=receipt["journey_id"], previous_journey_id=receipt["previous_journey_id"])
        if entry is None or entry.expires_at <= self._monotonic():
            raise HTTPException(401, "Native entry is invalid")
        return entry

    def native_entry_context(self, request: Request) -> dict:
        with self._selection_lock:
            return self._native_entry_context(request)

    def _native_entry_context(self, request: Request) -> dict:
        entry = self.require_native_entry(request)
        if not entry.consumed:
            if not entry.continuation_checked:
                entry.continuation_target = self._saved_continuation_target(entry, request)
                entry.continuation_checked = True
            return {"state": "select_workspace", "csrf_token": _session_csrf(entry.identifier), "entry_id": entry.entry_id,
                **({"target_journey_id": entry.journey_id} if entry.journey_id is not None else {}),
                **({"continue_saved": True} if entry.continuation_target is not None else {})}
        try:
            session = self.require_session(request)
        except HTTPException:
            return {"state": "unconfirmed"}
        if session.identifier != entry.session_identifier or session.journey_id != entry.selected_journey_id:
            return {"state": "unconfirmed"}
        self.require_native_entry(request)
        return {"state": "selected", "journey_id": entry.selected_journey_id,
                **({"previous_journey_id": entry.previous_journey_id} if entry.previous_journey_id else {})}

    def select_native_workspace(self, request: Request, journey_id: str | None, *, recover: bool = False,
                                continue_saved: bool = False) -> ProvisioningBrowserSession:
        with self._selection_lock:
            return self._select_native_workspace(request, journey_id, recover=recover, continue_saved=continue_saved)

    def _select_native_workspace(self, request: Request, journey_id: str | None, *, recover: bool,
                                 continue_saved: bool = False) -> ProvisioningBrowserSession:
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
        if continue_saved:
            if recover or entry.continuation_target is None:
                raise HTTPException(409, "Saved setup is unavailable")
            try:
                existing = self.require_session(request)
            except HTTPException as error:
                if error.status_code != 401:
                    raise
                existing = None
            if existing is not None:
                from app.persistence.onboarding import OnboardingJourneyUnavailable
                from app.persistence.continuation_selection import ContinuationSelectionUnavailable
                try:
                    target = self._continuations.require_current(existing.journey_id)["target"]
                    if target != entry.continuation_target:
                        raise OnboardingJourneyUnavailable("journey_unavailable")
                    self._continuations.retain_native_session(journey_id=existing.journey_id,
                        identifier=existing.identifier, entry_identifier=entry.identifier, entry_id=entry.entry_id,
                        entry_expires_at=self._wall_clock() + max(0, entry.expires_at - self._monotonic()),
                        previous_journey_id=journey_id, saved_target=entry.continuation_target,
                        authorize=lambda: self.require_native_entry(request))
                except (OnboardingJourneyUnavailable, ContinuationSelectionUnavailable):
                    raise HTTPException(409, "Saved setup is unavailable") from None
                session = existing
            else:
                session = self._admit_registered_session(request, entry, None,
                    target=entry.continuation_target, previous_journey_id=journey_id, saved_navigation=True)
                if session is None:
                    raise HTTPException(409, "Saved setup is unavailable")
            entry.session_identifier = session.identifier
            entry.selected_journey_id = session.journey_id
            entry.previous_journey_id = journey_id
            entry.consumed = True
            return session
        if journey_id is None and entry.continuation_target is not None:
            # An offered saved target requires its explicit selection variant;
            # the ordinary null path must not choose again from changed facts.
            raise HTTPException(409, "Saved setup is unavailable")
        if recover:
            if journey_id is None or self._journeys is None:
                raise HTTPException(409, "Onboarding scope is unavailable")
            from app.persistence.onboarding import OnboardingJourneyUnavailable
            identifier = secrets.token_urlsafe(32)
            def authorize():
                if self.require_native_entry(request) is not entry or entry.consumed:
                    raise HTTPException(409, "Native entry was used")
            try:
                selected = self._journeys.renew_native_session(previous_journey_id=journey_id,
                    identifier=identifier, csrf=_session_csrf(identifier),
                    existing_identifier=request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME),
                    ttl_seconds=self._ttl_seconds, authorize=authorize)
            except OnboardingJourneyUnavailable:
                raise HTTPException(409, "Onboarding scope is unavailable") from None
            session = ProvisioningBrowserSession(selected["identifier"], _session_csrf(selected["identifier"]),
                selected["expires_at"], selected["journey_id"])
            self._sessions[session.identifier] = session
            entry.session_identifier = session.identifier
            entry.selected_journey_id = session.journey_id
            entry.previous_journey_id = journey_id
            entry.consumed = True
            return session
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
            if self.context_kind(session) == "registered-continuation":
                from app.persistence.continuation_selection import ContinuationSelectionUnavailable
                from app.persistence.onboarding import OnboardingJourneyUnavailable
                def authorize():
                    if self.require_native_entry(request) is not entry or entry.consumed:
                        raise HTTPException(409, "Native entry was used")
                try:
                    self._continuations.retain_native_session(
                        journey_id=session.journey_id, identifier=session.identifier,
                        entry_identifier=entry.identifier, entry_id=entry.entry_id,
                        entry_expires_at=self._wall_clock() + max(0, entry.expires_at - self._monotonic()),
                        authorize=authorize,
                    )
                except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable):
                    raise HTTPException(409, "Saved setup is unavailable") from None
        else:
            session = self._admit_registered_session(request, entry, journey_id)
            if session is None:
                session = self._create_session(journey_id, authorize=lambda: self.require_native_entry(request))
        entry.session_identifier = session.identifier
        entry.selected_journey_id = session.journey_id
        entry.consumed = True
        return session

    def _admit_registered_session(self, request: Request, entry: NativeEntry,
                                  journey_id: str | None, *, target=None,
                                  previous_journey_id: str | None = None,
                                  saved_navigation: bool = False) -> ProvisioningBrowserSession | None:
        if self._continuations is None or self._continuation_key is None:
            return None
        from app.persistence.continuation_selection import ContinuationSelectionUnavailable
        from app.persistence.onboarding import OnboardingJourneyUnavailable
        from app.security.installation_key import InstallationKeyError
        try:
            if target is not None:
                pass
            elif journey_id is not None:
                current = self._journeys.require_current(journey_id)
                if current["kind"] != "registered-continuation":
                    return None
                target = self._continuations.select_for_admission(journey_id)
            else:
                try:
                    target = self._continuations.select_for_admission()
                except ContinuationSelectionUnavailable as error:
                    # A first-run claim with no creator yet keeps its existing
                    # creator-confirmation route, never an automatic substitute.
                    if error.reason in {"registration_required", "target_unavailable"}:
                        return None
                    raise
            reopened = self._continuation_key.reopen_existing()
            identifier = secrets.token_urlsafe(32)
            entry_deadline = self._wall_clock() + max(0, entry.expires_at - self._monotonic())
            def authorize():
                if self.require_native_entry(request) is not entry or entry.consumed:
                    raise HTTPException(409, "Native entry was used")
            selected = self._continuations.admit_native_session(
                target=target, reopened_key=reopened, identifier=identifier, csrf=_session_csrf(identifier),
                entry_identifier=entry.identifier, entry_id=entry.entry_id, entry_expires_at=entry_deadline,
                existing_identifier=request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME),
                ttl_seconds=self._ttl_seconds, authorize=authorize,
                previous_journey_id=previous_journey_id, saved_navigation=saved_navigation,
            )
        except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, InstallationKeyError):
            raise HTTPException(409, "Saved setup is unavailable") from None
        session = ProvisioningBrowserSession(identifier, _session_csrf(identifier),
            selected["expires_at"], selected["journey_id"])
        self._sessions[identifier] = session
        return session

    def _saved_continuation_target(self, entry: NativeEntry, request: Request):
        """Offer an exact saved intent without replacing existing initial authority."""
        if self._continuations is None or self._continuation_key is None:
            return None
        from app.persistence.continuation_selection import ContinuationSelectionUnavailable
        from app.persistence.onboarding import OnboardingJourneyUnavailable
        try:
            try:
                existing = self.require_session(request)
            except HTTPException as error:
                if error.status_code != 401:
                    raise
                existing = None
            if existing is not None and self.context_kind(existing) == "initial-enrollment":
                return None
            return self._continuations.saved_native_target(entry.journey_id)
        except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, ValueError, KeyError, TypeError):
            return None

    def context_kind(self, session: ProvisioningBrowserSession) -> str:
        if self._journeys is None or session.journey_id is None:
            return "initial-enrollment"
        from app.persistence.onboarding import OnboardingJourneyUnavailable
        try:
            return self._journeys.require_current(session.journey_id)["kind"]
        except OnboardingJourneyUnavailable:
            raise HTTPException(401, "Provisioning session is invalid") from None

    def require_initial_context(self, session: ProvisioningBrowserSession) -> None:
        if self.context_kind(session) != "initial-enrollment":
            raise HTTPException(409, "Setup context is unavailable")

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
        csrf = _session_csrf(identifier)
        if self._journeys is not None:
            from app.persistence.onboarding import OnboardingJourneyUnavailable
            try:
                selected = self._journeys.create_session(identifier=identifier, csrf=csrf,
                    journey_id=journey_id, expires_at=self._wall_clock()+self._ttl_seconds, authorize=authorize)
            except (OnboardingJourneyUnavailable, ValueError):
                raise HTTPException(409, "Onboarding scope is unavailable") from None
            session = ProvisioningBrowserSession(identifier, csrf, selected["expires_at"], selected["journey_id"])
        else:
            if authorize is not None:
                authorize()
            session = ProvisioningBrowserSession(identifier, csrf, self._monotonic()+self._ttl_seconds)
        self._sessions[session.identifier] = session
        return session

    def require_session(self, request: Request) -> ProvisioningBrowserSession:
        """Require an unexpired bounded provisioning browser session."""
        self._require_exact_host(request)
        identifier = request.cookies.get(PROVISIONING_SESSION_COOKIE_NAME)
        if not isinstance(identifier,str) or re.fullmatch(r"[A-Za-z0-9_-]{43}",identifier) is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning session is invalid")
        session = self._sessions.get(identifier or "")
        if self._journeys is not None:
            # Memory is only a cache, never authority after durable expiry/deletion.
            stored = self._journeys.current_session(identifier)
            session = None if stored is None else ProvisioningBrowserSession(
                identifier, _session_csrf(identifier), stored["expires_at"], stored["journey_id"])
        now = self._wall_clock() if self._journeys is not None else self._monotonic()
        if session is None or session.expires_at <= now:
            if identifier is not None:
                self._sessions.pop(identifier, None)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "provisioning session is invalid")
        return session

    def session_remaining(self, request: Request) -> float:
        session = self.require_session(request)
        now = self._wall_clock() if self._journeys is not None else self._monotonic()
        return max(0.0, session.expires_at - now)

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
