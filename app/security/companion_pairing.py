"""Local pairing orchestration; all durable decisions remain in the auth store."""

from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import AuthenticationStateError, SQLiteAuthenticationStore
from app.persistence.companion_pairing import (
    CompanionPairingCASMismatch,
    CompanionPairingPersistence,
    CompanionPairingPersistenceError,
    CompanionPairingState,
    CompanionPairingWindow,
)
from app.security import companion_pairing_proof as proof
from app.security.installation_key import InstallationKeyAuthority, InstallationKeyError
from app.security.local_data_key import LocalDataKeyError, protect_local_secret
from app.security.runtime_policy import (
    RuntimeAuthorizationDenied,
    RuntimePolicy,
    require_identity,
)

NOISE_KEY_PURPOSE = "companion-noise-static/v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_B64 = re.compile(r"[A-Za-z0-9_-]+")
_REQUEST_FIELDS = {
    "type",
    "agent_installation_id",
    "agent_identity_jwk",
    "agent_noise_key",
    "agent_nonce",
}
_CONFIRM_FIELDS = {"type", "pairing_id", "agent_proof"}
_FAILURE_STATES = {"declined", "cancelled", "expired", "revoked"}


class CompanionPairingError(RuntimeError):
    """Public fixed refusal, with no provider, database, or request payload."""

    def __init__(self, code: str = "pairing_state_refused") -> None:
        self.code = (
            code
            if code
            in {
                "pairing_state_refused",
                "pairing_storage_refused",
                "pairing_key_refused",
                "pairing_grant_refused",
                "pairing_message_invalid",
                "pairing_proof_refused",
                "pairing_nonce_refused",
                "pairing_generation_refused",
            }
            else "pairing_state_refused"
        )
        super().__init__(self.code)


def _boundary(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except proof.CompanionPairingProofError as error:
            raise CompanionPairingError(error.code) from None
        except (LocalDataKeyError, InstallationKeyError):
            raise CompanionPairingError("pairing_key_refused") from None
        except sqlite3.DatabaseError:
            raise CompanionPairingError("pairing_storage_refused") from None
        except (
            AuthenticationStateError,
            RuntimeAuthorizationDenied,
            CompanionPairingPersistenceError,
        ):
            raise CompanionPairingError() from None

    return guarded


def encode_pairing_id(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object, size: int) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) != (size * 8 + 5) // 6
        or not _B64.fullmatch(value)
    ):
        raise CompanionPairingError("pairing_message_invalid")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != size or encode_pairing_id(raw) != value:
        raise CompanionPairingError("pairing_message_invalid")
    return raw


def _document(document: object, fields: set[str], kind: str) -> dict:
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("type") != kind
    ):
        raise CompanionPairingError("pairing_message_invalid")
    return document


