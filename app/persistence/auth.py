"""Typed durable storage for local authentication authority."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Protocol
from uuid import uuid4

from app.persistence.database import AuthSQLite
from app.persistence.migrations import MigrationRunner
from app.security.runtime_policy import (
    AuthContext,
    AuthorizationEpoch,
    RevocationObservation,
    RuntimePolicy,
    StaleRuntimePolicyError,
)


class TicketPurpose(str, Enum):
    BRIDGE_WEBSOCKET = "bridge-websocket"
    AGENT_WEBSOCKET = "agent-websocket"
    AGENT_CONFIG = "agent-config"


class RevocationScopeType(str, Enum):
    PRINCIPAL = "principal"
    CREATOR_ACCOUNT = "creator_account"
    WEBAUTHN_CREDENTIAL = "webauthn_credential"
    AGENT_PAIRING = "agent_pairing"
    BRIDGE_SESSION = "bridge_session"
    VERIFIED_GRANT = "verified_grant"
    INSTALLATION = "installation"


@dataclass(frozen=True, slots=True)
class RevocationKey:
    scope_type: RevocationScopeType
    scope_id: str

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("revocation scope_id must not be empty")


@dataclass(frozen=True, slots=True)
class VerifiedGrantReference:
    reference_id: str
    grant_identifier: str
    grant_type: str
    grant_digest: str
    issuer: str
    subject: str
    installation_id: str
    creator_account_id: str | None
    valid_from: datetime
    expires_at: datetime
    verified_at: datetime
    organization_id: str | None = None
    installation_key_id: str | None = None
    installation_key_jkt: str | None = None
    membership_id: str | None = None
    approval_id: str | None = None
    approval_revision: int | None = None
    entitlement_id: str | None = None
    product_id: str | None = None
    allowed_creator_account_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class WebAuthnCredential:
    credential_id: str
    principal_id: str
    external_issuer: str
    external_subject: str
    installation_id: str
    public_key: bytes
    signature_count: int
    enrolled_at: datetime


@dataclass(frozen=True, slots=True)
class AgentPairing:
    pairing_id: str
    key_id: str
    principal_id: str
    creator_account_id: str
    agent_installation_id: str
    external_issuer: str
    external_subject: str
    installation_id: str
    public_key: bytes
    key_fingerprint: str
    created_at: datetime
    grant_reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallationKeyReservation:
    provider_name: str
    provider_key_name: str
    algorithm: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstallationKeyReference:
    provider_name: str
    provider_key_name: str
    algorithm: str
    installation_key_id: str
    installation_key_jkt: str
    public_key_jwk: str
    created_at: datetime
    activated_at: datetime


@dataclass(frozen=True, slots=True)
class WebAuthnChallengeBinding:
    principal_id: str
    credential_id: str | None
    relying_party_id: str
    expected_origin: str


@dataclass(frozen=True, slots=True)
class AgentChallengeBinding:
    principal_id: str
    pairing_id: str
    request_method: str
    request_path: str
    request_body_digest: str
    ticket_purpose: TicketPurpose
    agent_installation_id: str
    creator_account_id: str
    key_id: str
    brain_audience: str


@dataclass(frozen=True, slots=True)
class IssuedChallenge:
    challenge_id: str
    value: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedChallenge:
    challenge_id: str


@dataclass(frozen=True, slots=True)
class BridgeSessionIssue:
    credential_id: str
    principal_id: str
    creator_account_id: str
    role: str
    expires_at: datetime
    grant_reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IssuedBridgeSession:
    session_id: str
    session_value: str
    csrf_value: str
    policy: RuntimePolicy

    @property
    def principal_id(self) -> str:
        return self.policy.identity.principal_id

    @property
    def creator_account_id(self) -> str:
        return self.policy.identity.creator_account_id

    @property
    def role(self) -> str:
        return self.policy.identity.role

    @property
    def expires_at(self) -> datetime:
        if self.policy.expires_at is None:
            raise RuntimeError("issued Bridge session policy requires an expiry")
        return self.policy.expires_at


@dataclass(frozen=True, slots=True)
class ActiveBridgeSession:
    session_id: str
    policy: RuntimePolicy

    @property
    def principal_id(self) -> str:
        return self.policy.identity.principal_id

    @property
    def creator_account_id(self) -> str:
        return self.policy.identity.creator_account_id

    @property
    def role(self) -> str:
        return self.policy.identity.role

    @property
    def expires_at(self) -> datetime:
        if self.policy.expires_at is None:
            raise RuntimeError("active Bridge session policy requires an expiry")
        return self.policy.expires_at


@dataclass(frozen=True, slots=True)
class TicketIssue:
    purpose: TicketPurpose
    principal_id: str
    role: str
    creator_account_id: str
    expires_at: datetime
    parent_session_id: str | None = None
    parent_pairing_id: str | None = None
    expected_bridge_session_id: str | None = None
    expected_agent_installation_id: str | None = None


@dataclass(frozen=True, slots=True)
class TicketBinding:
    purpose: TicketPurpose
    creator_account_id: str
    role: str
    expected_bridge_session_id: str | None = None
    expected_agent_installation_id: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    ticket_id: str
    value: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedTicket:
    ticket_id: str
    purpose: TicketPurpose
    parent_session_id: str | None
    parent_pairing_id: str | None
    policy: RuntimePolicy

    @property
    def principal_id(self) -> str:
        return self.policy.identity.principal_id

    @property
    def role(self) -> str:
        return self.policy.identity.role

    @property
    def creator_account_id(self) -> str:
        return self.policy.identity.creator_account_id


class AuthenticationStore(Protocol):
    def reserve_installation_key(
        self, reservation: InstallationKeyReservation
    ) -> InstallationKeyReservation: ...

    def installation_key_reservation(
        self,
    ) -> InstallationKeyReservation | None: ...

    def activate_installation_key(
        self, reference: InstallationKeyReference
    ) -> None: ...

    def installation_key_reference(self) -> InstallationKeyReference | None: ...

    def register_webauthn_credential(self, credential: WebAuthnCredential) -> None: ...

    def webauthn_credential(
        self, credential_id: str, *, principal_id: str
    ) -> WebAuthnCredential | None: ...

    def advance_webauthn_signature_count(
        self,
        credential_id: str,
        *,
        principal_id: str,
        expected_count: int,
        asserted_count: int,
    ) -> bool: ...

    def record_verified_grant(self, grant: VerifiedGrantReference) -> None: ...

    def record_verified_grants(
        self, grants: tuple[VerifiedGrantReference, ...]
    ) -> None: ...

    def replace_verified_grant(
        self, previous_reference_id: str, grant: VerifiedGrantReference
    ) -> None: ...

    def verified_grant(
        self, reference_id: str
    ) -> VerifiedGrantReference | None: ...

    def verified_grants(self) -> tuple[VerifiedGrantReference, ...]: ...

    def register_agent_pairing(self, pairing: AgentPairing) -> None: ...

    def activate_agent_pairing(
        self, policy: RuntimePolicy, pairing_id: str
    ) -> bool: ...

    def issue_webauthn_challenge(
        self, binding: WebAuthnChallengeBinding, *, expires_at: datetime
    ) -> IssuedChallenge: ...

    def issue_agent_challenge(
        self, binding: AgentChallengeBinding, *, expires_at: datetime
    ) -> IssuedChallenge: ...

    def consume_webauthn_challenge(
        self, value: str, binding: WebAuthnChallengeBinding
    ) -> ConsumedChallenge | None: ...

    def consume_agent_challenge(
        self, value: str, binding: AgentChallengeBinding
    ) -> ConsumedChallenge | None: ...

    def issue_bridge_session(self, issue: BridgeSessionIssue) -> IssuedBridgeSession: ...

    def read_bridge_session(
        self, session_value: str, *, csrf_value: str | None = None
    ) -> ActiveBridgeSession | None: ...

    def issue_ticket(self, issue: TicketIssue) -> IssuedTicket: ...

    def consume_ticket(
        self, value: str, binding: TicketBinding
    ) -> ConsumedTicket | None: ...

    def build_runtime_policy(
        self,
        identity: AuthContext,
        *,
        signed_object_digests: tuple[str, ...] = (),
    ) -> RuntimePolicy: ...

    def build_runtime_policy_from_grants(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
    ) -> RuntimePolicy: ...

    def runtime_policy_is_current(self, policy: RuntimePolicy) -> bool: ...

    def revoke(self, key: RevocationKey, *, reason: str | None = None) -> int: ...

    def revocation_version(self, key: RevocationKey) -> int: ...


class AuthenticationStateError(ValueError):
    """Raised when an authentication object cannot be issued from current state."""


_BRIDGE_REQUIRED_GRANTS = frozenset(
    {"installation_grant", "membership_snapshot", "creator_account_binding"}
)
_AGENT_REQUIRED_GRANTS = frozenset(
    {"installation_grant", "creator_account_binding"}
)


class SQLiteAuthenticationStore:
    """SQLite authentication authority with transactional one-time consumers."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        clock: Callable[[], datetime] | None = None,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.database = AuthSQLite(path, busy_timeout_ms=busy_timeout_ms)
        migration_path = Path(
            migrations_dir or Path(__file__).with_name("auth_sql")
        )
        MigrationRunner(
            self.database,
            migrations_dir=migration_path,
            lock_path=self.database.path.parent / ".auth-migration.lock",
            backups_dir=self.database.path.parent / "backups",
        ).run()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build_runtime_policy(
        self,
        identity: AuthContext,
        *,
        signed_object_digests: tuple[str, ...] = (),
    ) -> RuntimePolicy:
        with self.database.read() as connection:
            return RuntimePolicy(
                identity=identity,
                authorization_epoch=self._authorization_epoch(connection),
                signed_object_digests=_unique(signed_object_digests),
            )

    @staticmethod
    def _authorization_epoch(connection: sqlite3.Connection) -> AuthorizationEpoch:
        row = connection.execute(
            "SELECT value FROM authorization_epoch WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise AuthenticationStateError("Authorization epoch is unavailable")
        return AuthorizationEpoch(int(row["value"]))

    @staticmethod
    def _increment_authorization_epoch(connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            "UPDATE authorization_epoch SET value = value + 1 WHERE singleton = 1"
        )
        if cursor.rowcount != 1:
            raise AuthenticationStateError("Authorization epoch update failed")

    def reserve_installation_key(
        self, reservation: InstallationKeyReservation
    ) -> InstallationKeyReservation:
        _require_installation_key_reservation(reservation)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO installation_key_reference (
                    singleton, provider_name, provider_key_name, algorithm, created_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (
                    reservation.provider_name,
                    reservation.provider_key_name,
                    reservation.algorithm,
                    _time_text(reservation.created_at),
                ),
            )
            if cursor.rowcount == 1:
                self._increment_authorization_epoch(connection)
            row = connection.execute(
                "SELECT * FROM installation_key_reference WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise AuthenticationStateError("Installation key reservation failed")
        return _installation_key_reservation(row)

    def installation_key_reservation(
        self,
    ) -> InstallationKeyReservation | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM installation_key_reference WHERE singleton = 1"
            ).fetchone()
        return None if row is None else _installation_key_reservation(row)

    def activate_installation_key(
        self, reference: InstallationKeyReference
    ) -> None:
        _require_installation_key_reference(reference)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM installation_key_reference WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise AuthenticationStateError("Installation key is not reserved")
            reservation = _installation_key_reservation(row)
            expected = (
                reference.provider_name,
                reference.provider_key_name,
                reference.algorithm,
                reference.created_at,
            )
            actual = (
                reservation.provider_name,
                reservation.provider_key_name,
                reservation.algorithm,
                reservation.created_at,
            )
            if actual != expected:
                raise AuthenticationStateError(
                    "Installation key does not match its durable reservation"
                )
            if row["activated_at"] is not None:
                if _installation_key_reference(row) != reference:
                    raise AuthenticationStateError(
                        "Installation key reference cannot be replaced"
                    )
                return
            cursor = connection.execute(
                """
                UPDATE installation_key_reference SET
                    installation_key_id = ?, installation_key_jkt = ?,
                    public_key_jwk = ?, activated_at = ?
                WHERE singleton = 1 AND activated_at IS NULL
                """,
                (
                    reference.installation_key_id,
                    reference.installation_key_jkt,
                    reference.public_key_jwk,
                    _time_text(reference.activated_at),
                ),
            )
            if cursor.rowcount != 1:
                raise AuthenticationStateError("Installation key activation failed")
            self._increment_authorization_epoch(connection)

    def installation_key_reference(self) -> InstallationKeyReference | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM installation_key_reference
                WHERE singleton = 1 AND activated_at IS NOT NULL
                """
            ).fetchone()
        return None if row is None else _installation_key_reference(row)

    def register_webauthn_credential(self, credential: WebAuthnCredential) -> None:
        if credential.signature_count < 0:
            raise ValueError("signature_count must be non-negative")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO webauthn_credentials (
                    credential_id, principal_id, external_issuer, external_subject,
                    installation_id, public_key, signature_count, enrolled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential.credential_id,
                    credential.principal_id,
                    credential.external_issuer,
                    credential.external_subject,
                    credential.installation_id,
                    credential.public_key,
                    credential.signature_count,
                    _time_text(credential.enrolled_at),
                ),
            )
            self._ensure_scope(
                connection,
                RevocationKey(
                    RevocationScopeType.WEBAUTHN_CREDENTIAL,
                    credential.credential_id,
                ),
            )
            self._increment_authorization_epoch(connection)

    def webauthn_credential(
        self, credential_id: str, *, principal_id: str
    ) -> WebAuthnCredential | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM webauthn_credentials
                WHERE credential_id = ? AND principal_id = ? AND revoked_at IS NULL
                """,
                (credential_id, principal_id),
            ).fetchone()
        if row is None:
            return None
        return WebAuthnCredential(
            credential_id=row["credential_id"],
            principal_id=row["principal_id"],
            external_issuer=row["external_issuer"],
            external_subject=row["external_subject"],
            installation_id=row["installation_id"],
            public_key=bytes(row["public_key"]),
            signature_count=row["signature_count"],
            enrolled_at=_parse_time(row["enrolled_at"]),
        )

    def advance_webauthn_signature_count(
        self,
        credential_id: str,
        *,
        principal_id: str,
        expected_count: int,
        asserted_count: int,
    ) -> bool:
        if expected_count < 0 or asserted_count < 0:
            raise ValueError("signature counts must be non-negative")
        if asserted_count == 0:
            if expected_count != 0:
                return False
            with self.database.read() as connection:
                row = connection.execute(
                    """
                    SELECT 1 FROM webauthn_credentials
                    WHERE credential_id = ? AND principal_id = ?
                      AND signature_count = 0 AND revoked_at IS NULL
                    """,
                    (credential_id, principal_id),
                ).fetchone()
            return row is not None
        if asserted_count <= expected_count:
            return False
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE webauthn_credentials SET signature_count = ?
                WHERE credential_id = ? AND principal_id = ?
                  AND signature_count = ? AND revoked_at IS NULL
                """,
                (asserted_count, credential_id, principal_id, expected_count),
            )
            if cursor.rowcount == 1:
                self._increment_authorization_epoch(connection)
        return cursor.rowcount == 1

    def record_verified_grant(self, grant: VerifiedGrantReference) -> None:
        self.record_verified_grants((grant,))

    def record_verified_grants(
        self, grants: tuple[VerifiedGrantReference, ...]
    ) -> None:
        if not grants:
            raise ValueError("verified grant collection must not be empty")
        for grant in grants:
            self._validate_verified_grant(grant)
        with self.database.transaction() as connection:
            for grant in grants:
                self._insert_verified_grant(connection, grant)
            self._increment_authorization_epoch(connection)

    def replace_verified_grant(
        self, previous_reference_id: str, grant: VerifiedGrantReference
    ) -> None:
        self._validate_verified_grant(grant)
        if previous_reference_id == grant.reference_id:
            raise ValueError("replacement grant must have a new reference")
        with self.database.transaction() as connection:
            previous = connection.execute(
                """
                SELECT grant_type, revoked_at FROM verified_grant_references
                WHERE reference_id = ?
                """,
                (previous_reference_id,),
            ).fetchone()
            if (
                previous is None
                or previous["revoked_at"] is not None
                or previous["grant_type"] != grant.grant_type
            ):
                raise AuthenticationStateError(
                    "Verified grant replacement does not match an active reference"
                )
            self._insert_verified_grant(connection, grant)
            self._revoke_in_transaction(
                connection,
                RevocationKey(
                    RevocationScopeType.VERIFIED_GRANT,
                    previous_reference_id,
                ),
                reason="replaced",
            )
            self._increment_authorization_epoch(connection)

    def verified_grant(
        self, reference_id: str
    ) -> VerifiedGrantReference | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM verified_grant_references WHERE reference_id = ?",
                (reference_id,),
            ).fetchone()
        return None if row is None else _verified_grant_reference(row)

    def verified_grants(self) -> tuple[VerifiedGrantReference, ...]:
        """Return every verified grant reference that is not revoked."""

        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM verified_grant_references
                WHERE revoked_at IS NULL
                ORDER BY verified_at, reference_id
                """
            ).fetchall()
        return tuple(_verified_grant_reference(row) for row in rows)

    @staticmethod
    def _validate_verified_grant(grant: VerifiedGrantReference) -> None:
        _require_interval(grant.valid_from, grant.expires_at)
        if (
            grant.grant_type == "creator_account_binding"
            and grant.creator_account_id is None
        ):
            raise ValueError(
                "creator_account_binding requires an exact creator account"
            )
        if grant.approval_revision is not None and grant.approval_revision <= 0:
            raise ValueError("approval revision must be positive")

    @staticmethod
    def _insert_verified_grant(
        connection: sqlite3.Connection, grant: VerifiedGrantReference
    ) -> None:
        allowed_accounts = (
            None
            if grant.allowed_creator_account_ids is None
            else json.dumps(
                list(grant.allowed_creator_account_ids),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        connection.execute(
            """
            INSERT INTO verified_grant_references (
                reference_id, grant_identifier, grant_type, grant_digest,
                issuer, subject, installation_id, creator_account_id,
                valid_from, expires_at, verified_at, organization_id,
                installation_key_id, installation_key_jkt, membership_id,
                approval_id, approval_revision, entitlement_id, product_id,
                allowed_creator_account_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant.reference_id,
                grant.grant_identifier,
                grant.grant_type,
                grant.grant_digest,
                grant.issuer,
                grant.subject,
                grant.installation_id,
                grant.creator_account_id,
                _time_text(grant.valid_from),
                _time_text(grant.expires_at),
                _time_text(grant.verified_at),
                grant.organization_id,
                grant.installation_key_id,
                grant.installation_key_jkt,
                grant.membership_id,
                grant.approval_id,
                grant.approval_revision,
                grant.entitlement_id,
                grant.product_id,
                allowed_accounts,
            ),
        )
        SQLiteAuthenticationStore._ensure_scope(
            connection,
            RevocationKey(RevocationScopeType.VERIFIED_GRANT, grant.reference_id),
        )

    def register_agent_pairing(self, pairing: AgentPairing) -> None:
        grants = _unique(pairing.grant_reference_ids)
        with self.database.transaction() as connection:
            now = self._now()
            self._require_agent_grants(connection, grants, now, pairing)
            connection.execute(
                """
                INSERT INTO agent_pairings (
                    pairing_id, key_id, principal_id, creator_account_id,
                    agent_installation_id, external_issuer, external_subject,
                    installation_id, public_key, key_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing.pairing_id,
                    pairing.key_id,
                    pairing.principal_id,
                    pairing.creator_account_id,
                    pairing.agent_installation_id,
                    pairing.external_issuer,
                    pairing.external_subject,
                    pairing.installation_id,
                    pairing.public_key,
                    pairing.key_fingerprint,
                    _time_text(pairing.created_at),
                ),
            )
            connection.executemany(
                """
                INSERT INTO agent_pairing_grants(pairing_id, grant_reference_id)
                VALUES (?, ?)
                """,
                ((pairing.pairing_id, reference_id) for reference_id in grants),
            )
            self._ensure_scope(
                connection,
                RevocationKey(RevocationScopeType.AGENT_PAIRING, pairing.pairing_id),
            )
            self._increment_authorization_epoch(connection)

    def activate_agent_pairing(
        self, policy: RuntimePolicy, pairing_id: str
    ) -> bool:
        with self.database.transaction() as connection:
            instant = self._now()
            if not self._policy_is_current(connection, policy, instant):
                raise StaleRuntimePolicyError(
                    "Runtime policy authorization epoch is stale"
                )
            pairing = connection.execute(
                """
                SELECT * FROM agent_pairings
                WHERE pairing_id = ? AND activated_at IS NULL AND revoked_at IS NULL
                """,
                (pairing_id,),
            ).fetchone()
            if pairing is None:
                return False
            grants = self._pairing_grants(connection, pairing_id)
            self._require_agent_grants(connection, grants, instant, pairing)
            self._snapshot_revocations(
                connection, self._agent_keys(pairing, grants)
            )
            cursor = connection.execute(
                """
                UPDATE agent_pairings SET activated_at = ?
                WHERE pairing_id = ? AND activated_at IS NULL AND revoked_at IS NULL
                  AND created_at <= ?
                """,
                (_time_text(instant), pairing_id, _time_text(instant)),
            )
            if cursor.rowcount == 1:
                self._increment_authorization_epoch(connection)
            return cursor.rowcount == 1

    def issue_webauthn_challenge(
        self, binding: WebAuthnChallengeBinding, *, expires_at: datetime
    ) -> IssuedChallenge:
        challenge_id, value, digest = _new_secret()
        keys = [RevocationKey(RevocationScopeType.PRINCIPAL, binding.principal_id)]
        if binding.credential_id is not None:
            keys.append(
                RevocationKey(
                    RevocationScopeType.WEBAUTHN_CREDENTIAL,
                    binding.credential_id,
                )
            )
        with self.database.transaction() as connection:
            now = self._now()
            _require_interval(now, expires_at)
            if binding.credential_id is not None:
                row = connection.execute(
                    """
                    SELECT principal_id, revoked_at FROM webauthn_credentials
                    WHERE credential_id = ?
                    """,
                    (binding.credential_id,),
                ).fetchone()
                if (
                    row is None
                    or row["principal_id"] != binding.principal_id
                    or row["revoked_at"] is not None
                ):
                    raise AuthenticationStateError("WebAuthn credential is not active")
            snapshots = self._snapshot_revocations(connection, keys)
            connection.execute(
                """
                INSERT INTO auth_challenges (
                    challenge_id, challenge_kind, secret_digest, principal_id,
                    credential_id, relying_party_id, expected_origin,
                    issued_at, expires_at
                ) VALUES (?, 'webauthn', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    challenge_id,
                    digest,
                    binding.principal_id,
                    binding.credential_id,
                    binding.relying_party_id,
                    binding.expected_origin,
                    _time_text(now),
                    _time_text(expires_at),
                ),
            )
            self._insert_bindings(connection, "challenge", challenge_id, snapshots)
            self._increment_authorization_epoch(connection)
        return IssuedChallenge(challenge_id, value, expires_at)

    def issue_agent_challenge(
        self, binding: AgentChallengeBinding, *, expires_at: datetime
    ) -> IssuedChallenge:
        if binding.ticket_purpose is TicketPurpose.BRIDGE_WEBSOCKET:
            raise ValueError("Agent challenge requires an Agent ticket purpose")
        challenge_id, value, digest = _new_secret()
        with self.database.transaction() as connection:
            now = self._now()
            _require_interval(now, expires_at)
            pairing, grants = self._require_pairing_current(
                connection, binding.pairing_id, now
            )
            expected = (
                binding.principal_id,
                binding.creator_account_id,
                binding.agent_installation_id,
                binding.key_id,
            )
            actual = (
                pairing["principal_id"],
                pairing["creator_account_id"],
                pairing["agent_installation_id"],
                pairing["key_id"],
            )
            if expected != actual:
                raise AuthenticationStateError("Agent challenge binding does not match")
            keys = self._agent_keys(pairing, grants)
            snapshots = self._snapshot_revocations(connection, keys)
            connection.execute(
                """
                INSERT INTO auth_challenges (
                    challenge_id, challenge_kind, secret_digest, principal_id,
                    pairing_id, request_method, request_path, request_body_digest,
                    ticket_purpose, agent_installation_id, creator_account_id,
                    key_id, brain_audience, issued_at, expires_at
                ) VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    challenge_id,
                    digest,
                    binding.principal_id,
                    binding.pairing_id,
                    binding.request_method,
                    binding.request_path,
                    binding.request_body_digest,
                    binding.ticket_purpose.value,
                    binding.agent_installation_id,
                    binding.creator_account_id,
                    binding.key_id,
                    binding.brain_audience,
                    _time_text(now),
                    _time_text(expires_at),
                ),
            )
            self._insert_bindings(connection, "challenge", challenge_id, snapshots)
            self._increment_authorization_epoch(connection)
        return IssuedChallenge(challenge_id, value, expires_at)

    def consume_webauthn_challenge(
        self, value: str, binding: WebAuthnChallengeBinding
    ) -> ConsumedChallenge | None:
        expected = {
            "challenge_kind": "webauthn",
            "principal_id": binding.principal_id,
            "credential_id": binding.credential_id,
            "relying_party_id": binding.relying_party_id,
            "expected_origin": binding.expected_origin,
        }
        return self._consume_challenge(value, expected, ())

    def consume_agent_challenge(
        self, value: str, binding: AgentChallengeBinding
    ) -> ConsumedChallenge | None:
        expected = {
            "challenge_kind": "agent",
            "principal_id": binding.principal_id,
            "pairing_id": binding.pairing_id,
            "request_method": binding.request_method,
            "request_path": binding.request_path,
            "request_body_digest": binding.request_body_digest,
            "ticket_purpose": binding.ticket_purpose.value,
            "agent_installation_id": binding.agent_installation_id,
            "creator_account_id": binding.creator_account_id,
            "key_id": binding.key_id,
            "brain_audience": binding.brain_audience,
        }
        with self.database.read() as connection:
            grants = self._pairing_grants(connection, binding.pairing_id)
        return self._consume_challenge(value, expected, grants)

    def issue_bridge_session(self, issue: BridgeSessionIssue) -> IssuedBridgeSession:
        if issue.role not in {"creator", "operator"}:
            raise ValueError("Bridge session role must be creator or operator")
        grants = _unique(issue.grant_reference_ids)
        session_id, session_value, session_digest = _new_secret()
        _, csrf_value, csrf_digest = _new_secret()
        with self.database.transaction() as connection:
            now = self._now()
            _require_interval(now, issue.expires_at)
            credential = connection.execute(
                """
                SELECT principal_id, external_issuer, external_subject,
                       installation_id, revoked_at
                FROM webauthn_credentials WHERE credential_id = ?
                """,
                (issue.credential_id,),
            ).fetchone()
            if (
                credential is None
                or credential["principal_id"] != issue.principal_id
                or credential["revoked_at"] is not None
            ):
                raise AuthenticationStateError("WebAuthn credential is not active")
            self._require_bridge_grants(
                connection,
                grants,
                now,
                external_issuer=credential["external_issuer"],
                external_subject=credential["external_subject"],
                installation_id=credential["installation_id"],
                creator_account_id=issue.creator_account_id,
            )
            keys = [
                RevocationKey(RevocationScopeType.PRINCIPAL, issue.principal_id),
                RevocationKey(
                    RevocationScopeType.CREATOR_ACCOUNT, issue.creator_account_id
                ),
                RevocationKey(
                    RevocationScopeType.WEBAUTHN_CREDENTIAL, issue.credential_id
                ),
                RevocationKey(
                    RevocationScopeType.INSTALLATION, credential["installation_id"]
                ),
                RevocationKey(RevocationScopeType.BRIDGE_SESSION, session_id),
                *self._grant_keys(grants),
            ]
            snapshots = self._snapshot_revocations(connection, keys)
            connection.execute(
                """
                INSERT INTO bridge_sessions (
                    session_id, secret_digest, csrf_digest, credential_id,
                    principal_id, creator_account_id, role, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session_digest,
                    csrf_digest,
                    issue.credential_id,
                    issue.principal_id,
                    issue.creator_account_id,
                    issue.role,
                    _time_text(now),
                    _time_text(issue.expires_at),
                ),
            )
            self._insert_grants(
                connection, "bridge_session_grants", "session_id", session_id, grants
            )
            self._insert_bindings(connection, "bridge_session", session_id, snapshots)
            self._increment_authorization_epoch(connection)
            policy = self._runtime_policy(
                connection,
                identity=AuthContext(
                    principal_id=issue.principal_id,
                    creator_account_id=issue.creator_account_id,
                    role=issue.role,  # type: ignore[arg-type]
                    session_id=session_id,
                    session_expires_at=int(issue.expires_at.timestamp()),
                ),
                expires_at=issue.expires_at,
                revocations=snapshots,
                grant_reference_ids=grants,
            )
        return IssuedBridgeSession(
            session_id,
            session_value,
            csrf_value,
            policy,
        )

    def read_bridge_session(
        self, session_value: str, *, csrf_value: str | None = None
    ) -> ActiveBridgeSession | None:
        with self.database.read() as connection:
            now = self._now()
            row = connection.execute(
                """
                SELECT * FROM bridge_sessions WHERE secret_digest = ?
                """,
                (_secret_digest(session_value),),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if csrf_value is not None and row["csrf_digest"] != _secret_digest(csrf_value):
                return None
            policy = self._runtime_policy(
                connection,
                identity=AuthContext(
                    principal_id=row["principal_id"],
                    creator_account_id=row["creator_account_id"],
                    role=row["role"],
                    session_id=row["session_id"],
                    session_expires_at=int(_parse_time(row["expires_at"]).timestamp()),
                ),
                expires_at=_parse_time(row["expires_at"]),
                revocations=self._object_revocations(
                    connection, "bridge_session", row["session_id"]
                ),
                grant_reference_ids=self._session_grants(
                    connection, row["session_id"]
                ),
            )
            if not self._policy_is_current(connection, policy, now):
                return None
            return ActiveBridgeSession(row["session_id"], policy)

    def issue_ticket(self, issue: TicketIssue) -> IssuedTicket:
        self._validate_ticket_issue(issue)
        ticket_id, value, digest = _new_secret()
        with self.database.transaction() as connection:
            now = self._now()
            _require_interval(now, issue.expires_at)
            if issue.purpose is TicketPurpose.BRIDGE_WEBSOCKET:
                parent = self._require_session_current(
                    connection, issue.parent_session_id or "", now
                )
                actual = (
                    parent["principal_id"],
                    parent["creator_account_id"],
                    parent["role"],
                )
                grants = self._session_grants(connection, parent["session_id"])
                if issue.expires_at > _parse_time(parent["expires_at"]):
                    raise AuthenticationStateError(
                        "Ticket cannot outlive its Bridge session"
                    )
                keys = self._object_keys(
                    connection, "bridge_session", parent["session_id"]
                )
            else:
                parent, grants = self._require_pairing_current(
                    connection, issue.parent_pairing_id or "", now
                )
                actual = (
                    parent["principal_id"],
                    parent["creator_account_id"],
                    issue.role,
                )
                keys = self._agent_keys(parent, grants)
                if (
                    issue.expected_agent_installation_id
                    != parent["agent_installation_id"]
                ):
                    raise AuthenticationStateError(
                        "Ticket Agent installation does not match pairing"
                    )
            expected = (issue.principal_id, issue.creator_account_id, issue.role)
            if actual != expected:
                raise AuthenticationStateError("Ticket parent binding does not match")
            keys = [
                *keys,
                *self._grant_keys(grants),
            ]
            snapshots = self._snapshot_revocations(connection, keys)
            connection.execute(
                """
                INSERT INTO runtime_tickets (
                    ticket_id, secret_digest, purpose, principal_id, role,
                    creator_account_id, parent_session_id, parent_pairing_id,
                    expected_bridge_session_id, expected_agent_installation_id,
                    issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    digest,
                    issue.purpose.value,
                    issue.principal_id,
                    issue.role,
                    issue.creator_account_id,
                    issue.parent_session_id,
                    issue.parent_pairing_id,
                    issue.expected_bridge_session_id,
                    issue.expected_agent_installation_id,
                    _time_text(now),
                    _time_text(issue.expires_at),
                ),
            )
            self._insert_grants(
                connection, "runtime_ticket_grants", "ticket_id", ticket_id, grants
            )
            self._insert_bindings(connection, "ticket", ticket_id, snapshots)
            self._increment_authorization_epoch(connection)
        return IssuedTicket(ticket_id, value, issue.expires_at)

    def consume_ticket(
        self, value: str, binding: TicketBinding
    ) -> ConsumedTicket | None:
        with self.database.transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM runtime_tickets WHERE secret_digest = ?",
                (_secret_digest(value),),
            ).fetchone()
            if row is None:
                return None
            expected = (
                binding.purpose.value,
                binding.creator_account_id,
                binding.role,
                binding.expected_bridge_session_id,
                binding.expected_agent_installation_id,
            )
            actual = (
                row["purpose"],
                row["creator_account_id"],
                row["role"],
                row["expected_bridge_session_id"],
                row["expected_agent_installation_id"],
            )
            grants = self._ticket_grants(connection, row["ticket_id"])
            policy = self._runtime_policy(
                connection,
                identity=AuthContext(
                    principal_id=row["principal_id"],
                    creator_account_id=row["creator_account_id"],
                    role=row["role"],
                ),
                expires_at=_parse_time(row["expires_at"]),
                revocations=self._object_revocations(
                    connection, "ticket", row["ticket_id"]
                ),
                grant_reference_ids=grants,
            )
            if (
                expected != actual
                or row["consumed_at"] is not None
                or row["invalidated_at"] is not None
                or not self._policy_is_current(connection, policy, now)
            ):
                return None
            cursor = connection.execute(
                """
                UPDATE runtime_tickets SET consumed_at = ?
                WHERE ticket_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (_time_text(now), row["ticket_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._increment_authorization_epoch(connection)
            policy = self._runtime_policy(
                connection,
                identity=policy.identity,
                expires_at=policy.expires_at,
                revocations=policy.revocations,
                grant_reference_ids=policy.signed_object_reference_ids,
            )
            return ConsumedTicket(
                row["ticket_id"],
                TicketPurpose(row["purpose"]),
                row["parent_session_id"],
                row["parent_pairing_id"],
                policy,
            )

    def runtime_policy_is_current(self, policy: RuntimePolicy) -> bool:
        with self.database.read() as connection:
            return self._policy_is_current(connection, policy, self._now())

    def build_runtime_policy_from_grants(
        self,
        identity: AuthContext,
        grant_reference_ids: tuple[str, ...],
    ) -> RuntimePolicy:
        references = _unique(grant_reference_ids)
        if not references:
            raise ValueError("runtime policy requires verified grant references")
        with self.database.read() as connection:
            now = self._now()
            rows = [
                connection.execute(
                    """
                    SELECT reference_id, grant_type, creator_account_id,
                           allowed_creator_account_ids, expires_at
                    FROM verified_grant_references
                    WHERE reference_id = ?
                    """,
                    (reference_id,),
                ).fetchone()
                for reference_id in references
            ]
            if any(row is None for row in rows) or not self._grants_are_current(
                connection, references, now
            ):
                raise AuthenticationStateError(
                    "Runtime policy verified grants are not current"
                )
            for row in rows:
                if row is None:
                    raise AuthenticationStateError(
                        "Runtime policy verified grant is unavailable"
                    )
                scoped_account = row["creator_account_id"]
                if (
                    scoped_account is not None
                    and scoped_account != identity.creator_account_id
                ):
                    raise AuthenticationStateError(
                        "Runtime policy verified grant has a different account scope"
                    )
                allowed = row["allowed_creator_account_ids"]
                if allowed is not None:
                    values = json.loads(allowed)
                    if identity.creator_account_id not in values:
                        raise AuthenticationStateError(
                            "Runtime policy membership does not allow the account"
                        )
            expiry = min(_parse_time(row["expires_at"]) for row in rows if row)
            revocations = tuple(
                RevocationObservation(
                    scope_type=RevocationScopeType.VERIFIED_GRANT.value,
                    scope_id=reference_id,
                    version=self._revocation_version(connection, key),
                )
                for reference_id in references
                for key in (
                    RevocationKey(
                        RevocationScopeType.VERIFIED_GRANT,
                        reference_id,
                    ),
                )
            )
            return self._runtime_policy(
                connection,
                identity=identity,
                expires_at=expiry,
                revocations=revocations,
                grant_reference_ids=references,
            )

    def revoke(self, key: RevocationKey, *, reason: str | None = None) -> int:
        with self.database.transaction() as connection:
            version = self._revoke_in_transaction(connection, key, reason=reason)
            self._increment_authorization_epoch(connection)
            return version

    def _revoke_in_transaction(
        self,
        connection: sqlite3.Connection,
        key: RevocationKey,
        *,
        reason: str | None,
    ) -> int:
        timestamp = _time_text(self._now())
        connection.execute(
            """
            INSERT INTO auth_revocation_state (
                scope_type, scope_id, version, revoked_at, reason
            ) VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                version = auth_revocation_state.version + 1,
                revoked_at = excluded.revoked_at,
                reason = excluded.reason
            """,
            (key.scope_type.value, key.scope_id, timestamp, reason),
        )
        for table, object_type, id_column, invalidated_column in (
            ("bridge_sessions", "bridge_session", "session_id", "revoked_at"),
            ("auth_challenges", "challenge", "challenge_id", "invalidated_at"),
            ("runtime_tickets", "ticket", "ticket_id", "invalidated_at"),
        ):
            connection.execute(
                f"""
                UPDATE {table} SET {invalidated_column} = COALESCE({invalidated_column}, ?)
                WHERE {id_column} IN (
                    SELECT object_id FROM auth_revocation_bindings
                    WHERE object_type = ? AND scope_type = ? AND scope_id = ?
                )
                """,
                (timestamp, object_type, key.scope_type.value, key.scope_id),
            )
        direct_table = {
            RevocationScopeType.WEBAUTHN_CREDENTIAL: (
                "webauthn_credentials",
                "credential_id",
            ),
            RevocationScopeType.AGENT_PAIRING: ("agent_pairings", "pairing_id"),
            RevocationScopeType.BRIDGE_SESSION: ("bridge_sessions", "session_id"),
            RevocationScopeType.VERIFIED_GRANT: (
                "verified_grant_references",
                "reference_id",
            ),
        }.get(key.scope_type)
        if direct_table is not None:
            table, id_column = direct_table
            connection.execute(
                f"UPDATE {table} SET revoked_at = COALESCE(revoked_at, ?) "
                f"WHERE {id_column} = ?",
                (timestamp, key.scope_id),
            )
        return self._revocation_version(connection, key)

    def revocation_version(self, key: RevocationKey) -> int:
        with self.database.read() as connection:
            return self._revocation_version(connection, key)

    @staticmethod
    def _revocation_version(
        connection: sqlite3.Connection, key: RevocationKey
    ) -> int:
        row = connection.execute(
            """
            SELECT version FROM auth_revocation_state
            WHERE scope_type = ? AND scope_id = ?
            """,
            (key.scope_type.value, key.scope_id),
        ).fetchone()
        return 0 if row is None else int(row["version"])

    def _consume_challenge(
        self,
        value: str,
        expected: dict[str, object],
        grants: tuple[str, ...],
    ) -> ConsumedChallenge | None:
        with self.database.transaction() as connection:
            now = self._now()
            row = connection.execute(
                "SELECT * FROM auth_challenges WHERE secret_digest = ?",
                (_secret_digest(value),),
            ).fetchone()
            if row is None or any(row[key] != value for key, value in expected.items()):
                return None
            revocations = self._object_revocations(
                connection, "challenge", row["challenge_id"]
            )
            if (
                row["consumed_at"] is not None
                or row["invalidated_at"] is not None
                or not self._authorization_inputs_are_current(
                    connection,
                    expires_at=_parse_time(row["expires_at"]),
                    grant_reference_ids=grants,
                    revocations=revocations,
                    now=now,
                )
            ):
                return None
            cursor = connection.execute(
                """
                UPDATE auth_challenges SET consumed_at = ?
                WHERE challenge_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (_time_text(now), row["challenge_id"]),
            )
            if cursor.rowcount != 1:
                return None
            self._increment_authorization_epoch(connection)
            return ConsumedChallenge(row["challenge_id"])

    def _require_session_current(
        self, connection: sqlite3.Connection, session_id: str, now: datetime
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM bridge_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise AuthenticationStateError("Bridge session is not active")
        policy = self._runtime_policy(
            connection,
            identity=AuthContext(
                principal_id=row["principal_id"],
                creator_account_id=row["creator_account_id"],
                role=row["role"],
                session_id=session_id,
                session_expires_at=int(_parse_time(row["expires_at"]).timestamp()),
            ),
            expires_at=_parse_time(row["expires_at"]),
            revocations=self._object_revocations(
                connection, "bridge_session", session_id
            ),
            grant_reference_ids=self._session_grants(connection, session_id),
        )
        if not self._policy_is_current(connection, policy, now):
            raise AuthenticationStateError("Bridge session is not active")
        return row

    def _require_pairing_current(
        self, connection: sqlite3.Connection, pairing_id: str, now: datetime
    ) -> tuple[sqlite3.Row, tuple[str, ...]]:
        row = connection.execute(
            "SELECT * FROM agent_pairings WHERE pairing_id = ?", (pairing_id,)
        ).fetchone()
        grants = self._pairing_grants(connection, pairing_id)
        if (
            row is None
            or row["activated_at"] is None
            or _parse_time(row["activated_at"]) > now
            or row["revoked_at"] is not None
        ):
            raise AuthenticationStateError("Agent pairing is not active")
        self._require_agent_grants(connection, grants, now, row)
        keys = self._agent_keys(row, grants)
        snapshots = self._snapshot_revocations(connection, keys)
        if any(snapshot.version != 0 for snapshot in snapshots):
            raise AuthenticationStateError("Agent pairing is revoked")
        return row, grants

    def _snapshot_revocations(
        self, connection: sqlite3.Connection, keys: Iterable[RevocationKey]
    ) -> tuple[RevocationObservation, ...]:
        snapshots: list[RevocationObservation] = []
        for key in _unique_keys(keys):
            self._ensure_scope(connection, key)
            row = connection.execute(
                """
                SELECT version, revoked_at FROM auth_revocation_state
                WHERE scope_type = ? AND scope_id = ?
                """,
                (key.scope_type.value, key.scope_id),
            ).fetchone()
            if row["revoked_at"] is not None:
                raise AuthenticationStateError("Authentication scope is revoked")
            snapshots.append(
                RevocationObservation(
                    key.scope_type.value,
                    key.scope_id,
                    int(row["version"]),
                )
            )
        return tuple(snapshots)

    @staticmethod
    def _ensure_scope(connection: sqlite3.Connection, key: RevocationKey) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO auth_revocation_state(scope_type, scope_id, version)
            VALUES (?, ?, 0)
            """,
            (key.scope_type.value, key.scope_id),
        )

    @staticmethod
    def _insert_bindings(
        connection: sqlite3.Connection,
        object_type: str,
        object_id: str,
        snapshots: tuple[RevocationObservation, ...],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO auth_revocation_bindings (
                object_type, object_id, scope_type, scope_id, observed_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (
                    object_type,
                    object_id,
                    snapshot.scope_type,
                    snapshot.scope_id,
                    snapshot.version,
                )
                for snapshot in snapshots
            ),
        )

    @staticmethod
    def _insert_grants(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        object_id: str,
        grants: tuple[str, ...],
    ) -> None:
        connection.executemany(
            f"INSERT INTO {table}({id_column}, grant_reference_id) VALUES (?, ?)",
            ((object_id, reference_id) for reference_id in grants),
        )

    def _require_grants_current(
        self,
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        now: datetime,
    ) -> None:
        if not self._grants_are_current(connection, grants, now):
            raise AuthenticationStateError("Verified grant reference is not current")

    def _require_grants_for_account(
        self,
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        now: datetime,
        creator_account_id: str,
    ) -> None:
        self._require_grants_current(connection, grants, now)
        for reference_id in grants:
            row = connection.execute(
                """
                SELECT grant_type, creator_account_id
                FROM verified_grant_references
                WHERE reference_id = ?
                """,
                (reference_id,),
            ).fetchone()
            stored_account = row["creator_account_id"]
            exact_binding_mismatch = (
                row["grant_type"] == "creator_account_binding"
                and stored_account != creator_account_id
            )
            scoped_reference_mismatch = (
                row["grant_type"] != "creator_account_binding"
                and stored_account not in {None, creator_account_id}
            )
            if exact_binding_mismatch or scoped_reference_mismatch:
                raise AuthenticationStateError(
                    "Verified grant reference has a different creator account"
                )

    def _require_bridge_grants(
        self,
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        now: datetime,
        *,
        external_issuer: str,
        external_subject: str,
        installation_id: str,
        creator_account_id: str,
    ) -> None:
        self._require_grants_for_account(
            connection, grants, now, creator_account_id
        )
        self._require_grant_types(connection, grants, _BRIDGE_REQUIRED_GRANTS)
        for reference_id in grants:
            row = connection.execute(
                """
                SELECT issuer, subject, installation_id
                FROM verified_grant_references WHERE reference_id = ?
                """,
                (reference_id,),
            ).fetchone()
            if (
                row["issuer"] != external_issuer
                or row["subject"] != external_subject
                or row["installation_id"] != installation_id
            ):
                raise AuthenticationStateError(
                    "Verified grant reference does not match the local credential"
                )

    def _require_agent_grants(
        self,
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        now: datetime,
        pairing: AgentPairing | sqlite3.Row,
    ) -> None:
        creator_account_id = _record_value(pairing, "creator_account_id")
        external_issuer = _record_value(pairing, "external_issuer")
        external_subject = _record_value(pairing, "external_subject")
        installation_id = _record_value(pairing, "installation_id")
        self._require_grants_for_account(
            connection, grants, now, creator_account_id
        )
        self._require_grant_types(connection, grants, _AGENT_REQUIRED_GRANTS)
        for reference_id in grants:
            row = connection.execute(
                """
                SELECT issuer, subject, installation_id
                FROM verified_grant_references WHERE reference_id = ?
                """,
                (reference_id,),
            ).fetchone()
            if (
                row["issuer"] != external_issuer
                or row["subject"] != external_subject
                or row["installation_id"] != installation_id
            ):
                raise AuthenticationStateError(
                    "Verified grant reference does not match the pairing authority"
                )

    @staticmethod
    def _require_grant_types(
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        required: frozenset[str],
    ) -> None:
        found = {
            row["grant_type"]
            for reference_id in grants
            for row in connection.execute(
                """
                SELECT grant_type FROM verified_grant_references
                WHERE reference_id = ?
                """,
                (reference_id,),
            ).fetchall()
        }
        if not required <= found:
            raise AuthenticationStateError(
                "Required verified grant reference types are missing"
            )

    @staticmethod
    def _grants_are_current(
        connection: sqlite3.Connection,
        grants: tuple[str, ...],
        now: datetime,
    ) -> bool:
        timestamp = _time_text(now)
        for reference_id in grants:
            row = connection.execute(
                """
                SELECT 1 FROM verified_grant_references
                WHERE reference_id = ? AND revoked_at IS NULL
                  AND valid_from <= ? AND expires_at > ?
                """,
                (reference_id, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return False
        return True

    @staticmethod
    def _object_revocations(
        connection: sqlite3.Connection,
        object_type: str,
        object_id: str,
    ) -> tuple[RevocationObservation, ...]:
        rows = connection.execute(
            """
            SELECT scope_type, scope_id, observed_version
            FROM auth_revocation_bindings
            WHERE object_type = ? AND object_id = ?
            ORDER BY scope_type, scope_id
            """,
            (object_type, object_id),
        ).fetchall()
        return tuple(
            RevocationObservation(
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                version=int(row["observed_version"]),
            )
            for row in rows
        )

    def _runtime_policy(
        self,
        connection: sqlite3.Connection,
        *,
        identity: AuthContext,
        expires_at: datetime | None = None,
        revocations: tuple[RevocationObservation, ...] = (),
        grant_reference_ids: tuple[str, ...] = (),
        signed_object_digests: tuple[str, ...] = (),
    ) -> RuntimePolicy:
        references = _unique(grant_reference_ids)
        grant_digests: list[str] = []
        for reference_id in references:
            row = connection.execute(
                """
                SELECT grant_digest FROM verified_grant_references
                WHERE reference_id = ?
                """,
                (reference_id,),
            ).fetchone()
            if row is None:
                raise AuthenticationStateError(
                    "Runtime policy signed-object reference is unavailable"
                )
            grant_digests.append(row["grant_digest"])
        return RuntimePolicy(
            identity=identity,
            authorization_epoch=self._authorization_epoch(connection),
            signed_object_digests=_unique((*signed_object_digests, *grant_digests)),
            signed_object_reference_ids=references,
            expires_at=expires_at,
            revocations=revocations,
        )

    def _policy_is_current(
        self,
        connection: sqlite3.Connection,
        policy: RuntimePolicy,
        now: datetime,
    ) -> bool:
        if policy.authorization_epoch != self._authorization_epoch(connection):
            return False
        return self._authorization_inputs_are_current(
            connection,
            expires_at=policy.expires_at,
            grant_reference_ids=policy.signed_object_reference_ids,
            revocations=policy.revocations,
            now=now,
        )

    def _authorization_inputs_are_current(
        self,
        connection: sqlite3.Connection,
        *,
        expires_at: datetime | None,
        grant_reference_ids: tuple[str, ...],
        revocations: tuple[RevocationObservation, ...],
        now: datetime,
    ) -> bool:
        if expires_at is not None and now >= expires_at:
            return False
        if not self._grants_are_current(connection, grant_reference_ids, now):
            return False
        for observed in revocations:
            row = connection.execute(
                """
                SELECT version, revoked_at FROM auth_revocation_state
                WHERE scope_type = ? AND scope_id = ?
                """,
                (observed.scope_type, observed.scope_id),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or int(row["version"]) != observed.version
            ):
                return False
        return True

    @staticmethod
    def _object_keys(
        connection: sqlite3.Connection, object_type: str, object_id: str
    ) -> list[RevocationKey]:
        rows = connection.execute(
            """
            SELECT scope_type, scope_id FROM auth_revocation_bindings
            WHERE object_type = ? AND object_id = ?
            """,
            (object_type, object_id),
        ).fetchall()
        return [
            RevocationKey(RevocationScopeType(row["scope_type"]), row["scope_id"])
            for row in rows
        ]

    @staticmethod
    def _grant_keys(grants: tuple[str, ...]) -> list[RevocationKey]:
        return [
            RevocationKey(RevocationScopeType.VERIFIED_GRANT, reference_id)
            for reference_id in grants
        ]

    def _agent_keys(
        self, pairing: sqlite3.Row, grants: tuple[str, ...]
    ) -> list[RevocationKey]:
        return [
            RevocationKey(RevocationScopeType.PRINCIPAL, pairing["principal_id"]),
            RevocationKey(
                RevocationScopeType.CREATOR_ACCOUNT,
                pairing["creator_account_id"],
            ),
            RevocationKey(
                RevocationScopeType.INSTALLATION,
                pairing["agent_installation_id"],
            ),
            RevocationKey(
                RevocationScopeType.INSTALLATION,
                pairing["installation_id"],
            ),
            RevocationKey(RevocationScopeType.AGENT_PAIRING, pairing["pairing_id"]),
            *self._grant_keys(grants),
        ]

    @staticmethod
    def _pairing_grants(
        connection: sqlite3.Connection, pairing_id: str
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT grant_reference_id FROM agent_pairing_grants
            WHERE pairing_id = ? ORDER BY grant_reference_id
            """,
            (pairing_id,),
        ).fetchall()
        return tuple(row["grant_reference_id"] for row in rows)

    @staticmethod
    def _session_grants(
        connection: sqlite3.Connection, session_id: str
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT grant_reference_id FROM bridge_session_grants
            WHERE session_id = ? ORDER BY grant_reference_id
            """,
            (session_id,),
        ).fetchall()
        return tuple(row["grant_reference_id"] for row in rows)

    @staticmethod
    def _ticket_grants(
        connection: sqlite3.Connection, ticket_id: str
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT grant_reference_id FROM runtime_ticket_grants
            WHERE ticket_id = ? ORDER BY grant_reference_id
            """,
            (ticket_id,),
        ).fetchall()
        return tuple(row["grant_reference_id"] for row in rows)

    @staticmethod
    def _validate_ticket_issue(issue: TicketIssue) -> None:
        if issue.purpose is TicketPurpose.BRIDGE_WEBSOCKET:
            valid = (
                issue.parent_session_id is not None
                and issue.parent_pairing_id is None
                and issue.expected_bridge_session_id == issue.parent_session_id
                and issue.expected_agent_installation_id is None
            )
        else:
            valid = (
                issue.parent_session_id is None
                and issue.parent_pairing_id is not None
                and issue.expected_bridge_session_id is None
                and issue.expected_agent_installation_id is not None
                and issue.role == "agent"
            )
        if not valid:
            raise ValueError("Ticket parent and expected binding do not match purpose")

    def _now(self) -> datetime:
        value = self._clock()
        _time_text(value)
        return value.astimezone(timezone.utc)


