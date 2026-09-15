"""ADR 0008 credentials confined to an authenticated ADR 0024 Noise session.

The request signature uses ES256, 64-byte low-S P1363 and unpadded base64url.
Its bytes are REQUEST_DOMAIN followed by uint32-big-endian length framed UTF-8
fields: session_id, challenge, method, path, empty-body SHA-256 hexadecimal,
ticket purpose, Agent installation, creator account, key ID (pairing ID), and
Brain audience (installation ID). Agent derives all fixed and pinned fields;
only session_id and the challenge come from the encrypted challenge response.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from contextlib import contextmanager
from datetime import timedelta, timezone
from functools import wraps
from threading import Event, RLock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    AgentChallengeBinding,
    AuthenticationStateError,
    SQLiteAuthenticationStore,
    TicketBinding,
    TicketIssue,
    TicketPurpose,
)
from app.persistence.companion_pairing import (
    CompanionPairingPersistence,
    CompanionPairingPersistenceError,
    CompanionSessionSnapshot,
)
from app.security import companion_pairing_proof as pairing_proof

REQUEST_DOMAIN = b"OFCA-AGENT-REQUEST-V1\x00"
REQUEST_METHOD = "POST"
REQUEST_PATH = "/agent/session-ticket"
EMPTY_BODY_DIGEST = hashlib.sha256(b"").hexdigest()
CHALLENGE_LIFETIME_SECONDS = 30
TICKET_LIFETIME_SECONDS = 30
MAX_SESSION_CHALLENGES = 8
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class CompanionSessionError(RuntimeError):
    def __init__(self):
        super().__init__("companion_session_refused")


def _boundary(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (
            AuthenticationStateError,
            CompanionPairingPersistenceError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise CompanionSessionError() from None

    return guarded


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def request_signing_message(
    session_id: str, challenge: str, snapshot: CompanionSessionSnapshot
) -> bytes:
    pin = snapshot.pin
    fields = (
        session_id,
        challenge,
        REQUEST_METHOD,
        REQUEST_PATH,
        EMPTY_BODY_DIGEST,
        TicketPurpose.AGENT_WEBSOCKET.value,
        pin.agent_installation_id,
        pin.creator_account_id,
        _encode(pin.pairing_id),
        pin.installation_id,
    )
    return REQUEST_DOMAIN + b"".join(
        struct.pack("!I", len(encoded)) + encoded
        for field in fields
        for encoded in (field.encode("utf-8"),)
    )


def _verify_request(
    snapshot: CompanionSessionSnapshot, session_id: str, challenge: str, signature: str
) -> bool:
    try:
        if not isinstance(signature, str) or len(signature) != 86:
            return False
        raw = base64.urlsafe_b64decode(signature + "==")
        if len(raw) != 64 or _encode(raw) != signature:
            return False
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not 0 < r < _P256_ORDER or not 0 < s <= _P256_ORDER // 2:
            return False
        jwk = json.loads(snapshot.pin.agent_identity_jwk)
        pairing_proof.jwk_thumbprint(jwk)
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "="), "big"),
            int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "="), "big"),
            ec.SECP256R1(),
        ).public_key()
        public.verify(
            encode_dss_signature(r, s),
            request_signing_message(session_id, challenge, snapshot),
            ec.ECDSA(hashes.SHA256()),
        )
        return True
    except (
        ValueError,
        TypeError,
        KeyError,
        InvalidSignature,
        pairing_proof.CompanionPairingProofError,
    ):
        return False


class CompanionSessionAuthority:
    def __init__(self, authentication: SQLiteAuthenticationStore):
        self.authentication = authentication
        self.persistence = CompanionPairingPersistence(authentication)

    @_boundary
    def prepare(self, pairing_id: bytes) -> CompanionSessionSnapshot:
        return self.persistence.session_authority(pairing_id)

    @_boundary
    def authorize(
        self, snapshot: CompanionSessionSnapshot
    ) -> ProtectedCompanionSession:
        """Called only by the Noise route after both fixed confirmations pass."""
        session_id = secrets.token_urlsafe(32)
        self.persistence.refresh_session_policy(snapshot, session_id)
        return ProtectedCompanionSession(self.authentication, snapshot, session_id)


class ProtectedCompanionSession:
    """One route-owned handle; never serialize it or reuse it for another socket."""

    def __init__(self, authentication, snapshot, session_id):
        self.authentication = authentication
        self.snapshot = snapshot
        self.binding = snapshot.binding(session_id)
        self.session_id = session_id
        self.expires_at = snapshot.expires_at
        self._closed = Event()
        self._lock = RLock()
        self._pending = None
        self._challenges = 0
        self._authenticated = False
        self._hello_accepted = False
        self._config_digest = None

    def _require_open(self):
        if self._closed.is_set():
            raise CompanionSessionError()

    def _request_binding(self):
        pin = self.snapshot.pin
        return AgentChallengeBinding(
            self.binding.principal_id,
            self.binding.pairing_id,
            REQUEST_METHOD,
            REQUEST_PATH,
            EMPTY_BODY_DIGEST,
            TicketPurpose.AGENT_WEBSOCKET,
            pin.agent_installation_id,
            pin.creator_account_id,
            self.binding.pairing_id,
            pin.installation_id,
        )

    @_boundary
    def current_policy(self):
        with self._lock:
            self._require_open()
            return self.authentication.companion_session_policy(self.binding)

    @contextmanager
    def operation(self):
        with self._lock:
            self._require_open()
            try:
                with self.authentication.companion_session_operation(
                    self.binding
                ) as policy:
                    self._require_open()
                    yield policy
            except (
                AuthenticationStateError,
                CompanionPairingPersistenceError,
                sqlite3.Error,
            ):
                raise CompanionSessionError() from None

    @_boundary
    def challenge(self) -> dict:
        with self._lock:
            self._require_open()
            if (
                self._authenticated
                or self._pending is not None
                or self._challenges >= MAX_SESSION_CHALLENGES
            ):
                raise CompanionSessionError()
            expires = min(
                self.expires_at,
                self.authentication._now()
                + timedelta(seconds=CHALLENGE_LIFETIME_SECONDS),
            )
            pending = self.authentication.issue_companion_challenge(
                self.binding,
                self._request_binding(),
                expires_at=expires,
            )
            self._pending = pending
            self._challenges += 1
            return {
                "challenge_id": pending.challenge_id,
                "challenge": pending.value,
                "session_id": self.session_id,
                "expires_at": pending.expires_at.astimezone(timezone.utc).isoformat(),
            }

    @_boundary
    def authenticate(self, challenge_id: str, signature: str) -> dict:
        with self._lock:
            self._require_open()
            pending, self._pending = self._pending, None
            if (
                pending is None
                or self._authenticated
                or challenge_id != pending.challenge_id
            ):
                raise CompanionSessionError()
            consumed = self.authentication.consume_companion_challenge(
                self.binding,
                pending.value,
                self._request_binding(),
            )
            if consumed is None or not _verify_request(
                self.snapshot, self.session_id, pending.value, signature
            ):
                raise CompanionSessionError()
            self._require_open()
            self._authenticated = True
            ticket = self._issue_ticket(TicketPurpose.AGENT_WEBSOCKET)
            return {
                "auth_ticket": ticket,
                "creator_account_id": self.binding.creator_account_id,
            }

    def _issue_ticket(self, purpose):
        self._require_open()
        if not self._authenticated:
            raise CompanionSessionError()
        expires = min(
            self.expires_at,
            self.authentication._now() + timedelta(seconds=TICKET_LIFETIME_SECONDS),
        )
        return self.authentication.issue_companion_ticket(
            self.binding,
            TicketIssue(
                purpose=purpose,
                principal_id=self.binding.principal_id,
                role="agent",
                creator_account_id=self.binding.creator_account_id,
                expires_at=expires,
                parent_pairing_id=self.binding.pairing_id,
                expected_agent_installation_id=self.binding.agent_installation_id,
            ),
        ).value

    def _ticket_binding(self, purpose, account, agent_id):
        if (
            account != self.binding.creator_account_id
            or str(agent_id) != self.binding.agent_installation_id
        ):
            raise CompanionSessionError()
        return TicketBinding(
            purpose,
            account,
            "agent",
            expected_agent_installation_id=str(agent_id),
        )

    @_boundary
    def accept_hello(self, ticket: str, account: str, agent_id):
        with self._lock:
            self._require_open()
            if not self._authenticated or self._hello_accepted:
                raise CompanionSessionError()
            consumed = self.authentication.consume_companion_ticket(
                self.binding,
                ticket,
                self._ticket_binding(TicketPurpose.AGENT_WEBSOCKET, account, agent_id),
            )
            if consumed is None:
                raise CompanionSessionError()
            self._hello_accepted = True
            reconnect = self._issue_ticket(TicketPurpose.AGENT_WEBSOCKET)
            config = self._issue_ticket(TicketPurpose.AGENT_CONFIG)
            return self.binding.principal_id, account, reconnect, config

    @_boundary
    def validate_config(self, ticket: str, account: str, agent_id) -> None:
        with self._lock:
            self._require_open()
            if not self._hello_accepted or not isinstance(ticket, str):
                raise CompanionSessionError()
            binding = self._ticket_binding(
                TicketPurpose.AGENT_CONFIG, account, agent_id
            )
            digest = hashlib.sha256(ticket.encode("utf-8")).digest()
            if self._config_digest is None:
                consumed = self.authentication.consume_companion_ticket(
                    self.binding, ticket, binding
                )
                if consumed is None:
                    raise CompanionSessionError()
                self._config_digest = digest
            elif not secrets.compare_digest(digest, self._config_digest):
                raise CompanionSessionError()
            self.authentication.companion_session_policy(self.binding)

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._pending = None
            self._config_digest = None
            self._authenticated = False
            try:
                self.authentication.close_companion_session(self.session_id)
            except (sqlite3.Error, AuthenticationStateError):
                # The closed in-memory capability cannot be used again, even if
                # durable invalidation is temporarily unavailable.
                raise CompanionSessionError() from None
