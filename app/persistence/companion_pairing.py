"""Durable staging for local companion pairing before Agent admission."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    AuthenticationStateError,
    CompanionSessionBinding,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    _verified_grant_reference,
)
from app.security.runtime_policy import RuntimePolicy

_BYTES32: Final = 32
_MAX_PROTECTED_KEY_BYTES: Final = 4096
_MAX_JWK_BYTES: Final = 4096
_MAX_THUMBPRINT_BYTES: Final = 256
_MAX_REASON_BYTES: Final = 256
_MAX_GENERATION: Final = (1 << 53) - 1
_MAX_WINDOW_LIFETIME: Final = timedelta(seconds=300)


class CompanionPairingState(str, Enum):
    OPEN = "open"
    OFFERED = "offered"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REVOKED = "revoked"


_LIVE_STATES: Final = frozenset(
    {
        CompanionPairingState.OPEN,
        CompanionPairingState.OFFERED,
        CompanionPairingState.AWAITING_CONFIRMATION,
    }
)
_FAILURE_TERMINAL_STATES: Final = frozenset(
    {
        CompanionPairingState.DECLINED,
        CompanionPairingState.CANCELLED,
        CompanionPairingState.EXPIRED,
        CompanionPairingState.REVOKED,
    }
)


class CompanionPairingPersistenceError(RuntimeError):
    """Base error for companion-pairing staging persistence."""


class CompanionPairingConflict(CompanionPairingPersistenceError):
    """A live staging window already occupies the installation."""


class CompanionPairingCASMismatch(CompanionPairingPersistenceError):
    """A versioned state transition no longer matches durable state."""


class CompanionPairingGrantUnavailable(CompanionPairingPersistenceError):
    """Frozen pairing grants are not retained and current."""


class CompanionPairingGenerationExhausted(CompanionPairingPersistenceError):
    """No further generation can be represented safely."""


class CompanionPairingStateError(CompanionPairingPersistenceError):
    """A requested transition violates the pairing lifecycle."""


@dataclass(frozen=True, slots=True)
class CompanionPairingWindow:
    pairing_id: bytes
    installation_id: str
    creator_account_id: str
    generation: int
    state: CompanionPairingState
    version: int
    opened_at: datetime
    expires_at: datetime
    brain_nonce: bytes
    brain_noise_public_key: bytes
    wrapped_brain_noise_private_key: bytes | None = field(repr=False, compare=False)
    agent_installation_id: str | None = None
    agent_identity_jwk: str | None = None
    agent_identity_thumbprint: str | None = None
    agent_noise_public_key: bytes | None = None
    agent_nonce: bytes | None = None
    pairing_digest: bytes | None = None
    grant_digest: bytes | None = None
    installation_grant_reference_id: str | None = None
    creator_account_binding_reference_id: str | None = None
    offered_at: datetime | None = None
    awaiting_confirmation_at: datetime | None = None
    confirmation_principal_id: str | None = None
    confirmation_session_id: str | None = None
    confirmed_at: datetime | None = None
    terminal_at: datetime | None = None
    terminal_reason: str | None = None
    request_claimed_at: datetime | None = None
    opening_principal_id: str | None = None
    opening_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompanionPin:
    pairing_id: bytes
    generation: int
    version: int
    installation_id: str
    organization_id: str
    installation_key_id: str
    installation_key_jkt: str
    creator_account_id: str
    agent_installation_id: str
    agent_identity_jwk: str
    agent_identity_thumbprint: str
    agent_noise_public_key: bytes
    brain_noise_public_key: bytes
    wrapped_brain_noise_private_key: bytes = field(repr=False, compare=False)
    pairing_digest: bytes
    grant_digest: bytes
    installation_grant_reference_id: str
    creator_account_binding_reference_id: str
    confirmation_principal_id: str
    confirmation_session_id: str
    confirmed_at: datetime
    opened_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompanionSessionSnapshot:
    pin: CompanionPin
    installation_grant: VerifiedGrantReference = field(repr=False, compare=False)
    creator_account_binding: VerifiedGrantReference = field(repr=False, compare=False)
    expires_at: datetime

    @property
    def authorization(self) -> dict[str, str]:
        """Secret record: the caller may only send it inside the Noise session."""
        return {
            "type": "session.authorization",
            "installation_grant": self.installation_grant.compact_jws,
            "creator_account_binding": self.creator_account_binding.compact_jws,
        }

    def binding(self, session_id: str) -> CompanionSessionBinding:
        return CompanionSessionBinding(
            session_id=session_id,
            pairing_id=_encoded_pairing_id(self.pin.pairing_id),
            generation=self.pin.generation,
            pairing_digest=self.pin.pairing_digest,
            principal_id="agent:" + self.pin.agent_installation_id,
            creator_account_id=self.pin.creator_account_id,
            agent_installation_id=self.pin.agent_installation_id,
            grant_reference_ids=(
                self.installation_grant.reference_id,
                self.creator_account_binding.reference_id,
            ),
            expires_at=self.expires_at,
        )


class CompanionPairingPersistence:
    """CAS persistence for pre-admission companion pairing state."""

    def __init__(self, authentication: SQLiteAuthenticationStore) -> None:
        self.authentication = authentication
        self.database = authentication.database

    def allocate_generation(self, installation_id: str) -> int:
        _require_text(installation_id, name="installation_id")
        with self.database.transaction() as connection:
            return self._allocate_generation(connection, installation_id)

    def highest_generation(self, installation_id: str) -> int | None:
        _require_text(installation_id, name="installation_id")
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT highest_generation
                FROM companion_pairing_generations
                WHERE installation_id = ?
                """,
                (installation_id,),
            ).fetchone()
        return None if row is None else int(row["highest_generation"])

    def open_window(
        self,
        *,
        pairing_id: bytes,
        installation_id: str,
        creator_account_id: str,
        opened_at: datetime,
        expires_at: datetime,
        brain_nonce: bytes,
        brain_noise_public_key: bytes,
        wrapped_brain_noise_private_key: bytes,
    ) -> CompanionPairingWindow:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        brain_nonce = _require_bytes32(brain_nonce, name="brain_nonce")
        brain_noise_public_key = _require_bytes32(
            brain_noise_public_key, name="brain_noise_public_key"
        )
        wrapped_brain_noise_private_key = _require_bounded_blob(
            wrapped_brain_noise_private_key,
            name="wrapped_brain_noise_private_key",
            maximum=_MAX_PROTECTED_KEY_BYTES,
        )
        _require_text(installation_id, name="installation_id")
        _require_text(creator_account_id, name="creator_account_id")
        _require_interval(opened_at, expires_at)
        if expires_at - opened_at > _MAX_WINDOW_LIFETIME:
            raise ValueError("pairing window exceeds the contract lifetime")

        with self.database.transaction() as connection:
            now = self.authentication._now()
            if opened_at > now or expires_at <= now:
                raise ValueError("pairing window must be live when opened")
            self._expire_live_window_if_due(connection, installation_id, now)
            existing = connection.execute(
                """
                SELECT 1
                FROM companion_pairing_windows
                WHERE installation_id = ?
                  AND state IN ('open', 'offered', 'awaiting_confirmation')
                """,
                (installation_id,),
            ).fetchone()
            if existing is not None:
                raise CompanionPairingConflict(
                    "A live companion pairing window already exists"
                )

            generation = self._allocate_generation(connection, installation_id)
            try:
                connection.execute(
                    """
                    INSERT INTO companion_pairing_windows (
                        pairing_id, installation_id, creator_account_id,
                        generation, state, version, opened_at, expires_at,
                        brain_nonce, brain_noise_public_key,
                        wrapped_brain_noise_private_key
                    ) VALUES (?, ?, ?, ?, 'open', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        pairing_id,
                        installation_id,
                        creator_account_id,
                        generation,
                        _time_text(opened_at),
                        _time_text(expires_at),
                        brain_nonce,
                        brain_noise_public_key,
                        wrapped_brain_noise_private_key,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CompanionPairingConflict(
                    "Companion pairing window creation conflicted"
                ) from error
            return self._window_in_transaction(connection, pairing_id)

    def window(self, pairing_id: bytes) -> CompanionPairingWindow | None:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM companion_pairing_windows WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
        return None if row is None else _window(row)

    def open_authorized_window(
        self,
        *,
        session_id: str,
        creator_account_id: str,
        pairing_id: bytes,
        opened_at: datetime,
        expires_at: datetime,
        brain_nonce: bytes,
        brain_noise_public_key: bytes,
        wrapped_brain_noise_private_key: bytes,
    ) -> CompanionPairingWindow:
        """Open and freeze authority in the same transaction as the generation."""
        _require_text(session_id, name="session_id")
        _require_text(creator_account_id, name="creator_account_id")
        _require_bytes32(pairing_id, name="pairing_id")
        _require_bytes32(brain_nonce, name="brain_nonce")
        _require_bytes32(brain_noise_public_key, name="brain_noise_public_key")
        _require_bounded_blob(
            wrapped_brain_noise_private_key,
            name="wrapped_brain_noise_private_key",
            maximum=_MAX_PROTECTED_KEY_BYTES,
        )
        _require_interval(opened_at, expires_at)
        if expires_at - opened_at > _MAX_WINDOW_LIFETIME:
            raise ValueError("pairing window exceeds the contract lifetime")
        with self.database.transaction() as connection:
            now = self.authentication._now()
            if opened_at > now or expires_at <= now:
                raise ValueError("pairing window must be live when opened")
            session, credential = self._require_bridge_authority(
                connection, session_id, creator_account_id, now
            )
            installation_id = str(credential["installation_id"])
            self._require_approved_account(
                connection, creator_account_id, installation_id
            )
            refs = self.authentication._session_grants(connection, session_id)
            selected: dict[str, str] = {}
            for reference_id in refs:
                grant = connection.execute(
                    "SELECT grant_type, compact_jws FROM verified_grant_references "
                    "WHERE reference_id = ?",
                    (reference_id,),
                ).fetchone()
                if (
                    grant is not None
                    and grant["grant_type"]
                    in {"installation_grant", "creator_account_binding"}
                    and grant["compact_jws"] is not None
                ):
                    grant_type = str(grant["grant_type"])
                    if grant_type in selected:
                        raise CompanionPairingGrantUnavailable(
                            "Companion pairing grant authority is ambiguous"
                        )
                    selected[grant_type] = reference_id
            if set(selected) != {"installation_grant", "creator_account_binding"}:
                raise CompanionPairingGrantUnavailable(
                    "Companion pairing grants are not current and retained"
                )
            installation_ref = selected["installation_grant"]
            account_ref = selected["creator_account_binding"]
            self._require_pairing_grants(
                connection,
                installation_grant_reference_id=installation_ref,
                creator_account_binding_reference_id=account_ref,
                installation_id=installation_id,
                creator_account_id=creator_account_id,
                now=now,
            )
            grant = connection.execute(
                "SELECT organization_id FROM verified_grant_references WHERE reference_id = ?",
                (installation_ref,),
            ).fetchone()
            self._require_approved_account(
                connection,
                creator_account_id,
                installation_id,
                organization_id=str(grant["organization_id"]),
            )
            self._expire_live_window_if_due(connection, installation_id, now)
            if (
                connection.execute(
                    "SELECT 1 FROM companion_pairing_windows WHERE installation_id = ? "
                    "AND state IN ('open', 'offered', 'awaiting_confirmation')",
                    (installation_id,),
                ).fetchone()
                is not None
            ):
                raise CompanionPairingConflict(
                    "A live companion pairing window already exists"
                )
            generation = self._allocate_generation(connection, installation_id)
            try:
                connection.execute(
                    """INSERT INTO companion_pairing_windows (
                        pairing_id, installation_id, creator_account_id, generation,
                        state, version, opened_at, expires_at, brain_nonce,
                        brain_noise_public_key, wrapped_brain_noise_private_key,
                        installation_grant_reference_id, creator_account_binding_reference_id,
                        opening_principal_id, opening_session_id
                    ) VALUES (?, ?, ?, ?, 'open', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pairing_id,
                        installation_id,
                        creator_account_id,
                        generation,
                        _time_text(opened_at),
                        _time_text(expires_at),
                        brain_nonce,
                        brain_noise_public_key,
                        wrapped_brain_noise_private_key,
                        installation_ref,
                        account_ref,
                        session["principal_id"],
                        session_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise CompanionPairingConflict(
                    "Companion pairing window creation conflicted"
                ) from None
            return self._window_in_transaction(connection, pairing_id)

    def claim_request(
        self, installation_id: str | None = None
    ) -> CompanionPairingWindow:
        """Claim once; a competing request commits cancellation before refusing."""
        conflict = False
        with self.database.transaction() as connection:
            now = self.authentication._now()
            rows = connection.execute(
                "SELECT * FROM companion_pairing_windows "
                "WHERE state IN ('open', 'offered', 'awaiting_confirmation') "
                + ("AND installation_id = ?" if installation_id is not None else ""),
                (installation_id,) if installation_id is not None else (),
            ).fetchall()
            if len(rows) != 1:
                raise CompanionPairingStateError(
                    "No unique companion pairing window is open"
                )
            row = rows[0]
            if _parse_time(str(row["expires_at"])) <= now:
                self._terminate_row(connection, row, CompanionPairingState.EXPIRED, now)
                conflict = True
            elif row["request_claimed_at"] is not None or row["state"] != "open":
                self._terminate_row(
                    connection, row, CompanionPairingState.CANCELLED, now
                )
                conflict = True
            else:
                self._require_frozen_pairing_grants(connection, row, now)
                connection.execute(
                    "UPDATE companion_pairing_windows SET request_claimed_at = ?, "
                    "version = version + 1 WHERE pairing_id = ? AND version = ?",
                    (_time_text(now), row["pairing_id"], row["version"]),
                )
            window = self._window_in_transaction(connection, bytes(row["pairing_id"]))
        if conflict:
            raise CompanionPairingConflict("Companion pairing request was refused")
        return window

    def offer_window(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        agent_installation_id: str,
        agent_identity_jwk: str,
        agent_identity_thumbprint: str,
        agent_noise_public_key: bytes,
        agent_nonce: bytes,
        pairing_digest: bytes,
        grant_digest: bytes,
        installation_grant_reference_id: str,
        creator_account_binding_reference_id: str,
        offered_at: datetime,
    ) -> CompanionPairingWindow:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        _require_text(agent_installation_id, name="agent_installation_id")
        _require_bounded_text(
            agent_identity_jwk, name="agent_identity_jwk", maximum=_MAX_JWK_BYTES
        )
        _require_bounded_text(
            agent_identity_thumbprint,
            name="agent_identity_thumbprint",
            maximum=_MAX_THUMBPRINT_BYTES,
        )
        agent_noise_public_key = _require_bytes32(
            agent_noise_public_key, name="agent_noise_public_key"
        )
        agent_nonce = _require_bytes32(agent_nonce, name="agent_nonce")
        pairing_digest = _require_bytes32(pairing_digest, name="pairing_digest")
        grant_digest = _require_bytes32(grant_digest, name="grant_digest")
        _require_text(
            installation_grant_reference_id,
            name="installation_grant_reference_id",
        )
        _require_text(
            creator_account_binding_reference_id,
            name="creator_account_binding_reference_id",
        )
        _time_text(offered_at)

        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = self._require_expected_window(
                connection,
                pairing_id,
                expected_version=expected_version,
                expected_state=CompanionPairingState.OPEN,
            )
            if (
                row["opening_session_id"] is not None
                and row["request_claimed_at"] is None
            ):
                raise CompanionPairingStateError(
                    "Companion pairing request was not claimed"
                )
            if row["installation_grant_reference_id"] is not None and (
                row["installation_grant_reference_id"]
                != installation_grant_reference_id
                or row["creator_account_binding_reference_id"]
                != creator_account_binding_reference_id
            ):
                raise CompanionPairingGrantUnavailable(
                    "Companion pairing grant selection changed"
                )
            self._require_transition_time(
                row, offered_at, lower_column="opened_at", now=now
            )
            self._require_not_expired(row, now)
            if row["opening_session_id"] is not None:
                self._require_frozen_pairing_grants(connection, row, now)
            self._require_pairing_grants(
                connection,
                installation_grant_reference_id=installation_grant_reference_id,
                creator_account_binding_reference_id=(
                    creator_account_binding_reference_id
                ),
                installation_id=str(row["installation_id"]),
                creator_account_id=str(row["creator_account_id"]),
                now=now,
            )
            cursor = connection.execute(
                """
                UPDATE companion_pairing_windows SET
                    state = 'offered',
                    version = version + 1,
                    agent_installation_id = ?,
                    agent_identity_jwk = ?,
                    agent_identity_thumbprint = ?,
                    agent_noise_public_key = ?,
                    agent_nonce = ?,
                    pairing_digest = ?,
                    grant_digest = ?,
                    installation_grant_reference_id = ?,
                    creator_account_binding_reference_id = ?,
                    offered_at = ?
                WHERE pairing_id = ? AND version = ? AND state = 'open'
                """,
                (
                    agent_installation_id,
                    agent_identity_jwk,
                    agent_identity_thumbprint,
                    agent_noise_public_key,
                    agent_nonce,
                    pairing_digest,
                    grant_digest,
                    installation_grant_reference_id,
                    creator_account_binding_reference_id,
                    _time_text(offered_at),
                    pairing_id,
                    expected_version,
                ),
            )
            self._require_updated(cursor)
            return self._window_in_transaction(connection, pairing_id)

    def await_confirmation(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        at: datetime,
    ) -> CompanionPairingWindow:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        _time_text(at)
        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = self._require_expected_window(
                connection,
                pairing_id,
                expected_version=expected_version,
                expected_state=CompanionPairingState.OFFERED,
            )
            self._require_transition_time(row, at, lower_column="offered_at", now=now)
            self._require_not_expired(row, now)
            self._require_frozen_pairing_grants(connection, row, now)
            cursor = connection.execute(
                """
                UPDATE companion_pairing_windows SET
                    state = 'awaiting_confirmation',
                    version = version + 1,
                    awaiting_confirmation_at = ?
                WHERE pairing_id = ? AND version = ? AND state = 'offered'
                """,
                (_time_text(at), pairing_id, expected_version),
            )
            self._require_updated(cursor)
            return self._window_in_transaction(connection, pairing_id)

    def confirm_window(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        confirmation_principal_id: str,
        confirmation_session_id: str,
        confirmed_at: datetime,
    ) -> CompanionPairingWindow:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        _require_text(confirmation_principal_id, name="confirmation_principal_id")
        _require_text(confirmation_session_id, name="confirmation_session_id")
        _time_text(confirmed_at)
        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = self._require_expected_window(
                connection,
                pairing_id,
                expected_version=expected_version,
                expected_state=CompanionPairingState.AWAITING_CONFIRMATION,
            )
            self._require_transition_time(
                row,
                confirmed_at,
                lower_column="awaiting_confirmation_at",
                now=now,
            )
            self._require_not_expired(row, now)
            self._require_frozen_pairing_grants(connection, row, now)
            try:
                session = self.authentication._require_session_current(
                    connection, confirmation_session_id, now
                )
            except AuthenticationStateError:
                raise CompanionPairingStateError(
                    "Companion pairing requires a current Bridge session"
                ) from None
            if (
                session["principal_id"] != confirmation_principal_id
                or session["creator_account_id"] != row["creator_account_id"]
                or session["role"] not in {"creator", "operator"}
            ):
                raise CompanionPairingStateError(
                    "Companion pairing confirmation authority does not match"
                )
            credential = connection.execute(
                "SELECT external_issuer, external_subject, installation_id "
                "FROM webauthn_credentials WHERE credential_id = ?",
                (session["credential_id"],),
            ).fetchone()
            grant = connection.execute(
                "SELECT issuer, subject, installation_id FROM verified_grant_references "
                "WHERE reference_id = ?",
                (row["installation_grant_reference_id"],),
            ).fetchone()
            if credential is None or any(
                credential[local] != grant[hosted]
                for local, hosted in (
                    ("external_issuer", "issuer"),
                    ("external_subject", "subject"),
                    ("installation_id", "installation_id"),
                )
            ):
                raise CompanionPairingStateError(
                    "Companion pairing confirmation identity does not match"
                )
            cursor = connection.execute(
                """
                UPDATE companion_pairing_windows SET
                    state = 'confirmed',
                    version = version + 1,
                    confirmation_principal_id = ?,
                    confirmation_session_id = ?,
                    confirmed_at = ?
                WHERE pairing_id = ?
                  AND version = ?
                  AND state = 'awaiting_confirmation'
                """,
                (
                    confirmation_principal_id,
                    confirmation_session_id,
                    _time_text(confirmed_at),
                    pairing_id,
                    expected_version,
                ),
            )
            self._require_updated(cursor)
            return self._window_in_transaction(connection, pairing_id)

    def confirm_and_admit(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        confirmation_principal_id: str,
        confirmation_session_id: str,
        confirmed_at: datetime,
    ) -> CompanionPin:
        """Commit the confirmed pin and erase staging in one authority transaction."""
        _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        _time_text(confirmed_at)
        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = self._require_expected_window(
                connection,
                pairing_id,
                expected_version=expected_version,
                expected_state=CompanionPairingState.AWAITING_CONFIRMATION,
            )
            self._require_transition_time(
                row,
                confirmed_at,
                lower_column="awaiting_confirmation_at",
                now=now,
            )
            self._require_not_expired(row, now)
            self._require_frozen_pairing_grants(connection, row, now)
            session, credential = self._require_bridge_authority(
                connection,
                confirmation_session_id,
                str(row["creator_account_id"]),
                now,
                installation_id=str(row["installation_id"]),
            )
            if session["principal_id"] != confirmation_principal_id:
                raise CompanionPairingStateError(
                    "Companion pairing confirmation authority does not match"
                )
            self._require_approved_account(
                connection, str(row["creator_account_id"]), str(row["installation_id"])
            )
            grant = connection.execute(
                "SELECT * FROM verified_grant_references WHERE reference_id = ?",
                (row["installation_grant_reference_id"],),
            ).fetchone()
            self._require_approved_account(
                connection,
                str(row["creator_account_id"]),
                str(row["installation_id"]),
                organization_id=str(grant["organization_id"]),
            )
            if any(
                credential[local] != grant[hosted]
                for local, hosted in (
                    ("external_issuer", "issuer"),
                    ("external_subject", "subject"),
                    ("installation_id", "installation_id"),
                )
            ):
                raise CompanionPairingStateError(
                    "Companion pairing confirmation identity does not match"
                )
            try:
                self.authentication._snapshot_revocations(
                    connection,
                    (
                        RevocationKey(
                            RevocationScopeType.INSTALLATION,
                            str(row["agent_installation_id"]),
                        ),
                        RevocationKey(
                            RevocationScopeType.PRINCIPAL,
                            "agent:" + str(row["agent_installation_id"]),
                        ),
                    ),
                )
            except AuthenticationStateError:
                raise CompanionPairingStateError(
                    "Companion pairing Agent identity is revoked"
                ) from None
            old = connection.execute(
                "SELECT pairing_id FROM agent_pairings WHERE agent_installation_id = ? "
                "AND creator_account_id = ? AND revoked_at IS NULL",
                (row["agent_installation_id"], row["creator_account_id"]),
            ).fetchall()
            for previous in old:
                self.authentication._revoke_in_transaction(
                    connection,
                    RevocationKey(
                        RevocationScopeType.AGENT_PAIRING, str(previous["pairing_id"])
                    ),
                    reason="companion_replaced",
                )
            identifier = _encoded_pairing_id(pairing_id)
            references = (
                str(row["installation_grant_reference_id"]),
                str(row["creator_account_binding_reference_id"]),
            )
            connection.execute(
                """INSERT INTO agent_pairings (
                    pairing_id, key_id, principal_id, creator_account_id,
                    agent_installation_id, external_issuer, external_subject,
                    installation_id, public_key, key_fingerprint, created_at,
                    pairing_generation, pairing_digest, agent_noise_static_public_key,
                    brain_noise_static_public_key, protected_brain_noise_static_private_key,
                    confirmation_principal_id, confirmation_session_id, confirmed_at,
                    companion_organization_id, companion_installation_key_id,
                    companion_installation_key_jkt, companion_grant_digest,
                    companion_window_version, companion_window_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    identifier,
                    "agent:" + str(row["agent_installation_id"]),
                    row["creator_account_id"],
                    row["agent_installation_id"],
                    grant["issuer"],
                    grant["subject"],
                    row["installation_id"],
                    str(row["agent_identity_jwk"]).encode("utf-8"),
                    row["agent_identity_thumbprint"],
                    row["opened_at"],
                    row["generation"],
                    row["pairing_digest"],
                    row["agent_noise_public_key"],
                    row["brain_noise_public_key"],
                    row["wrapped_brain_noise_private_key"],
                    confirmation_principal_id,
                    confirmation_session_id,
                    _time_text(confirmed_at),
                    grant["organization_id"],
                    grant["installation_key_id"],
                    grant["installation_key_jkt"],
                    row["grant_digest"],
                    expected_version + 1,
                    row["expires_at"],
                ),
            )
            connection.executemany(
                "INSERT INTO agent_pairing_grants(pairing_id, grant_reference_id) VALUES (?, ?)",
                ((identifier, reference) for reference in references),
            )
            self.authentication._ensure_scope(
                connection, RevocationKey(RevocationScopeType.AGENT_PAIRING, identifier)
            )
            # The confirmed pin is the durable result. Keeping a staging key or a
            # mutable confirmed window would allow a second admission or late CAS.
            deleted = connection.execute(
                "DELETE FROM companion_pairing_windows WHERE pairing_id = ? AND version = ? "
                "AND state = 'awaiting_confirmation'",
                (pairing_id, expected_version),
            )
            self._require_updated(deleted)
            self.authentication._increment_authorization_epoch(connection)
            return self._pin_in_transaction(connection, pairing_id)

    def companion_pin(self, pairing_id: bytes) -> CompanionPin | None:
        _require_bytes32(pairing_id, name="pairing_id")
        with self.database.read() as connection:
            return self._optional_pin_in_transaction(connection, pairing_id)

    def authorized_pins(self, session_id: str) -> tuple[CompanionPin, ...]:
        """Return at most sixteen current pins within the Bridge account scope."""
        with self.database.transaction() as connection:
            now = self.authentication._now()
            try:
                session = self.authentication._require_session_current(
                    connection, session_id, now
                )
            except AuthenticationStateError:
                raise CompanionPairingStateError(
                    "Companion pairing requires a current Bridge session"
                ) from None
            _, credential = self._require_bridge_authority(
                connection, session_id, str(session["creator_account_id"]), now
            )
            rows = connection.execute(
                "SELECT pairing_id FROM agent_pairings WHERE creator_account_id = ? AND installation_id = ? "
                "AND pairing_generation IS NOT NULL AND revoked_at IS NULL "
                "AND protected_brain_noise_static_private_key IS NOT NULL "
                "ORDER BY confirmed_at, pairing_id LIMIT 16",
                (session["creator_account_id"], credential["installation_id"]),
            ).fetchall()
            return tuple(
                self._pin_in_transaction(
                    connection, base64.urlsafe_b64decode(str(row["pairing_id"]) + "=")
                )
                for row in rows
            )

    def session_authority(self, pairing_id: bytes) -> CompanionSessionSnapshot:
        """Select current retained grants for the pin without changing its identity."""
        _require_bytes32(pairing_id, name="pairing_id")
        with self.database.transaction() as connection:
            now = self.authentication._now()
            pin = self._pin_in_transaction(connection, pairing_id)
            parent = connection.execute(
                "SELECT external_issuer, external_subject FROM agent_pairings WHERE pairing_id = ?",
                (_encoded_pairing_id(pairing_id),),
            ).fetchone()
            selected: dict[str, VerifiedGrantReference] = {}
            for kind in ("installation_grant", "creator_account_binding"):
                grant = connection.execute(
                    "SELECT * FROM verified_grant_references WHERE grant_type = ? "
                    "AND installation_id = ? AND organization_id = ? AND installation_key_id = ? "
                    "AND installation_key_jkt = ? AND issuer = ? AND subject = ? "
                    "AND creator_account_id IS ? AND compact_jws IS NOT NULL "
                    "AND revoked_at IS NULL AND valid_from <= ? AND expires_at > ? "
                    "ORDER BY verified_at DESC, reference_id DESC LIMIT 1",
                    (
                        kind,
                        pin.installation_id,
                        pin.organization_id,
                        pin.installation_key_id,
                        pin.installation_key_jkt,
                        parent["external_issuer"],
                        parent["external_subject"],
                        (
                            pin.creator_account_id
                            if kind == "creator_account_binding"
                            else None
                        ),
                        _time_text(now),
                        _time_text(now),
                    ),
                ).fetchone()
                if grant is None:
                    raise CompanionPairingGrantUnavailable(
                        "Companion session grants are unavailable"
                    )
                selected[kind] = _verified_grant_reference(grant)
            snapshot = CompanionSessionSnapshot(
                pin=pin,
                installation_grant=selected["installation_grant"],
                creator_account_binding=selected["creator_account_binding"],
                expires_at=min(
                    now + timedelta(seconds=900),
                    *(grant.expires_at for grant in selected.values()),
                ),
            )
            self.authentication._require_companion_session_current(
                connection, snapshot.binding("pending-handshake"), now
            )
            return snapshot

    def refresh_session_policy(
        self, snapshot: CompanionSessionSnapshot, session_id: str
    ) -> RuntimePolicy:
        return self.authentication.companion_session_policy(
            snapshot.binding(session_id)
        )

    def authorized_record(
        self, session_id: str, pairing_id: bytes
    ) -> CompanionPairingWindow | CompanionPin:
        _require_bytes32(pairing_id, name="pairing_id")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_pairing_windows WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            record = (
                _window(row)
                if row is not None
                else self._optional_pin_in_transaction(connection, pairing_id)
            )
            if record is None:
                raise CompanionPairingStateError("Companion pairing is unavailable")
            now = self.authentication._now()
            self._require_bridge_authority(
                connection,
                session_id,
                record.creator_account_id,
                now,
                installation_id=record.installation_id,
            )
            if (
                row is not None
                and record.state in _LIVE_STATES
                and record.expires_at <= now
            ):
                self._terminate_row(connection, row, CompanionPairingState.EXPIRED, now)
                return self._window_in_transaction(connection, pairing_id)
            return record

    def cancel_authorized_window(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        session_id: str,
        decline: bool = False,
    ) -> CompanionPairingWindow:
        _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = connection.execute(
                "SELECT * FROM companion_pairing_windows WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if (
                row is None
                or expected_version > int(row["version"])
                or CompanionPairingState(str(row["state"])) not in _LIVE_STATES
            ):
                raise CompanionPairingCASMismatch(
                    "Companion pairing window no longer matches expected state"
                )
            self._require_bridge_authority(
                connection,
                session_id,
                str(row["creator_account_id"]),
                now,
                installation_id=str(row["installation_id"]),
            )
            # A request may advance while Bridge is closing its view. Cancel the
            # current immutable window, not only the version Bridge last saw.
            # Confirmation remains a strict compare-and-set on its reviewed view.
            state = (
                CompanionPairingState.DECLINED
                if decline
                else CompanionPairingState.CANCELLED
            )
            if now >= _parse_time(str(row["expires_at"])):
                state = CompanionPairingState.EXPIRED
            self._terminate_row(connection, row, state, now)
            return self._window_in_transaction(connection, pairing_id)

    def revoke_companion_pin(
        self, pairing_id: bytes, *, session_id: str, expected_version: int | None = None
    ) -> bool:
        _require_bytes32(pairing_id, name="pairing_id")
        if expected_version is not None:
            _require_version(expected_version)
        with self.database.transaction() as connection:
            pin = self._optional_pin_in_transaction(connection, pairing_id)
            if pin is None:
                return False
            if expected_version is not None and pin.version != expected_version:
                raise CompanionPairingCASMismatch(
                    "Companion pin no longer matches expected state"
                )
            self._require_bridge_authority(
                connection,
                session_id,
                pin.creator_account_id,
                self.authentication._now(),
                installation_id=pin.installation_id,
            )
            self._revoke_pin(connection, pairing_id)
            return True

    def abort_candidate(self, pairing_id: bytes) -> bool:
        """Fence cancellation by the owning pairing socket, including late admission."""
        _require_bytes32(pairing_id, name="pairing_id")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_pairing_windows WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
            if row is not None and CompanionPairingState(
                str(row["state"])
            ) in _LIVE_STATES | {CompanionPairingState.CONFIRMED}:
                self._terminate_row(
                    connection,
                    row,
                    CompanionPairingState.CANCELLED,
                    self.authentication._now(),
                )
                return True
            if self._optional_pin_in_transaction(connection, pairing_id) is not None:
                self._revoke_pin(connection, pairing_id)
                return True
            return False

    def _revoke_pin(self, connection: sqlite3.Connection, pairing_id: bytes) -> None:
        self.authentication._revoke_in_transaction(
            connection,
            RevocationKey(
                RevocationScopeType.AGENT_PAIRING, _encoded_pairing_id(pairing_id)
            ),
            reason="companion_revoked",
        )
        self.authentication._increment_authorization_epoch(connection)

    def terminate_window(
        self,
        pairing_id: bytes,
        *,
        expected_version: int,
        expected_state: CompanionPairingState,
        terminal_state: CompanionPairingState,
        at: datetime,
        reason: str | None = None,
    ) -> CompanionPairingWindow:
        pairing_id = _require_bytes32(pairing_id, name="pairing_id")
        _require_version(expected_version)
        if expected_state not in _LIVE_STATES | {CompanionPairingState.CONFIRMED}:
            raise ValueError("expected_state must hold a candidate")
        if (
            expected_state is CompanionPairingState.CONFIRMED
            and terminal_state
            not in {
                CompanionPairingState.CANCELLED,
                CompanionPairingState.REVOKED,
            }
        ):
            raise ValueError("confirmed candidates may only be cancelled or revoked")
        if terminal_state not in _FAILURE_TERMINAL_STATES:
            raise ValueError("terminal_state must clear candidate secret material")
        if reason is not None:
            _require_bounded_text(reason, name="reason", maximum=_MAX_REASON_BYTES)
        _time_text(at)

        with self.database.transaction() as connection:
            now = self.authentication._now()
            row = self._require_expected_window(
                connection,
                pairing_id,
                expected_version=expected_version,
                expected_state=expected_state,
            )
            lower_column = {
                CompanionPairingState.OPEN: "opened_at",
                CompanionPairingState.OFFERED: "offered_at",
                CompanionPairingState.AWAITING_CONFIRMATION: "awaiting_confirmation_at",
                CompanionPairingState.CONFIRMED: "confirmed_at",
            }[expected_state]
            self._require_transition_time(row, at, lower_column=lower_column, now=now)
            expires_at = _parse_time(str(row["expires_at"]))
            if terminal_state is CompanionPairingState.EXPIRED and (
                now < expires_at or at < expires_at
            ):
                raise CompanionPairingStateError(
                    "Companion pairing window has not expired"
                )
            cursor = connection.execute(
                """
                UPDATE companion_pairing_windows SET
                    state = ?,
                    version = version + 1,
                    wrapped_brain_noise_private_key = NULL,
                    confirmation_principal_id = NULL,
                    confirmation_session_id = NULL,
                    confirmed_at = NULL,
                    terminal_at = ?,
                    terminal_reason = ?
                WHERE pairing_id = ? AND version = ? AND state = ?
                """,
                (
                    terminal_state.value,
                    _time_text(at),
                    reason,
                    pairing_id,
                    expected_version,
                    expected_state.value,
                ),
            )
            self._require_updated(cursor)
            return self._window_in_transaction(connection, pairing_id)

    def _allocate_generation(
        self, connection: sqlite3.Connection, installation_id: str
    ) -> int:
        row = connection.execute(
            """
            SELECT highest_generation
            FROM companion_pairing_generations
            WHERE installation_id = ?
            """,
            (installation_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO companion_pairing_generations(
                    installation_id, highest_generation
                ) VALUES (?, 1)
                """,
                (installation_id,),
            )
            return 1

        current = int(row["highest_generation"])
        if current >= _MAX_GENERATION:
            raise CompanionPairingGenerationExhausted(
                "Companion pairing generation is exhausted"
            )
        next_generation = current + 1
        cursor = connection.execute(
            """
            UPDATE companion_pairing_generations
            SET highest_generation = ?
            WHERE installation_id = ? AND highest_generation = ?
            """,
            (next_generation, installation_id, current),
        )
        if cursor.rowcount != 1:
            raise CompanionPairingPersistenceError(
                "Companion pairing generation allocation failed"
            )
        return next_generation

    @staticmethod
    def _terminate_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        state: CompanionPairingState,
        now: datetime,
    ) -> None:
        updated = connection.execute(
            "UPDATE companion_pairing_windows SET state = ?, version = version + 1, "
            "wrapped_brain_noise_private_key = NULL, confirmation_principal_id = NULL, "
            "confirmation_session_id = NULL, confirmed_at = NULL, terminal_at = ?, terminal_reason = ? "
            "WHERE pairing_id = ? AND version = ?",
            (
                state.value,
                _time_text(now),
                state.value,
                row["pairing_id"],
                row["version"],
            ),
        )
        CompanionPairingPersistence._require_updated(updated)

    def _require_bridge_authority(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        creator_account_id: str,
        now: datetime,
        *,
        installation_id: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        try:
            session = self.authentication._require_session_current(
                connection, session_id, now
            )
        except AuthenticationStateError:
            raise CompanionPairingStateError(
                "Companion pairing requires a current Bridge session"
            ) from None
        if session["creator_account_id"] != creator_account_id or session[
            "role"
        ] not in {"creator", "operator"}:
            raise CompanionPairingStateError(
                "Companion pairing authority does not match"
            )
        credential = connection.execute(
            "SELECT * FROM webauthn_credentials WHERE credential_id = ? AND revoked_at IS NULL",
            (session["credential_id"],),
        ).fetchone()
        if credential is None or (
            installation_id is not None
            and credential["installation_id"] != installation_id
        ):
            raise CompanionPairingStateError(
                "Companion pairing installation authority does not match"
            )
        return session, credential

    @staticmethod
    def _require_approved_account(
        connection: sqlite3.Connection,
        creator_account_id: str,
        installation_id: str,
        *,
        organization_id: str | None = None,
    ) -> None:
        binding = connection.execute(
            "SELECT 1 FROM authorized_account_bindings AS binding "
            "JOIN provisioning_candidates AS candidate "
            "ON candidate.association_request_id = binding.association_request_id "
            "WHERE binding.creator_account_id = ? AND binding.installation_id = ? "
            "AND binding.revoked_at IS NULL AND candidate.state = 'approved' "
            "AND candidate.creator_account_id = binding.creator_account_id "
            "AND candidate.installation_id = binding.installation_id "
            + (
                "AND candidate.organization_id = ?"
                if organization_id is not None
                else ""
            ),
            (
                (creator_account_id, installation_id, organization_id)
                if organization_id is not None
                else (creator_account_id, installation_id)
            ),
        ).fetchone()
        if binding is None:
            raise CompanionPairingStateError(
                "Companion pairing requires an approved creator account"
            )

    def _optional_pin_in_transaction(
        self,
        connection: sqlite3.Connection,
        pairing_id: bytes,
    ) -> CompanionPin | None:
        row = connection.execute(
            "SELECT * FROM agent_pairings WHERE pairing_id = ? AND pairing_generation IS NOT NULL "
            "AND revoked_at IS NULL AND protected_brain_noise_static_private_key IS NOT NULL",
            (_encoded_pairing_id(pairing_id),),
        ).fetchone()
        if row is None:
            return None
        references = {
            str(grant["grant_type"]): str(grant["reference_id"])
            for grant in connection.execute(
                "SELECT grant_type, reference_id FROM verified_grant_references "
                "JOIN agent_pairing_grants ON grant_reference_id = reference_id WHERE pairing_id = ?",
                (row["pairing_id"],),
            ).fetchall()
        }
        return CompanionPin(
            pairing_id=pairing_id,
            generation=int(row["pairing_generation"]),
            version=int(row["companion_window_version"]),
            installation_id=str(row["installation_id"]),
            organization_id=str(row["companion_organization_id"]),
            installation_key_id=str(row["companion_installation_key_id"]),
            installation_key_jkt=str(row["companion_installation_key_jkt"]),
            creator_account_id=str(row["creator_account_id"]),
            agent_installation_id=str(row["agent_installation_id"]),
            agent_identity_jwk=bytes(row["public_key"]).decode("utf-8"),
            agent_identity_thumbprint=str(row["key_fingerprint"]),
            agent_noise_public_key=bytes(row["agent_noise_static_public_key"]),
            brain_noise_public_key=bytes(row["brain_noise_static_public_key"]),
            wrapped_brain_noise_private_key=bytes(
                row["protected_brain_noise_static_private_key"]
            ),
            pairing_digest=bytes(row["pairing_digest"]),
            grant_digest=bytes(row["companion_grant_digest"]),
            installation_grant_reference_id=references["installation_grant"],
            creator_account_binding_reference_id=references["creator_account_binding"],
            confirmation_principal_id=str(row["confirmation_principal_id"]),
            confirmation_session_id=str(row["confirmation_session_id"]),
            confirmed_at=_parse_time(str(row["confirmed_at"])),
            opened_at=_parse_time(str(row["created_at"])),
            expires_at=_parse_time(str(row["companion_window_expires_at"])),
        )

    def _pin_in_transaction(
        self, connection: sqlite3.Connection, pairing_id: bytes
    ) -> CompanionPin:
        pin = self._optional_pin_in_transaction(connection, pairing_id)
        if pin is None:
            raise CompanionPairingPersistenceError("Companion pairing pin disappeared")
        return pin

    def _expire_live_window_if_due(
        self,
        connection: sqlite3.Connection,
        installation_id: str,
        now: datetime,
    ) -> None:
        row = connection.execute(
            """
            SELECT *
            FROM companion_pairing_windows
            WHERE installation_id = ?
              AND state IN ('open', 'offered', 'awaiting_confirmation')
            """,
            (installation_id,),
        ).fetchone()
        if row is None or _parse_time(str(row["expires_at"])) > now:
            return
        cursor = connection.execute(
            """
            UPDATE companion_pairing_windows SET
                state = 'expired',
                version = version + 1,
                wrapped_brain_noise_private_key = NULL,
                terminal_at = ?,
                terminal_reason = 'expired'
            WHERE pairing_id = ? AND version = ? AND state = ?
            """,
            (
                _time_text(now),
                bytes(row["pairing_id"]),
                int(row["version"]),
                str(row["state"]),
            ),
        )
        self._require_updated(cursor)

    def _require_pairing_grants(
        self,
        connection: sqlite3.Connection,
        *,
        installation_grant_reference_id: str,
        creator_account_binding_reference_id: str,
        installation_id: str,
        creator_account_id: str,
        now: datetime,
    ) -> None:
        references = (
            installation_grant_reference_id,
            creator_account_binding_reference_id,
        )
        try:
            self.authentication._snapshot_revocations(
                connection,
                (
                    RevocationKey(RevocationScopeType.INSTALLATION, installation_id),
                    RevocationKey(
                        RevocationScopeType.CREATOR_ACCOUNT, creator_account_id
                    ),
                    *self.authentication._grant_keys(references),
                ),
            )
            self.authentication._require_retained_pairing_grants(
                connection, references, now
            )
        except AuthenticationStateError as error:
            raise CompanionPairingGrantUnavailable(
                "Companion pairing grants are not current and retained"
            ) from error

        installation_grant = connection.execute(
            """
            SELECT grant_type, installation_id, creator_account_id, compact_jws,
                   issuer, subject, organization_id, installation_key_id, installation_key_jkt
            FROM verified_grant_references
            WHERE reference_id = ?
            """,
            (installation_grant_reference_id,),
        ).fetchone()
        account_binding = connection.execute(
            """
            SELECT grant_type, installation_id, creator_account_id, compact_jws,
                   issuer, subject, organization_id, installation_key_id, installation_key_jkt
            FROM verified_grant_references
            WHERE reference_id = ?
            """,
            (creator_account_binding_reference_id,),
        ).fetchone()
        if (
            installation_grant is None
            or str(installation_grant["grant_type"]) != "installation_grant"
            or str(installation_grant["installation_id"]) != installation_id
            or installation_grant["creator_account_id"] is not None
            or installation_grant["compact_jws"] is None
            or account_binding is None
            or str(account_binding["grant_type"]) != "creator_account_binding"
            or str(account_binding["installation_id"]) != installation_id
            or str(account_binding["creator_account_id"]) != creator_account_id
            or account_binding["compact_jws"] is None
        ):
            raise CompanionPairingGrantUnavailable(
                "Companion pairing grants do not match the window"
            )
        for name in (
            "issuer",
            "subject",
            "organization_id",
            "installation_key_id",
            "installation_key_jkt",
        ):
            if (
                not installation_grant[name]
                or installation_grant[name] != account_binding[name]
            ):
                raise CompanionPairingGrantUnavailable(
                    "Companion pairing grants do not share an installation identity"
                )
        key = connection.execute(
            "SELECT installation_key_id, installation_key_jkt "
            "FROM installation_key_reference "
            "WHERE singleton = 1 AND activated_at IS NOT NULL"
        ).fetchone()
        if key is None or any(
            key[name] != installation_grant[name]
            for name in ("installation_key_id", "installation_key_jkt")
        ):
            raise CompanionPairingGrantUnavailable(
                "Companion pairing grants do not match the active installation key"
            )

    def _require_frozen_pairing_grants(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
    ) -> None:
        installation_reference = row["installation_grant_reference_id"]
        account_reference = row["creator_account_binding_reference_id"]
        if installation_reference is None or account_reference is None:
            raise CompanionPairingStateError(
                "Companion pairing offer has no frozen grant references"
            )
        if row["opening_session_id"] is not None:
            self._require_bridge_authority(
                connection,
                str(row["opening_session_id"]),
                str(row["creator_account_id"]),
                now,
                installation_id=str(row["installation_id"]),
            )
        self._require_pairing_grants(
            connection,
            installation_grant_reference_id=str(installation_reference),
            creator_account_binding_reference_id=str(account_reference),
            installation_id=str(row["installation_id"]),
            creator_account_id=str(row["creator_account_id"]),
            now=now,
        )

    @staticmethod
    def _require_expected_window(
        connection: sqlite3.Connection,
        pairing_id: bytes,
        *,
        expected_version: int,
        expected_state: CompanionPairingState,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM companion_pairing_windows
            WHERE pairing_id = ? AND version = ? AND state = ?
            """,
            (pairing_id, expected_version, expected_state.value),
        ).fetchone()
        if row is None:
            raise CompanionPairingCASMismatch(
                "Companion pairing window no longer matches expected state"
            )
        return row

    @staticmethod
    def _require_transition_time(
        row: sqlite3.Row,
        value: datetime,
        *,
        lower_column: str,
        now: datetime,
    ) -> None:
        lower_value = row[lower_column]
        if lower_value is None:
            raise CompanionPairingStateError(
                "Companion pairing transition timestamp is unavailable"
            )
        if value < _parse_time(str(lower_value)) or value > now:
            raise ValueError("pairing transition timestamp is outside its valid range")

    @staticmethod
    def _require_not_expired(row: sqlite3.Row, at: datetime) -> None:
        if at >= _parse_time(str(row["expires_at"])):
            raise CompanionPairingStateError("Companion pairing window is expired")

    @staticmethod
    def _require_updated(cursor: sqlite3.Cursor) -> None:
        if cursor.rowcount != 1:
            raise CompanionPairingCASMismatch(
                "Companion pairing window update lost its CAS"
            )

    @staticmethod
    def _window_in_transaction(
        connection: sqlite3.Connection, pairing_id: bytes
    ) -> CompanionPairingWindow:
        row = connection.execute(
            "SELECT * FROM companion_pairing_windows WHERE pairing_id = ?",
            (pairing_id,),
        ).fetchone()
        if row is None:
            raise CompanionPairingPersistenceError(
                "Companion pairing window disappeared"
            )
        return _window(row)


def _window(row: sqlite3.Row) -> CompanionPairingWindow:
    return CompanionPairingWindow(
        pairing_id=bytes(row["pairing_id"]),
        installation_id=str(row["installation_id"]),
        creator_account_id=str(row["creator_account_id"]),
        generation=int(row["generation"]),
        state=CompanionPairingState(str(row["state"])),
        version=int(row["version"]),
        opened_at=_parse_time(str(row["opened_at"])),
        expires_at=_parse_time(str(row["expires_at"])),
        brain_nonce=bytes(row["brain_nonce"]),
        brain_noise_public_key=bytes(row["brain_noise_public_key"]),
        wrapped_brain_noise_private_key=_optional_bytes(
            row["wrapped_brain_noise_private_key"]
        ),
        agent_installation_id=_optional_text(row["agent_installation_id"]),
        agent_identity_jwk=_optional_text(row["agent_identity_jwk"]),
        agent_identity_thumbprint=_optional_text(row["agent_identity_thumbprint"]),
        agent_noise_public_key=_optional_bytes(row["agent_noise_public_key"]),
        agent_nonce=_optional_bytes(row["agent_nonce"]),
        pairing_digest=_optional_bytes(row["pairing_digest"]),
        grant_digest=_optional_bytes(row["grant_digest"]),
        installation_grant_reference_id=_optional_text(
            row["installation_grant_reference_id"]
        ),
        creator_account_binding_reference_id=_optional_text(
            row["creator_account_binding_reference_id"]
        ),
        offered_at=_optional_time(row["offered_at"]),
        awaiting_confirmation_at=_optional_time(row["awaiting_confirmation_at"]),
        confirmation_principal_id=_optional_text(row["confirmation_principal_id"]),
        confirmation_session_id=_optional_text(row["confirmation_session_id"]),
        confirmed_at=_optional_time(row["confirmed_at"]),
        terminal_at=_optional_time(row["terminal_at"]),
        terminal_reason=_optional_text(row["terminal_reason"]),
        request_claimed_at=_optional_time(row["request_claimed_at"]),
        opening_principal_id=_optional_text(row["opening_principal_id"]),
        opening_session_id=_optional_text(row["opening_session_id"]),
    )


def _require_bytes32(value: bytes, *, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != _BYTES32:
        raise ValueError(f"{name} must contain exactly 32 bytes")
    return value


def _encoded_pairing_id(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _require_bounded_blob(value: bytes, *, name: str, maximum: int) -> bytes:
    if not isinstance(value, bytes) or not value or len(value) > maximum:
        raise ValueError(f"{name} has an invalid size")
    return value


def _require_text(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")


def _require_bounded_text(value: str, *, name: str, maximum: int) -> None:
    _require_text(value, name=name)
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds its storage bound")


def _require_version(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected_version must be a non-negative integer")


def _require_interval(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ValueError("pairing expiry must be later than opening")
    _time_text(start)
    _time_text(end)


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("pairing timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_bytes(value: object) -> bytes | None:
    return None if value is None else bytes(value)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(str(value))