def _new_secret() -> tuple[str, str, str]:
    object_id = str(uuid4())
    value = secrets.token_urlsafe(32)
    return object_id, value, _secret_digest(value)


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authentication timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _require_installation_key_reservation(
    reservation: InstallationKeyReservation,
) -> None:
    if not all(
        (
            reservation.provider_name,
            reservation.provider_key_name,
            reservation.algorithm,
        )
    ):
        raise ValueError("Installation key reservation fields must not be empty")
    _time_text(reservation.created_at)


def _require_installation_key_reference(
    reference: InstallationKeyReference,
) -> None:
    _require_installation_key_reservation(
        InstallationKeyReservation(
            reference.provider_name,
            reference.provider_key_name,
            reference.algorithm,
            reference.created_at,
        )
    )
    if not all(
        (
            reference.installation_key_id,
            reference.installation_key_jkt,
            reference.public_key_jwk,
        )
    ):
        raise ValueError("Installation key reference fields must not be empty")
    _time_text(reference.activated_at)
    if reference.activated_at < reference.created_at:
        raise ValueError("Installation key activation predates its reservation")


def _installation_key_reservation(row: sqlite3.Row) -> InstallationKeyReservation:
    return InstallationKeyReservation(
        provider_name=str(row["provider_name"]),
        provider_key_name=str(row["provider_key_name"]),
        algorithm=str(row["algorithm"]),
        created_at=_parse_time(str(row["created_at"])),
    )


