"""Product-owned checks around a single, explicit native enrollment attempt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.security.account_passkey import ENROLLMENT_PROFILE, AccountPasskeyNativeClient
from app.security.account_passkey_webauthn import NativeRegistration, verify_registration
from app.security.browser_ceremony import binding_digest
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp, valid_identifier
from app.security.signed_objects import SignedObjectRejected, decode


@dataclass(frozen=True, slots=True)
class EnrollmentAttempt:
    enrollment_id: str
    attempt_id: str
    rp_id: str
    expires_at: datetime
    source_expires_at: datetime
    installation_id: str = field(repr=False)
    installation_key_jkt: str = field(repr=False)
    initial_access_nonce: str = field(repr=False)
    challenge: bytes = field(repr=False)
    user_handle: bytes = field(repr=False)
    display_label: str


@dataclass(frozen=True, slots=True)
class RegisteredPasskey:
    attempt: EnrollmentAttempt = field(repr=False)
    credential_id: str = field(repr=False)
    public_key: bytes = field(repr=False)
    registered_at: datetime
    receipt_expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class ObservedRegistration:
    """Public credential evidence checked against the original native challenge."""

    credential_id: str
    public_key: bytes


class AccountPasskeyEnrollment:
    """Accept only an owner's exact ceremony and a locally verified Hello result."""

    def __init__(
        self,
        client: AccountPasskeyNativeClient,
        *,
        rp_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._rp_id = rp_id
        self._clock = clock

    def begin(
        self,
        *,
        ceremony_id: str,
        browser_binding: dict[str, Any],
        enrollment_id: str,
        initial_access_nonce: str,
        before_send: Callable[[], None],
    ) -> EnrollmentAttempt:
        """Begin from the browser binding already checked by the setup owner."""
        source_kinds = {"initial": "initial-handoff", "registered": "registered-continuation"}
        try:
            source = browser_binding["source"]
            source_kind = source_kinds[source["kind"]]
            installation_id = browser_binding["installation_id"]
            installation_key_jkt = browser_binding["installation_key_jkt"]
            source_expires_at = timestamp(browser_binding["source_expires_at"])
            if (
                browser_binding["purpose"] != "connect-account"
                or not valid_identifier(installation_id)
                or len(decode(initial_access_nonce, 32)) != 32
                or len(decode(installation_key_jkt, 32)) != 32
            ):
                raise SignedObjectRejected()
            request = {
                "profile": ENROLLMENT_PROFILE,
                "source": {
                    "kind": source_kind,
                    "operation_id": source["operation_id"],
                    "binding_digest": binding_digest(browser_binding),
                    "ceremony_id": ceremony_id,
                },
                "enrollment_id": enrollment_id,
                "intent": "create-account-passkey",
                "initial_access_nonce": initial_access_nonce,
            }
        except (KeyError, TypeError, SignedObjectRejected):
            raise HostedGrantUnavailable("Account passkey source is invalid") from None
        if source_expires_at <= self._clock():
            raise HostedGrantUnavailable("Account passkey source expired")
        reply = self._client.exchange(
            "enrollment-begin",
            request,
            expected_key_jkt=installation_key_jkt,
            before_send=before_send,
        )
        before_send()
        expires = timestamp(reply["expires_at"])
        now = self._clock()
        if (
            reply["enrollment_id"] != enrollment_id
            or reply["rp_id"] != self._rp_id
            or not now < expires <= min(source_expires_at, now + timedelta(seconds=300))
        ):
            raise HostedGrantUnavailable("Account passkey attempt does not match")
        try:
            for field_name in ("attempt_id", "challenge", "user_handle"):
                if len(decode(reply[field_name], 32)) != 32:
                    raise SignedObjectRejected()
        except SignedObjectRejected:
            raise HostedGrantUnavailable("Account passkey attempt is invalid") from None
        return EnrollmentAttempt(
            enrollment_id=enrollment_id,
            attempt_id=reply["attempt_id"],
            rp_id=self._rp_id,
            expires_at=expires,
            source_expires_at=source_expires_at,
            installation_id=installation_id,
            installation_key_jkt=installation_key_jkt,
            initial_access_nonce=initial_access_nonce,
            challenge=decode(reply["challenge"], 32),
            user_handle=decode(reply["user_handle"], 32),
            display_label=reply["display_label"],
        )

    def finish(
        self,
        attempt: EnrollmentAttempt,
        registration: NativeRegistration,
        *,
        before_send: Callable[[], None],
    ) -> RegisteredPasskey:
        observed = self.observe(attempt, registration)

        def guard() -> None:
            before_send()
            if attempt.rp_id != self._rp_id or self._clock() >= attempt.expires_at:
                raise HostedGrantUnavailable("Account passkey attempt expired")

        return self._result(
            "enrollment-finish", attempt, observed, guard, before_send, native=registration
        )

    @staticmethod
    def observe(attempt: EnrollmentAttempt, registration: NativeRegistration) -> ObservedRegistration:
        return ObservedRegistration(
            registration.credential_id,
            verify_registration(registration, rp_id=attempt.rp_id, challenge=attempt.challenge),
        )

    def receipt(
        self,
        attempt: EnrollmentAttempt,
        registration: NativeRegistration | ObservedRegistration,
        *,
        before_send: Callable[[], None],
    ) -> RegisteredPasskey:
        """Reconcile an unknown completion with a fresh proof and the original result."""
        observed = registration if isinstance(registration, ObservedRegistration) else self.observe(attempt, registration)
        return self._result(
            "enrollment-receipt", attempt, observed, before_send, before_send
        )

    def cancel(self, attempt: EnrollmentAttempt, *, before_send: Callable[[], None]) -> None:
        self._exchange("enrollment-cancel", attempt, self._request(attempt), before_send)

    def _result(
        self,
        purpose: str,
        attempt: EnrollmentAttempt,
        registration: ObservedRegistration,
        before_send: Callable[[], None],
        require_current: Callable[[], None],
        *,
        native: NativeRegistration | None = None,
    ) -> RegisteredPasskey:
        request = self._request(attempt)
        if native is not None:
            request["registration"] = native.wire()
        result = self._exchange(purpose, attempt, request, before_send)
        registered = timestamp(result["registered_at"])
        expires = timestamp(result["receipt_expires_at"])
        now = self._clock()
        if (
            result["credential_id"] != registration.credential_id
            or registered >= attempt.expires_at
            or not registered
            <= now
            < expires
            <= min(attempt.source_expires_at, registered + timedelta(seconds=1800))
        ):
            raise HostedGrantUnavailable("Account passkey receipt does not match")
        require_current()
        return RegisteredPasskey(
            attempt, registration.credential_id, registration.public_key, registered, expires
        )

    def _exchange(
        self,
        purpose: str,
        attempt: EnrollmentAttempt,
        request: dict[str, Any],
        before_send: Callable[[], None],
    ) -> dict[str, Any]:
        if attempt.rp_id != self._rp_id:
            raise HostedGrantUnavailable("Account passkey relying party changed")
        reply = self._client.exchange(
            purpose,
            request,
            expected_key_jkt=attempt.installation_key_jkt,
            before_send=before_send,
        )
        if (reply["enrollment_id"], reply["attempt_id"]) != (
            attempt.enrollment_id,
            attempt.attempt_id,
        ):
            raise HostedGrantUnavailable("Account passkey attempt changed")
        return reply

    @staticmethod
    def _request(attempt: EnrollmentAttempt) -> dict[str, Any]:
        return {
            "profile": ENROLLMENT_PROFILE,
            "enrollment_id": attempt.enrollment_id,
            "attempt_id": attempt.attempt_id,
        }
