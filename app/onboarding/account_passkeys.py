"""Product-owned foreground enrollment and post-Connect local binding."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from threading import Lock

from app.onboarding.admission import AdmissionRefused, AdmittedSession, ScopedAdmissions
from app.onboarding.ceremonies import BrowserCeremonies
from app.persistence.auth import AuthenticationStateError, _new_uuid7
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.account_passkey import AccountPasskeyRefused
from app.security.account_passkey_authority import AccountPasskeyAuthority
from app.security.account_passkey_enrollment import AccountPasskeyEnrollment, EnrollmentAttempt, RegisteredPasskey
from app.security.account_passkey_webauthn import NativeRegistration
from app.security.account_passkey_windows import MAX_CEREMONY_SECONDS, WindowsHelloRefused
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.saved_data import InitialSavedDataAccess
from app.security.signed_objects import SignedObjectRejected
from app.security.webauthn import WebAuthnVerificationError


@dataclass(frozen=True, slots=True, repr=False)
class _CompletedEnrollment:
    parent: str
    binding_json: str
    ceremony_id: str
    creator_account_id: str
    registration: RegisteredPasskey
    presentation: AdmittedSession | None


class DesktopAccountPasskeys:
    def __init__(self, ceremonies: BrowserCeremonies, enrollment: AccountPasskeyEnrollment,
                 authority: AccountPasskeyAuthority, admissions: ScopedAdmissions) -> None:
        self.ceremonies = ceremonies
        self.enrollment = enrollment
        self.authority = authority
        self.store = authority.store
        self.admissions = admissions
        self.effects = OwnerEffects()
        self._lock = Lock()
        self._running = False
        self._completed: _CompletedEnrollment | None = None
        self._initial: tuple[AdmittedSession, InitialSavedDataAccess] | None = None
        self._uncertain: tuple[str, str, EnrollmentAttempt, NativeRegistration] | None = None

    def displayed(self, selection) -> str | None:
        if selection is None or selection["projection"]["decision"]["action"] != "connect-account":
            return None
        projection = selection["projection"]
        with self._lock:
            if self._completed is not None and (
                self._completed.parent, self._completed.ceremony_id
            ) == (selection["operation_id"], projection["ceremony_id"]):
                return "created"
            return "working" if self._running else "available"

    async def create(self, journey_id, selection, *, session, authorize) -> str:
        return await self.effects.run(
            self._create, journey_id, selection, session=session, authorize=authorize, journey_id=journey_id,
        )

    def _create(self, journey_id, selection, *, session, authorize) -> str:
        parent, projection = selection["operation_id"], selection["projection"]
        row, request = self.ceremonies.decisions._request(parent)
        binding = request["binding"]
        if (self.ceremonies._journey(parent) != journey_id or binding["purpose"] != "connect-account"
                or request["ceremony_id"] != projection["ceremony_id"]
                or request["operation_id"] != projection["operation_id"]):
            raise HostedGrantUnavailable("Account passkey source changed")
        guard = self.ceremonies.guarded(row, authorize, mutation=True)
        guard()
        with self._lock:
            if self._running:
                return "working"
            if self._completed is not None and self._completed.ceremony_id == request["ceremony_id"]:
                return "created"
            uncertain = self._uncertain
            self._running = True
        events.publish()
        try:
            presentation = None
            if uncertain is not None and uncertain[:2] == (parent, request["ceremony_id"]):
                _, _, attempt, native = uncertain
                registered = self.enrollment.receipt(attempt, native, before_send=guard)
            else:
                attempt = self.enrollment.begin(
                    ceremony_id=request["ceremony_id"], browser_binding=binding,
                    enrollment_id=_new_uuid7(self.store._now()), initial_access_nonce=secrets.token_urlsafe(32),
                    before_send=guard,
                )
                deadline = min(attempt.expires_at, timestamp(projection["decision"]["expires_at"]))
                duration = min(MAX_CEREMONY_SECONDS, (deadline - self.store._now()).total_seconds())
                try:
                    native = self.authority.broker.enroll(attempt, seconds=duration)
                except WindowsHelloRefused as refusal:
                    try:
                        self.enrollment.cancel(attempt, before_send=guard)
                    except (HostedGrantUnavailable, AccountPasskeyRefused):
                        pass
                    return refusal.reason
                guard()
                # A lost reply is reconciled only by reading this exact receipt.
                with self._lock:
                    self._uncertain = (parent, request["ceremony_id"], attempt, native)
                try:
                    registered = self.enrollment.finish(attempt, native, before_send=guard)
                    if session.stage == "setup":
                        presentation = session
                except HostedGrantUnavailable:
                    registered = self.enrollment.receipt(attempt, native, before_send=guard)
                except (AccountPasskeyRefused, WebAuthnVerificationError):
                    with self._lock:
                        self._uncertain = None
                    raise
            with self._lock:
                self._completed = _CompletedEnrollment(
                    parent, row["binding_json"], request["ceremony_id"], binding["intended_creator_id"],
                    registered, presentation,
                )
                self._uncertain = None
            return "created"
        except AccountPasskeyRefused as refusal:
            if refusal.support_ref is not None:
                try:
                    guard()
                except (AdmissionRefused, AuthenticationStateError, HostedGrantUnavailable):
                    return "unavailable"
                raise
            return "unavailable"
        except (AuthenticationStateError, HostedGrantUnavailable,
                SignedObjectRejected, WebAuthnVerificationError):
            return "unavailable"
        finally:
            with self._lock:
                self._running = False
            events.publish()

    def adopt_after_connect(self, operation, scope, *, authorize) -> None:
        """Install the exact record once the ordinary local finalization has committed."""
        with self._lock:
            completed = self._completed
        if completed is None or self.ceremonies._journey(completed.parent) != operation["journey_id"]:
            return
        if (completed.creator_account_id != scope["creator_account_id"]
                or completed.registration.attempt.installation_id != scope["installation_id"]):
            raise HostedGrantUnavailable("Account passkey registration does not match the committed connection")

        def guard(connection=None):
            authorize(connection=connection)
            current = self.ceremonies.row(completed.parent, connection=connection)
            if (current is None or current["binding_json"] != completed.binding_json
                    or current["ceremony_id"] != completed.ceremony_id):
                raise HostedGrantUnavailable("Account passkey connection changed")

        owner = self.authority.adopt(completed.registration, creator_account_id=completed.creator_account_id, guard=guard)
        initial = self._prepare_initial_access(completed, owner, operation["journey_id"], guard)
        with self._lock:
            if self._completed is completed:
                self._initial = initial
                self._completed = None

    def _prepare_initial_access(self, completed, owner, journey_id, guard):
        presentation = completed.presentation
        if presentation is None:
            return None

        def require_current(connection=None):
            if connection is None:
                with self.store.database.read() as connection:
                    return require_current(connection)
            guard(connection=connection)
            _, reference = self.admissions._require(
                connection, presentation.identifier, role="desktop", generation=presentation.generation,
            )
            if reference["journey_id"] != journey_id:
                raise AdmissionRefused("presentation_replaced")

        try:
            access = self.authority.prepare_initial_access(
                completed.registration, owner, generation=presentation.generation, guard=require_current,
            )
        except (AccountPasskeyRefused, AdmissionRefused, AuthenticationStateError,
                HostedGrantUnavailable, SignedObjectRejected):
            # Connect and the binding have committed. A refused or uncertain
            # first-access issue requires a fresh assertion, without a resend.
            return None
        return presentation, access

    def take_initial_access(self) -> tuple[AdmittedSession, InitialSavedDataAccess] | None:
        with self._lock:
            initial, self._initial = self._initial, None
            return initial

    def pending_adoption(self, operation) -> bool:
        with self._lock:
            completed = self._completed
        return bool(completed is not None and self.ceremonies._journey(completed.parent) == operation["journey_id"])

    async def close(self) -> None:
        self.effects.close_admission()
        await self.authority.close()
        await self.effects.drain()