class CompanionPairingService:
    def __init__(
        self,
        authentication: SQLiteAuthenticationStore,
        authority: InstallationKeyAuthority,
    ) -> None:
        self.authentication = authentication
        self.persistence = CompanionPairingPersistence(authentication)
        self.authority = authority

    def _now(self) -> datetime:
        return self.authentication._now()

    @staticmethod
    def _identity(policy: RuntimePolicy):
        identity = require_identity(policy)
        if not identity.session_id or identity.role not in {"creator", "operator"}:
            raise CompanionPairingError()
        return identity

    @_boundary
    def open(self, policy: RuntimePolicy, creator_account_id: str) -> dict:
        identity = self._identity(policy)
        if creator_account_id != identity.creator_account_id:
            raise CompanionPairingError()
        # Candidate private material is protected before any database write.
        # The store rechecks the authenticated account after key creation.
        key = X25519PrivateKey.generate()
        protected = protect_local_secret(
            key.private_bytes_raw(), purpose=NOISE_KEY_PURPOSE
        )
        public = key.public_key().public_bytes_raw()
        del key
        now = self._now()
        window = self.persistence.open_authorized_window(
            session_id=identity.session_id,
            creator_account_id=creator_account_id,
            pairing_id=secrets.token_bytes(32),
            opened_at=now,
            expires_at=now + timedelta(seconds=300),
            brain_nonce=secrets.token_bytes(32),
            brain_noise_public_key=public,
            wrapped_brain_noise_private_key=protected,
        )
        return self._public(window)

    @_boundary
    def status(self, policy: RuntimePolicy, pairing_id: str) -> dict:
        identity = self._identity(policy)
        record = self.persistence.authorized_record(
            identity.session_id, _decode(pairing_id, 32)
        )
        return self._public(record)

    @_boundary
    def confirm(self, policy: RuntimePolicy, pairing_id: str, version: int) -> dict:
        identity = self._identity(policy)
        pin = self.persistence.confirm_and_admit(
            _decode(pairing_id, 32),
            expected_version=version,
            confirmation_principal_id=identity.principal_id,
            confirmation_session_id=identity.session_id,
            confirmed_at=self._now(),
        )
        return self._public(pin)

    @_boundary
    def cancel(
        self,
        policy: RuntimePolicy,
        pairing_id: str,
        version: int,
        decline: bool = False,
    ) -> dict:
        identity = self._identity(policy)
        identifier = _decode(pairing_id, 32)
        try:
            window = self.persistence.cancel_authorized_window(
                identifier,
                expected_version=version,
                session_id=identity.session_id,
                decline=decline,
            )
            return self._public(window)
        except CompanionPairingCASMismatch:
            # Approval can win the transaction just before the operator cancels.
            # Fence that exact pin, without touching a replacement pairing ID.
            pin = self.persistence.authorized_record(identity.session_id, identifier)
            if isinstance(pin, CompanionPairingWindow) or version > pin.version:
                raise
            if not self.persistence.revoke_companion_pin(
                identifier, session_id=identity.session_id, expected_version=pin.version
            ):
                raise CompanionPairingError()
            return {**self._public(pin), "state": "cancelled"}

    @_boundary
    def claim(self) -> CompanionPairingWindow:
        return self.persistence.claim_request()

    @_boundary
    def offer(self, window: CompanionPairingWindow, document: dict) -> dict:
        request = _document(document, _REQUEST_FIELDS, "pair.request")
        if not isinstance(request["agent_installation_id"], str) or not _ID.fullmatch(
            request["agent_installation_id"]
        ):
            raise CompanionPairingError("pairing_message_invalid")
        installation = self.authentication.verified_grant(
            window.installation_grant_reference_id
        )
        binding = self.authentication.verified_grant(
            window.creator_account_binding_reference_id
        )
        key = self.authentication.installation_key_reference()
        if installation is None or binding is None or key is None:
            raise CompanionPairingError("pairing_grant_refused")
        transcript = proof.build_pairing_transcript(
            installation_grant=installation,
            creator_account_binding=binding,
            installation_key=key,
            pairing_id=window.pairing_id,
            generation=window.generation,
            agent_installation_id=request["agent_installation_id"],
            agent_identity_jwk=request["agent_identity_jwk"],
            agent_noise_key=_decode(request["agent_noise_key"], 32),
            brain_noise_key=window.brain_noise_public_key,
            agent_nonce=_decode(request["agent_nonce"], 32),
            brain_nonce=window.brain_nonce,
        )
        signed = proof.sign_brain_pairing_proof(self.authority, key, transcript)
        # Signing may complete after timeout or cancellation. This CAS rechecks
        # the claimed version, current grants and deadline before an offer leaves.
        self.persistence.offer_window(
            window.pairing_id,
            expected_version=window.version,
            agent_installation_id=request["agent_installation_id"],
            agent_identity_jwk=json.dumps(
                request["agent_identity_jwk"], sort_keys=True, separators=(",", ":")
            ),
            agent_identity_thumbprint=transcript.agent_identity_key_jkt,
            agent_noise_public_key=transcript.agent_noise_key,
            agent_nonce=transcript.agent_nonce,
            pairing_digest=proof.pairing_digest(transcript),
            grant_digest=transcript.grant_digest,
            installation_grant_reference_id=installation.reference_id,
            creator_account_binding_reference_id=binding.reference_id,
            offered_at=self._now(),
        )
        installation_jwk = json.loads(key.public_key_jwk)
        installation_jwk.pop("kid", None)
        return {
            "type": "pair.offer",
            "pairing_id": encode_pairing_id(window.pairing_id),
            "generation": window.generation,
            "creator_account_id": window.creator_account_id,
            "brain_noise_key": encode_pairing_id(window.brain_noise_public_key),
            "brain_nonce": encode_pairing_id(window.brain_nonce),
            "installation_jwk": installation_jwk,
            "installation_grant": installation.compact_jws,
            "creator_account_binding": binding.compact_jws,
            "brain_proof": signed,
        }

    @_boundary
    def confirm_agent(self, pairing_id: bytes, document: dict) -> None:
        request = _document(document, _CONFIRM_FIELDS, "pair.confirm")
        if _decode(request["pairing_id"], 32) != pairing_id:
            raise CompanionPairingError("pairing_message_invalid")
        window = self.persistence.window(pairing_id)
        if window is None or window.state is not CompanionPairingState.OFFERED:
            raise CompanionPairingError()
        if not proof.verify_pairing_proof(
            json.loads(window.agent_identity_jwk),
            "agent",
            window.pairing_digest,
            request["agent_proof"],
        ):
            raise CompanionPairingError("pairing_proof_refused")
        self.persistence.await_confirmation(
            pairing_id, expected_version=window.version, at=self._now()
        )

    @_boundary
    def outcome(self, pairing_id: bytes) -> str | None:
        window = self.persistence.window(pairing_id)
        if window is None:
            return (
                "confirmed"
                if self.persistence.companion_pin(pairing_id) is not None
                else "cancelled"
            )
        state = window.state.value
        if state in _FAILURE_STATES:
            return state if state != "revoked" else "cancelled"
        if window.expires_at <= self._now():
            self.persistence.terminate_window(
                pairing_id,
                expected_version=window.version,
                expected_state=window.state,
                terminal_state=CompanionPairingState.EXPIRED,
                at=self._now(),
                reason="expired",
            )
            return "expired"
        return None

    @_boundary
    def abort(self, pairing_id: bytes) -> None:
        self.persistence.abort_candidate(pairing_id)

    @staticmethod
    def _public(record) -> dict:
        window = isinstance(record, CompanionPairingWindow)
        state = record.state.value if window else "admitted"
        showing_code = state == "awaiting_confirmation"
        return {
            "pairing_id": encode_pairing_id(record.pairing_id),
            "creator_account_id": record.creator_account_id,
            "generation": record.generation,
            "version": record.version,
            "state": state,
            "expires_at": record.expires_at.astimezone(timezone.utc).isoformat(),
            "comparison_code": (
                proof.comparison_code(record.pairing_digest) if showing_code else None
            ),
            "agent_identity_thumbprint": (
                record.agent_identity_thumbprint if showing_code else None
            ),
        }