def _installation_key_reference(row: sqlite3.Row) -> InstallationKeyReference:
    if row["activated_at"] is None:
        raise AuthenticationStateError("Installation key is not active")
    return InstallationKeyReference(
        provider_name=str(row["provider_name"]),
        provider_key_name=str(row["provider_key_name"]),
        algorithm=str(row["algorithm"]),
        installation_key_id=str(row["installation_key_id"]),
        installation_key_jkt=str(row["installation_key_jkt"]),
        public_key_jwk=str(row["public_key_jwk"]),
        created_at=_parse_time(str(row["created_at"])),
        activated_at=_parse_time(str(row["activated_at"])),
    )


def _verified_grant_reference(row: sqlite3.Row) -> VerifiedGrantReference:
    allowed_value = row["allowed_creator_account_ids"]
    allowed_accounts: tuple[str, ...] | None = None
    if allowed_value is not None:
        parsed = json.loads(str(allowed_value))
        if not isinstance(parsed, list) or not all(
            isinstance(value, str) for value in parsed
        ):
            raise AuthenticationStateError(
                "Verified grant account scope is invalid"
            )
        allowed_accounts = tuple(parsed)
    return VerifiedGrantReference(
        reference_id=str(row["reference_id"]),
        grant_identifier=str(row["grant_identifier"]),
        grant_type=str(row["grant_type"]),
        grant_digest=str(row["grant_digest"]),
        issuer=str(row["issuer"]),
        subject=str(row["subject"]),
        installation_id=str(row["installation_id"]),
        creator_account_id=(
            None
            if row["creator_account_id"] is None
            else str(row["creator_account_id"])
        ),
        valid_from=_parse_time(str(row["valid_from"])),
        expires_at=_parse_time(str(row["expires_at"])),
        verified_at=_parse_time(str(row["verified_at"])),
        organization_id=(
            None if row["organization_id"] is None else str(row["organization_id"])
        ),
        installation_key_id=(
            None
            if row["installation_key_id"] is None
            else str(row["installation_key_id"])
        ),
        installation_key_jkt=(
            None
            if row["installation_key_jkt"] is None
            else str(row["installation_key_jkt"])
        ),
        membership_id=(
            None if row["membership_id"] is None else str(row["membership_id"])
        ),
        approval_id=(
            None if row["approval_id"] is None else str(row["approval_id"])
        ),
        approval_revision=(
            None
            if row["approval_revision"] is None
            else int(row["approval_revision"])
        ),
        entitlement_id=(
            None if row["entitlement_id"] is None else str(row["entitlement_id"])
        ),
        product_id=(
            None if row["product_id"] is None else str(row["product_id"])
        ),
        allowed_creator_account_ids=allowed_accounts,
    )


def _require_interval(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ValueError("expiry must be later than issue time")
    _time_text(start)
    _time_text(end)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _unique_keys(values: Iterable[RevocationKey]) -> tuple[RevocationKey, ...]:
    return tuple(dict.fromkeys(values))


def _record_value(record: AgentPairing | sqlite3.Row, name: str) -> str:
    if isinstance(record, AgentPairing):
        return str(getattr(record, name))
    return str(record[name])
