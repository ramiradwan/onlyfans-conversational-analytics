"""Typed durable storage for local authentication authority."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol
from uuid import uuid4

from app.persistence.database import AuthSQLite
from app.persistence.migrations import MigrationRunner
from app.security.grant_types import (
    ACCOUNT_AUTHORITY_GRANT_TYPES,
    AGENT_PAIRING_GRANT_TYPES,
    CREATOR_ACCOUNT_BINDING,
)
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


class ProvisioningCandidateState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class ClaimSubmissionState(str, Enum):
    SUBMITTED = "submitted"
    CONSUMED = "consumed"
    REFUSED = "refused"


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
    membership_roles: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedAccountBinding:
    """One creator account this installation has durably authorized.

    A verified grant reference records what the hosted control plane signed.
    This record states that local finalization accepted the account, and it
    retains the provenance of the grant tuple the acceptance rested on:
    `grant_bundle_sha256` digests the whole tuple and `grant_reference_ids`
    names its members. Eligibility is not stored; it is resolved from this
    record and current grant state.
    """

    creator_account_id: str
    installation_id: str
    platform_creator_id: str
    association_request_id: str
    grant_bundle_sha256: str
    authorized_at: datetime
    grant_reference_ids: tuple[str, ...]
    revoked_at: datetime | None = None


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


@dataclass(frozen=True, slots=True)
class ProvisioningCandidate:
    """One locally detected upstream account awaiting or holding hosted approval.

    Nonsecret recovery/state coordinates only: no claim secret, compact grant,
    or hosted credential ever belongs on this record.
    """

    association_request_id: str
    installation_id: str
    onboarding_transaction_id: str
    organization_id: str
    creator_account_id: str
    state: ProvisioningCandidateState
    requested_at: datetime
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimSubmission:
    """One pasted installation claim, recorded before it is consumed.

    The four coordinates are the claim's nonsecret recovery identity: they name
    the claim without carrying it. The claim secret, its challenge, and any
    hosted credential are excluded by construction and must stay excluded.

    `state` distinguishes a claim whose hosted outcome is still unknown
    (`SUBMITTED`) from one that resolved, which is what makes a claim
    interrupted mid-consumption recoverable. `outcome` carries the nonsecret
    refusal reason of a refused claim and is absent otherwise.
    """

    claim_id: str
    onboarding_transaction_id: str
    organization_id: str
    installation_id: str
    submitted_at: datetime
    state: ClaimSubmissionState = ClaimSubmissionState.SUBMITTED
    outcome: str | None = None
    resolved_at: datetime | None = None


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
        identity: AuthContext | None = None,
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

    def scope_is_revoked(self, key: RevocationKey) -> bool: ...

    def record_authorized_account_binding(
        self, binding: AuthorizedAccountBinding
    ) -> None: ...

    def authorized_account_bindings(
        self,
    ) -> tuple[AuthorizedAccountBinding, ...]: ...

    def revoke_authorized_account_binding(self, creator_account_id: str) -> bool: ...

    def record_provisioning_candidate(
        self, candidate: ProvisioningCandidate
    ) -> None: ...

    def provisioning_candidate(
        self, association_request_id: str
    ) -> ProvisioningCandidate | None: ...

    def pending_provisioning_candidate(
        self, installation_id: str
    ) -> ProvisioningCandidate | None: ...

    def approve_provisioning_candidate(
        self, association_request_id: str, *, resolved_at: datetime
    ) -> bool: ...

    def cancel_provisioning_candidate(
        self, association_request_id: str, *, resolved_at: datetime
    ) -> bool: ...

    def record_claim_submission(self, submission: ClaimSubmission) -> None: ...

    def resolve_claim_submission(
        self, claim_id: str, *, outcome: str | None, resolved_at: datetime
    ) -> bool: ...

    def claim_submission(self, claim_id: str) -> ClaimSubmission | None: ...

    def unresolved_claim_submissions(self) -> tuple[ClaimSubmission, ...]: ...

    def consumed_claim_submissions(self) -> tuple[ClaimSubmission, ...]: ...


class AuthenticationStateError(ValueError):
    """Raised when an authentication object cannot be issued from current state."""


AccountBindingRefusal = Literal[
    "additional_account_binding_unsupported",
    "account_binding_already_recorded",
]


class AccountBindingRefused(AuthenticationStateError):
    """Refusal carrying a fixed message and a nonsecret reason code."""

    def __init__(self, reason: AccountBindingRefusal) -> None:
        super().__init__("Authorized account binding is refused")
        self.reason = reason


_SHA256_TEXT = re.compile(r"[0-9a-f]{64}")


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
        identity: AuthContext | None = None,
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
        membership_roles = (
            None
            if grant.membership_roles is None
            else json.dumps(
                list(grant.membership_roles),
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
                allowed_creator_account_ids, membership_roles
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                membership_roles,
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

    def scope_is_revoked(self, key: RevocationKey) -> bool:
        """Report whether one revocation scope has been revoked."""

        with self.database.read() as connection:
            return self._scope_is_revoked(connection, key)

    @staticmethod
    def _scope_is_revoked(
        connection: sqlite3.Connection, key: RevocationKey
    ) -> bool:
        row = connection.execute(
            """
            SELECT revoked_at FROM auth_revocation_state
            WHERE scope_type = ? AND scope_id = ?
            """,
            (key.scope_type.value, key.scope_id),
        ).fetchone()
        return row is not None and row["revoked_at"] is not None

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
                row["grant_type"] == CREATOR_ACCOUNT_BINDING
                and stored_account != creator_account_id
            )
            scoped_reference_mismatch = (
                row["grant_type"] != CREATOR_ACCOUNT_BINDING
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
        self._require_grant_types(connection, grants, ACCOUNT_AUTHORITY_GRANT_TYPES)
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
        self._require_grant_types(connection, grants, AGENT_PAIRING_GRANT_TYPES)
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
        required: tuple[str, ...],
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
        if not set(required) <= found:
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

    def record_provisioning_candidate(
        self, candidate: ProvisioningCandidate
    ) -> None:
        """Persist one newly detected candidate as the installation's pending slot.

        Fails closed if another candidate is already pending for the same
        installation: a new/different candidate requires the caller to cancel
        the pending transaction first (competition is scoped to one candidate).
        Approved rows are never touched here and are never overwritten.
        """

        _require_provisioning_candidate(candidate)
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM provisioning_candidates
                WHERE installation_id = ? AND state = 'pending'
                """,
                (candidate.installation_id,),
            ).fetchone()
            if existing is not None:
                raise AuthenticationStateError(
                    "A provisioning candidate is already pending for this installation"
                )
            connection.execute(
                """
                INSERT INTO provisioning_candidates (
                    association_request_id, installation_id,
                    onboarding_transaction_id, organization_id,
                    creator_account_id, state, requested_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    candidate.association_request_id,
                    candidate.installation_id,
                    candidate.onboarding_transaction_id,
                    candidate.organization_id,
                    candidate.creator_account_id,
                    candidate.state.value,
                    _time_text(candidate.requested_at),
                ),
            )
            self._increment_authorization_epoch(connection)

    def provisioning_candidate(
        self, association_request_id: str
    ) -> ProvisioningCandidate | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM provisioning_candidates
                WHERE association_request_id = ?
                """,
                (association_request_id,),
            ).fetchone()
        return None if row is None else _provisioning_candidate(row)

    def pending_provisioning_candidate(
        self, installation_id: str
    ) -> ProvisioningCandidate | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM provisioning_candidates
                WHERE installation_id = ? AND state = 'pending'
                """,
                (installation_id,),
            ).fetchone()
        return None if row is None else _provisioning_candidate(row)

    def approve_provisioning_candidate(
        self, association_request_id: str, *, resolved_at: datetime
    ) -> bool:
        """Atomically transition one pending candidate to approved.

        This is the approval transition: it only ever succeeds against a row
        that is still `pending`. A candidate already resolved (approved or
        cancelled) by a competing transition is left untouched and this
        returns False rather than resurrecting or double-applying it.
        """

        return self._resolve_provisioning_candidate(
            association_request_id,
            resolved_state=ProvisioningCandidateState.APPROVED,
            resolved_at=resolved_at,
        )

    def cancel_provisioning_candidate(
        self, association_request_id: str, *, resolved_at: datetime
    ) -> bool:
        return self._resolve_provisioning_candidate(
            association_request_id,
            resolved_state=ProvisioningCandidateState.CANCELLED,
            resolved_at=resolved_at,
        )

    def _resolve_provisioning_candidate(
        self,
        association_request_id: str,
        *,
        resolved_state: ProvisioningCandidateState,
        resolved_at: datetime,
    ) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE provisioning_candidates
                SET state = ?, resolved_at = ?
                WHERE association_request_id = ? AND state = 'pending'
                """,
                (
                    resolved_state.value,
                    _time_text(resolved_at),
                    association_request_id,
                ),
            )
            if cursor.rowcount == 1:
                self._increment_authorization_epoch(connection)
        return cursor.rowcount == 1

    def record_claim_submission(self, submission: ClaimSubmission) -> None:
        """Persist one pasted claim's coordinates before it is consumed.

        The row is written unresolved so that a claim whose hosted outcome never
        arrives is still recoverable from durable state. Only the nonsecret
        coordinates are stored; the claim secret and challenge are not columns
        of this table and must never become columns of it.

        A resubmission of the same claim reopens the existing row rather than
        adding a second one, except once the claim is recorded as consumed:
        that outcome is terminal and a later attempt cannot erase it. Different
        coordinates under a recorded claim identifier are refused. Nothing here
        is an authorization input, so the authorization epoch is not touched.
        """

        _require_claim_submission(submission)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM provisioning_claim_submissions WHERE claim_id = ?",
                (submission.claim_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO provisioning_claim_submissions (
                        claim_id, onboarding_transaction_id, organization_id,
                        installation_id, state, outcome, submitted_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)
                    """,
                    (
                        submission.claim_id,
                        submission.onboarding_transaction_id,
                        submission.organization_id,
                        submission.installation_id,
                        ClaimSubmissionState.SUBMITTED.value,
                        _time_text(submission.submitted_at),
                    ),
                )
                return
            recorded = _claim_submission(existing)
            if _claim_coordinates(recorded) != _claim_coordinates(submission):
                raise AuthenticationStateError(
                    "Claim submission coordinates do not match the recorded claim"
                )
            if recorded.state is ClaimSubmissionState.CONSUMED:
                return
            connection.execute(
                """
                UPDATE provisioning_claim_submissions
                SET state = ?, outcome = NULL, submitted_at = ?, resolved_at = NULL
                WHERE claim_id = ?
                """,
                (
                    ClaimSubmissionState.SUBMITTED.value,
                    _time_text(submission.submitted_at),
                    submission.claim_id,
                ),
            )

    def resolve_claim_submission(
        self, claim_id: str, *, outcome: str | None, resolved_at: datetime
    ) -> bool:
        """Record what became of one submitted claim.

        `outcome` is None when the hosted plane consumed the claim, and a
        nonsecret refusal reason otherwise. A claim already recorded as consumed
        is left untouched. Returns whether a row moved.
        """

        if outcome is not None and not outcome:
            raise ValueError("Claim submission outcome must not be empty")
        state = (
            ClaimSubmissionState.CONSUMED
            if outcome is None
            else ClaimSubmissionState.REFUSED
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE provisioning_claim_submissions
                SET state = ?, outcome = ?, resolved_at = ?
                WHERE claim_id = ? AND state <> ?
                """,
                (
                    state.value,
                    outcome,
                    _time_text(resolved_at),
                    claim_id,
                    ClaimSubmissionState.CONSUMED.value,
                ),
            )
        return cursor.rowcount == 1

    def claim_submission(self, claim_id: str) -> ClaimSubmission | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_claim_submissions WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return None if row is None else _claim_submission(row)

    def unresolved_claim_submissions(self) -> tuple[ClaimSubmission, ...]:
        """Return every claim whose hosted outcome is still unknown."""

        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM provisioning_claim_submissions
                WHERE state = ?
                ORDER BY submitted_at, claim_id
                """,
                (ClaimSubmissionState.SUBMITTED.value,),
            ).fetchall()
        return tuple(_claim_submission(row) for row in rows)

    def consumed_claim_submissions(self) -> tuple[ClaimSubmission, ...]:
        """Return the durable coordinates of claims consumed by the hosted plane.

        A consumed claim is the local enrollment record.  Its coordinates may
        start a creator-association request, unlike an unresolved claim whose
        remote outcome is still unknown.
        """

        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM provisioning_claim_submissions
                WHERE state = ?
                ORDER BY submitted_at, claim_id
                """,
                (ClaimSubmissionState.CONSUMED.value,),
            ).fetchall()
        return tuple(_claim_submission(row) for row in rows)

    def record_authorized_account_binding(
        self, binding: AuthorizedAccountBinding
    ) -> None:
        """Durably authorize one creator account for this installation.

        Pilot cardinality: at most one authorized account exists per
        installation. A second, different account is refused, and so is a
        repeat of an account already recorded, rather than overwriting the
        provenance of the first. Revoked rows still count, so revoking one
        account does not open a slot for another.
        """

        _require_authorized_account_binding(binding)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT creator_account_id FROM authorized_account_bindings"
            ).fetchall()
            if any(
                str(row["creator_account_id"]) == binding.creator_account_id
                for row in existing
            ):
                raise AccountBindingRefused("account_binding_already_recorded")
            if existing:
                raise AccountBindingRefused(
                    "additional_account_binding_unsupported"
                )
            connection.execute(
                """
                INSERT INTO authorized_account_bindings (
                    creator_account_id, installation_id, platform_creator_id,
                    association_request_id, grant_bundle_sha256, authorized_at,
                    revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    binding.creator_account_id,
                    binding.installation_id,
                    binding.platform_creator_id,
                    binding.association_request_id,
                    binding.grant_bundle_sha256,
                    _time_text(binding.authorized_at),
                ),
            )
            self._insert_grants(
                connection,
                "authorized_account_binding_grants",
                "creator_account_id",
                binding.creator_account_id,
                _unique(binding.grant_reference_ids),
            )
            self._ensure_scope(
                connection,
                RevocationKey(
                    RevocationScopeType.CREATOR_ACCOUNT,
                    binding.creator_account_id,
                ),
            )
            self._increment_authorization_epoch(connection)

    def authorized_account_bindings(
        self,
    ) -> tuple[AuthorizedAccountBinding, ...]:
        """Return every authorized account binding, revoked ones included.

        Eligibility is decided by the resolver, not here: a caller that needs
        the currently eligible accounts must apply the revocation and validity
        predicates rather than treating this collection as an allowlist.
        """

        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM authorized_account_bindings
                ORDER BY authorized_at, creator_account_id
                """
            ).fetchall()
            return tuple(
                _authorized_account_binding(
                    row,
                    grants=tuple(
                        str(grant["grant_reference_id"])
                        for grant in connection.execute(
                            """
                            SELECT grant_reference_id
                            FROM authorized_account_binding_grants
                            WHERE creator_account_id = ?
                            ORDER BY grant_reference_id
                            """,
                            (row["creator_account_id"],),
                        ).fetchall()
                    ),
                )
                for row in rows
            )

    def revoke_authorized_account_binding(self, creator_account_id: str) -> bool:
        """Revoke one authorized account binding and its account scope."""

        with self.database.transaction() as connection:
            timestamp = _time_text(self._now())
            cursor = connection.execute(
                """
                UPDATE authorized_account_bindings SET revoked_at = ?
                WHERE creator_account_id = ? AND revoked_at IS NULL
                """,
                (timestamp, creator_account_id),
            )
            if cursor.rowcount != 1:
                return False
            self._revoke_in_transaction(
                connection,
                RevocationKey(
                    RevocationScopeType.CREATOR_ACCOUNT, creator_account_id
                ),
                reason="account_binding_revoked",
            )
            self._increment_authorization_epoch(connection)
        return True

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


