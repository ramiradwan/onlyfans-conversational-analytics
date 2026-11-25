"""Account-passkey local record delivery for the trusted desktop owner."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.security.account_passkey import (
    ACCESS_PROFILE,
    BINDING_PROFILE,
    WITHDRAWAL_PROFILE,
    AccountPasskeyNativeClient,
)
from app.security.account_passkey_enrollment import RegisteredPasskey
from app.security.account_passkey_records import (
    MAX_INITIAL_ACCESS_SECONDS,
    AccountPasskeyRecordVerifier,
    BindingScope,
    VerifiedPasskeyRecord,
    require_initial_binding,
    require_withdrawal_binding,
)
from app.security.account_passkey_webauthn import binding_public_key
from app.security.signed_objects import encode, require


class AccountPasskeyLocalRecords:
    """Fetch and verify exact records. The local owner controls their adoption."""

    def __init__(
        self,
        client: AccountPasskeyNativeClient,
        verifier: AccountPasskeyRecordVerifier,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client = client
        self._verifier = verifier
        self._clock = clock

    def read_binding(
        self,
        registration: RegisteredPasskey,
        *,
        before_send: Callable[[], None],
    ) -> VerifiedPasskeyRecord:
        scope = _registration_scope(registration)
        reply = self._client.exchange(
            "binding-read",
            {
                "profile": BINDING_PROFILE,
                "mode": "active",
                "installation_id": scope.installation_id,
                "credential_id": scope.credential_id,
            },
            expected_key_jkt=scope.installation_key_jkt,
            before_send=before_send,
        )
        require(reply["state"] == "active")
        binding = self._verify(reply["record"], BINDING_PROFILE, scope)
        require(
            binding_public_key(binding.payload["credential_public_key_cose"])
            == registration.public_key
        )
        before_send()
        return binding

    def issue_initial_access(
        self,
        registration: RegisteredPasskey,
        binding: VerifiedPasskeyRecord,
        *,
        before_send: Callable[[], None],
    ) -> VerifiedPasskeyRecord:
        attempt = registration.attempt
        scope = _binding_scope(binding)
        require(scope == _registration_scope(registration))
        verified_at = int(registration.registered_at.timestamp())
        source_expires_at = int(attempt.source_expires_at.timestamp())
        now = int(self._clock().timestamp())
        require(verified_at <= now < min(source_expires_at, verified_at + MAX_INITIAL_ACCESS_SECONDS))
        reply = self._client.exchange(
            "initial-local-access",
            {
                "profile": ACCESS_PROFILE,
                "installation_id": scope.installation_id,
                "credential_id": scope.credential_id,
                "binding_sha256": binding.digest,
                "enrollment_id": attempt.enrollment_id,
                "attempt_id": attempt.attempt_id,
                "initial_access_nonce": attempt.initial_access_nonce,
            },
            expected_key_jkt=scope.installation_key_jkt,
            before_send=before_send,
        )
        access = self._verify(reply["record"], ACCESS_PROFILE, scope)
        require_initial_binding(
            access,
            binding,
            enrollment_id=attempt.enrollment_id,
            attempt_id=attempt.attempt_id,
            nonce=attempt.initial_access_nonce,
        )
        require(
            access.payload["verified_at"] == verified_at
            and access.payload["source_expires_at"] <= source_expires_at
        )
        before_send()
        return access

    def read_withdrawal(
        self,
        binding: VerifiedPasskeyRecord,
        *,
        before_send: Callable[[], None],
    ) -> VerifiedPasskeyRecord:
        scope = _binding_scope(binding)
        reply = self._client.exchange(
            "binding-read",
            {
                "profile": BINDING_PROFILE,
                "mode": "withdrawal-only",
                "installation_id": scope.installation_id,
                "credential_id": scope.credential_id,
                "known_binding_sha256": binding.digest,
            },
            expected_key_jkt=scope.installation_key_jkt,
            before_send=before_send,
        )
        require(reply["state"] == "withdrawn")
        withdrawal = self._verify(reply["record"], WITHDRAWAL_PROFILE, scope)
        require_withdrawal_binding(withdrawal, binding)
        before_send()
        return withdrawal

    def acknowledge_withdrawal(
        self,
        withdrawal: VerifiedPasskeyRecord,
        *,
        require_applied: Callable[[VerifiedPasskeyRecord], None],
    ) -> None:
        """The local owner's guard checks the durable withdrawal before every send."""
        value = withdrawal.payload
        require(value["profile"] == WITHDRAWAL_PROFILE)
        self._client.exchange(
            "withdrawal-ack",
            {
                "profile": WITHDRAWAL_PROFILE,
                "installation_id": value["installation_id"],
                "credential_id": value["credential_id"],
                "generation": withdrawal.generation,
                "withdrawal_sha256": withdrawal.digest,
            },
            expected_key_jkt=value["installation_key_jkt"],
            before_send=lambda: require_applied(withdrawal),
        )

    def _verify(self, compact: str, profile: str, scope: BindingScope) -> VerifiedPasskeyRecord:
        return self._verifier.verify(
            compact, profile=profile, scope=scope, now=int(self._clock().timestamp())
        )


def _registration_scope(registration: RegisteredPasskey) -> BindingScope:
    attempt = registration.attempt
    return BindingScope(
        encode(attempt.user_handle),
        attempt.installation_id,
        attempt.installation_key_jkt,
        registration.credential_id,
        attempt.rp_id,
    )


def _binding_scope(binding: VerifiedPasskeyRecord) -> BindingScope:
    p = binding.payload
    require(p["profile"] == BINDING_PROFILE)
    return BindingScope(
        p["sub"], p["installation_id"], p["installation_key_jkt"], p["credential_id"], p["rp_id"]
    )
