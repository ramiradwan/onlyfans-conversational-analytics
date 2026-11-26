"""Product-owned foreground enrollment and post-Connect local binding."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.onboarding.admission import AdmissionRefused, AdmittedSession, ScopedAdmissions
from app.onboarding.ceremonies import BrowserCeremonies
from app.onboarding.contract import digest
from app.persistence.auth import AuthenticationStateError, _new_uuid7
from app.provisioning.events import events
from app.provisioning.owner_effects import OwnerEffects
from app.security.account_passkey import AccountPasskeyRefused
from app.security.account_passkey_authority import AccountPasskeyAuthority, InitialLocalAccess
from app.security.account_passkey_enrollment import (
    AccountPasskeyEnrollment, EnrollmentAttempt, ObservedRegistration, RegisteredPasskey,
)
from app.security.account_passkey_windows import MAX_CEREMONY_SECONDS, WindowsHelloRefused
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.signed_objects import SignedObjectRejected, decode, encode
from app.security.webauthn import WebAuthnVerificationError


@dataclass(frozen=True, slots=True, repr=False)
class _CompletedEnrollment:
    parent: str
    binding_json: str
    ceremony_id: str
    creator_account_id: str
    registration: RegisteredPasskey
    presentation: AdmittedSession | None
    scope_digest: str


def _scope(selection, row) -> str:
    return digest({
        "selection": selection,
        "binding": row["binding_json"],
        "authentication_revision": row["authentication_revision"],
    })


def _attempt_json(attempt: EnrollmentAttempt) -> str:
    return json.dumps({
        "attempt_id": attempt.attempt_id,
        "rp_id": attempt.rp_id,
        "expires_at": attempt.expires_at.timestamp(),
        "challenge": encode(attempt.challenge),
        "user_handle": encode(attempt.user_handle),
        "display_label": attempt.display_label,
    }, separators=(",", ":"))


def _read_attempt(retained, binding) -> EnrollmentAttempt:
    try:
        saved = json.loads(retained["attempt_json"])
        return EnrollmentAttempt(
            enrollment_id=retained["enrollment_id"], attempt_id=saved["attempt_id"], rp_id=saved["rp_id"],
            expires_at=datetime.fromtimestamp(saved["expires_at"], tz=timezone.utc),
            source_expires_at=timestamp(binding["source_expires_at"]),
            installation_id=binding["installation_id"], installation_key_jkt=binding["installation_key_jkt"],
            initial_access_nonce=retained["initial_access_nonce"],
            challenge=decode(saved["challenge"], 32), user_handle=decode(saved["user_handle"], 32),
            display_label=saved["display_label"],
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise HostedGrantUnavailable("The retained passkey attempt is invalid") from None


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
        self._initial: tuple[AdmittedSession, InitialLocalAccess] | None = None

    def displayed(self, selection, *, journey_id, connection) -> str | None:
        if selection is None or selection["projection"]["decision"]["action"] != "connect-account":
            return None
        with self._lock:
            completed, running = self._completed, self._running
        if completed is not None and completed.parent == selection["operation_id"]:
            try:
                row, _ = self.ceremonies.decisions._request(completed.parent, connection=connection)
                if completed.scope_digest == _scope(selection, row):
                    return "created"
            except HostedGrantUnavailable:
                return None
        if running:
            return "working"
        retained = connection.execute(
            "SELECT 1 FROM desktop_account_passkey_attempts WHERE journey_id=? AND retire_at>?",
            (journey_id, self.store._now().timestamp()),
        ).fetchone()
        return "recoverable" if retained is not None else "available"

    def _reserve(self, journey, scope, source_deadline, *, validate):
        now = self.store._now()
        if source_deadline <= now:
            raise HostedGrantUnavailable("The passkey enrollment source expired")
        with self.store.database.transaction() as connection:
            validate(connection)
            connection.execute("DELETE FROM desktop_account_passkey_attempts WHERE retire_at<=?",
                               (now.timestamp(),))
            retained = connection.execute(
                "SELECT * FROM desktop_account_passkey_attempts WHERE journey_id=?", (journey,),
            ).fetchone()
            if retained is not None:
                if retained["scope_digest"] != scope:
                    raise HostedGrantUnavailable("The previous passkey attempt has a different setup scope")
                return dict(retained), False
            enrollment_id, nonce = _new_uuid7(now), secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO desktop_account_passkey_attempts (journey_id,scope_digest,enrollment_id,"
                "initial_access_nonce,source_deadline,retire_at) VALUES (?,?,?,?,?,?)",
                (journey, scope, enrollment_id, nonce, source_deadline.timestamp(), source_deadline.timestamp()),
            )
            return {"enrollment_id": enrollment_id, "initial_access_nonce": nonce,
                    "attempt_json": None, "credential_id": None, "public_key": None}, True

    def _record_attempt(self, attempt, *, validate):
        with self.store.database.transaction() as connection:
            validate(connection)
            changed = connection.execute(
                "UPDATE desktop_account_passkey_attempts SET attempt_json=?,retire_at=MIN(source_deadline,?) "
                "WHERE enrollment_id=? AND attempt_json IS NULL",
                (_attempt_json(attempt), attempt.expires_at.timestamp(), attempt.enrollment_id),
            )
            if changed.rowcount != 1:
                raise HostedGrantUnavailable("The passkey enrollment attempt changed")

    def _record_observation(self, attempt, observed, *, validate):
        with self.store.database.transaction() as connection:
            validate(connection)
            changed = connection.execute(
                "UPDATE desktop_account_passkey_attempts SET credential_id=?,public_key=?,retire_at=MIN(source_deadline,?) "
                "WHERE enrollment_id=? AND attempt_json IS NOT NULL AND credential_id IS NULL AND retire_at>?",
                (observed.credential_id, observed.public_key, (attempt.expires_at + timedelta(seconds=1800)).timestamp(),
                 attempt.enrollment_id, self.store._now().timestamp()),
            )
            if changed.rowcount != 1:
                raise HostedGrantUnavailable("The pending passkey response changed")

    def _retire(self, attempt, *, guard, validate):
        self.enrollment.cancel(attempt, before_send=guard)
        with self.store.database.transaction() as connection:
            validate(connection)
            connection.execute("DELETE FROM desktop_account_passkey_attempts WHERE enrollment_id=?",
                               (attempt.enrollment_id,))

    def _recover(self, retained, attempt, *, guard, validate):
        if retained["credential_id"] is not None:
            observed = ObservedRegistration(retained["credential_id"], retained["public_key"])
            try:
                return self.enrollment.receipt(attempt, observed, before_send=guard)
            except AccountPasskeyRefused as refusal:
                if refusal.code != "result_unavailable":
                    raise
        # A successful, exact cancel settles a pending attempt. Ambiguous
        # responses keep it retained and never authorize a new Hello attempt.
        self._retire(attempt, guard=guard, validate=validate)
        return None

    def expire_retained(self) -> float | None:
        """Remove working records at their original deadline and return the next deadline."""
        now = self.store._now().timestamp()
        query = "SELECT MIN(retire_at) FROM desktop_account_passkey_attempts"
        with self.store.database.read() as connection:
            deadline = connection.execute(query).fetchone()[0]
        if deadline is not None and deadline <= now:
            with self.store.database.transaction() as connection:
                changed = connection.execute("DELETE FROM desktop_account_passkey_attempts WHERE retire_at<=?", (now,))
                deadline = connection.execute(query).fetchone()[0]
            if changed.rowcount:
                events.publish()
        return deadline

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
        scope = _scope(selection, row)

        def validate(connection):
            authorize(connection=connection)
            self.ceremonies.require_current(row, connection=connection, mutation=True)

        with self._lock:
            if self._running:
                return "working"
            if self._completed is not None and self._completed.scope_digest == scope:
                return "created"
            self._running = True
        events.publish()
        try:
            retained, fresh = self._reserve(journey_id, scope, timestamp(binding["source_expires_at"]), validate=validate)
            if retained["attempt_json"] is None:
                attempt = self.enrollment.begin(
                    ceremony_id=request["ceremony_id"], browser_binding=binding,
                    enrollment_id=retained["enrollment_id"], initial_access_nonce=retained["initial_access_nonce"],
                    before_send=guard,
                )
                self._record_attempt(attempt, validate=validate)
            else:
                attempt = _read_attempt(retained, binding)
            presentation = None
            if not fresh:
                registered = self._recover(retained, attempt, guard=guard, validate=validate)
                if registered is None:
                    return "cancelled"
            else:
                deadline = min(attempt.expires_at, timestamp(projection["decision"]["expires_at"]))
                duration = min(MAX_CEREMONY_SECONDS, (deadline - self.store._now()).total_seconds())
                try:
                    guard()
                    native = self.authority.broker.enroll(attempt, seconds=duration)
                except WindowsHelloRefused as refusal:
                    try:
                        self._retire(attempt, guard=guard, validate=validate)
                    except (HostedGrantUnavailable, AccountPasskeyRefused):
                        pass
                    return refusal.reason
                observed = self.enrollment.observe(attempt, native)
                self._record_observation(attempt, observed, validate=validate)
                try:
                    registered = self.enrollment.finish(attempt, native, before_send=guard)
                    if session.stage == "setup":
                        presentation = session
                except HostedGrantUnavailable:
                    registered = self.enrollment.receipt(attempt, observed, before_send=guard)
            with self.store.database.transaction() as connection:
                validate(connection)
                connection.execute(
                    "UPDATE desktop_account_passkey_attempts SET retire_at=MIN(retire_at,?) WHERE enrollment_id=?",
                    (registered.receipt_expires_at.timestamp(), attempt.enrollment_id),
                )
            with self._lock:
                self._completed = _CompletedEnrollment(
                    parent, row["binding_json"], request["ceremony_id"], binding["intended_creator_id"],
                    registered, presentation, scope,
                )
            return "created"
        except AccountPasskeyRefused as refusal:
            if refusal.support_ref is not None:
                try:
                    guard()
                except (AdmissionRefused, AuthenticationStateError, HostedGrantUnavailable):
                    return "unavailable"
                raise
            return "unavailable"
        except (AuthenticationStateError, HostedGrantUnavailable, SignedObjectRejected, WebAuthnVerificationError):
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
        with self.store.database.transaction() as connection:
            guard(connection)
            connection.execute("DELETE FROM desktop_account_passkey_attempts WHERE enrollment_id=?",
                               (completed.registration.attempt.enrollment_id,))
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

    def take_initial_access(self) -> tuple[AdmittedSession, InitialLocalAccess] | None:
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