def _require_provisioning_candidate(candidate: ProvisioningCandidate) -> None:
    if not all(
        (
            candidate.association_request_id,
            candidate.installation_id,
            candidate.onboarding_transaction_id,
            candidate.organization_id,
            candidate.creator_account_id,
        )
    ):
        raise ValueError("Provisioning candidate fields must not be empty")
    if candidate.state is not ProvisioningCandidateState.PENDING:
        raise ValueError("A newly recorded provisioning candidate must start pending")
    if candidate.resolved_at is not None:
        raise ValueError("A newly recorded provisioning candidate must not be resolved")
    _time_text(candidate.requested_at)


def _provisioning_candidate(row: sqlite3.Row) -> ProvisioningCandidate:
    resolved = row["resolved_at"]
    return ProvisioningCandidate(
        association_request_id=str(row["association_request_id"]),
        installation_id=str(row["installation_id"]),
        onboarding_transaction_id=str(row["onboarding_transaction_id"]),
        organization_id=str(row["organization_id"]),
        creator_account_id=str(row["creator_account_id"]),
        state=ProvisioningCandidateState(str(row["state"])),
        requested_at=_parse_time(str(row["requested_at"])),
        resolved_at=None if resolved is None else _parse_time(str(resolved)),
    )


def _require_claim_submission(submission: ClaimSubmission) -> None:
    if not all(_claim_coordinates(submission)):
        raise ValueError("Claim submission coordinates must not be empty")
    if submission.state is not ClaimSubmissionState.SUBMITTED:
        raise ValueError("A newly recorded claim submission must start submitted")
    if submission.outcome is not None or submission.resolved_at is not None:
        raise ValueError("A newly recorded claim submission must not be resolved")
    _time_text(submission.submitted_at)


def _claim_coordinates(submission: ClaimSubmission) -> tuple[str, str, str, str]:
    return (
        submission.claim_id,
        submission.onboarding_transaction_id,
        submission.organization_id,
        submission.installation_id,
    )


def _claim_submission(row: sqlite3.Row) -> ClaimSubmission:
    outcome = row["outcome"]
    resolved = row["resolved_at"]
    return ClaimSubmission(
        claim_id=str(row["claim_id"]),
        onboarding_transaction_id=str(row["onboarding_transaction_id"]),
        organization_id=str(row["organization_id"]),
        installation_id=str(row["installation_id"]),
        submitted_at=_parse_time(str(row["submitted_at"])),
        state=ClaimSubmissionState(str(row["state"])),
        outcome=None if outcome is None else str(outcome),
        resolved_at=None if resolved is None else _parse_time(str(resolved)),
    )


def _require_authorized_account_binding(binding: AuthorizedAccountBinding) -> None:
    if not all(
        (
            binding.creator_account_id,
            binding.installation_id,
            binding.platform_creator_id,
            binding.association_request_id,
        )
    ):
        raise ValueError("Authorized account binding fields must not be empty")
    if _SHA256_TEXT.fullmatch(binding.grant_bundle_sha256) is None:
        raise ValueError("Grant bundle digest must be lowercase SHA-256")
    if not binding.grant_reference_ids:
        raise ValueError("Authorized account binding requires its grant references")
    if binding.revoked_at is not None:
        raise ValueError("A newly recorded account binding must not be revoked")
    _time_text(binding.authorized_at)


def _authorized_account_binding(
    row: sqlite3.Row, *, grants: tuple[str, ...]
) -> AuthorizedAccountBinding:
    revoked = row["revoked_at"]
    return AuthorizedAccountBinding(
        creator_account_id=str(row["creator_account_id"]),
        installation_id=str(row["installation_id"]),
        platform_creator_id=str(row["platform_creator_id"]),
        association_request_id=str(row["association_request_id"]),
        grant_bundle_sha256=str(row["grant_bundle_sha256"]),
        authorized_at=_parse_time(str(row["authorized_at"])),
        grant_reference_ids=grants,
        revoked_at=None if revoked is None else _parse_time(str(revoked)),
    )


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
    roles_value = row["membership_roles"]
    membership_roles: tuple[str, ...] | None = None
    if roles_value is not None:
        parsed = json.loads(str(roles_value))
        if not isinstance(parsed, list) or not all(
            isinstance(value, str) for value in parsed
        ):
            raise AuthenticationStateError("Verified grant membership roles are invalid")
        membership_roles = tuple(parsed)
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
        membership_roles=membership_roles,
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
