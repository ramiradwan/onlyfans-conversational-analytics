"""Review prototype: install Noise pins from a purpose-specific signed receipt.

No issuer service or production trust anchors are defined here. The caller
supplies independently authenticated local expectations and purpose-scoped
issuer keys. The verifier intentionally models a closed production profile.
"""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import re
import struct
from threading import Lock

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from session import PROFILE, Session, SessionError

TYPE = "ofca-companion-pairing+jwt"
AUDIENCE = "urn:ofca:companion-pairing:v1"
PAIRING_KEY_PURPOSE = "pairing-receipt"
MAX_RECEIPT = 8192
MAX_HEADER = 512
MAX_PAYLOAD = 6144
MAX_RECEIPT_LIFETIME = 300

_BINDING_DOMAIN = b"OFCA-COMPANION-PAIRING-BINDING-V1\x00"
_ENROLLMENT_DOMAIN = b"OFCA-COMPANION-PAIRING-ENROLLMENT-V1\x00"
_PROOF_DOMAIN = b"OFCA-COMPANION-PAIRING-PROOF-V1\x00"
_GRANT_DOMAIN = b"OFCA-COMPANION-PAIRING-GRANTS-V1\x00"
_PAIRING_GRANT_TYPES = ("creator_account_binding", "installation_grant")
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{64}$")

FIELDS = {
    "iss", "aud", "iat", "exp", "jti", "suite",
    "organization_id", "installation_id", "installation_key_id",
    "installation_key_jkt", "agent_id", "agent_identity_key_id",
    "agent_identity_key_jkt", "account_id", "pairing_id",
    "agent_key", "brain_key", "agent_nonce", "brain_nonce", "generation",
    "grant_digest", "approval_id", "approval_revision", "offline_not_after",
}

_CONTEXT_FIELDS = (
    "organization_id", "installation_id", "installation_key_id",
    "installation_key_jkt", "agent_id", "agent_identity_key_id",
    "agent_identity_key_jkt", "account_id", "pairing_id", "agent_nonce",
    "brain_nonce", "generation", "grant_digest", "approval_id",
    "approval_revision", "offline_not_after",
)


@dataclass(frozen=True)
class IssuerKey:
    purpose: str
    public_key: ec.EllipticCurvePublicKey


@dataclass(frozen=True)
class Expectations:
    issuer: str
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    agent_id: str
    agent_identity_key_id: str
    agent_identity_key_jkt: str
    account_id: str
    pairing_id: str
    agent_nonce: str
    brain_nonce: str
    generation: int
    grant_digest: str
    approval_id: str
    approval_revision: int
    offline_not_after: int
    local_public_key: bytes
    initiator: bool
    deadline: int


@dataclass(frozen=True)
class Pins:
    peer_key: bytes
    local_key: bytes
    binding: bytes
    receipt_id: str
    pairing_id: str
    generation: int
    grant_digest: str
    approval_id: str
    approval_revision: int
    issued_at: int
    receipt_expires: int
    offline_not_after: int
    issuer_kid: str


def encode_key(value):
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("invalid key")
    return _b64url(value)


def decode_key(value):
    raw = _b64url_decode(value)
    if len(raw) != 32 or len(value) != 43:
        raise ValueError("invalid key encoding")
    return raw


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    if not isinstance(value, str) or not _B64U_RE.fullmatch(value) or "=" in value:
        raise ValueError("noncanonical base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("noncanonical base64url") from exc
    if _b64url(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _strict_object(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid json object")
    return value


def _u64(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("invalid integer")
    return struct.pack("!Q", value)


def _lp(domain, fields):
    result = bytearray(domain)
    for field in fields:
        if isinstance(field, str):
            field = field.encode("utf-8")
        elif type(field) is int:
            field = _u64(field)
        elif not isinstance(field, bytes):
            raise TypeError("invalid canonical field")
        result += struct.pack("!I", len(field)) + field
    return bytes(result)


def pairing_grant_digest(grants):
    """Digest exactly the verified grant provenance authorized for pairing."""
    if not isinstance(grants, dict) or set(grants) != set(_PAIRING_GRANT_TYPES):
        raise ValueError("invalid pairing grant set")
    fields = []
    for grant_type in _PAIRING_GRANT_TYPES:
        digest = grants[grant_type]
        if not isinstance(digest, str) or not _HEX32_RE.fullmatch(digest):
            raise ValueError("invalid pairing grant digest")
        fields.extend((grant_type, bytes.fromhex(digest)))
    return hashlib.sha256(_lp(_GRANT_DOMAIN, fields)).hexdigest()


def enrollment_digest(claims):
    """Hash the immutable enrollment tuple used by both possession proofs."""
    return hashlib.sha256(_lp(_ENROLLMENT_DOMAIN, (
        AUDIENCE, PROFILE.decode(), claims["organization_id"],
        claims["installation_id"], claims["installation_key_id"],
        claims["installation_key_jkt"], claims["agent_id"],
        claims["agent_identity_key_id"], claims["agent_identity_key_jkt"],
        claims["account_id"], claims["pairing_id"],
        decode_key(claims["agent_key"]), decode_key(claims["brain_key"]),
        bytes.fromhex(claims["agent_nonce"]), bytes.fromhex(claims["brain_nonce"]),
        claims["generation"], bytes.fromhex(claims["grant_digest"]),
        claims["approval_id"], claims["approval_revision"],
        claims["offline_not_after"],
    ))).digest()


def identity_proof_message(role, issuer_challenge, request_digest, identity_key_id):
    """Canonical message an enrolled identity signs for one pairing request."""
    if role not in {"agent", "brain"}:
        raise ValueError("invalid proof role")
    if not isinstance(issuer_challenge, bytes) or len(issuer_challenge) != 32:
        raise ValueError("invalid proof challenge")
    if not isinstance(request_digest, bytes) or len(request_digest) != 32:
        raise ValueError("invalid enrollment digest")
    if not isinstance(identity_key_id, str) or not _ID_RE.fullmatch(identity_key_id):
        raise ValueError("invalid identity key id")
    return _lp(_PROOF_DOMAIN, (
        "pairing-enrollment", AUDIENCE, role, issuer_challenge,
        request_digest, identity_key_id,
    ))


def receipt_binding(claims):
    """Canonical semantic receipt binding mixed into the Noise prologue.

    This deliberately excludes JWS signature bytes and `kid`. Key rotation or
    ECDSA re-signing therefore cannot split the transcript for the same verified
    authorization tuple.
    """
    return hashlib.sha256(_lp(_BINDING_DOMAIN, (
        TYPE, claims["iss"], claims["aud"], claims["iat"], claims["exp"],
        claims["jti"], claims["suite"], claims["organization_id"],
        claims["installation_id"], claims["installation_key_id"],
        claims["installation_key_jkt"], claims["agent_id"],
        claims["agent_identity_key_id"], claims["agent_identity_key_jkt"],
        claims["account_id"], claims["pairing_id"],
        decode_key(claims["agent_key"]), decode_key(claims["brain_key"]),
        bytes.fromhex(claims["agent_nonce"]), bytes.fromhex(claims["brain_nonce"]),
        claims["generation"], bytes.fromhex(claims["grant_digest"]),
        claims["approval_id"], claims["approval_revision"],
        claims["offline_not_after"],
    ))).digest()


def _verify_es256(public_key, signing_input, signature):
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or not isinstance(public_key.curve, ec.SECP256R1)
    ):
        raise ValueError("invalid issuer key")
    if len(signature) != 64:
        raise ValueError("invalid signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 1 <= r < _P256_ORDER or not 1 <= s <= _P256_ORDER // 2:
        raise ValueError("noncanonical signature")
    try:
        public_key.verify(
            encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
        )
    except InvalidSignature as exc:
        raise ValueError("invalid signature") from exc


def _parse_and_verify(token, expected_issuer, issuer_keys):
    if not isinstance(token, str) or len(token) > MAX_RECEIPT or token.count(".") != 2:
        raise ValueError("invalid receipt")
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header_raw = _b64url_decode(header_segment)
        payload_raw = _b64url_decode(payload_segment)
        signature = _b64url_decode(signature_segment)
    except ValueError:
        raise
    if len(header_raw) > MAX_HEADER or len(payload_raw) > MAX_PAYLOAD:
        raise ValueError("receipt component too large")
    header = _strict_object(header_raw)
    claims = _strict_object(payload_raw)
    if set(header) != {"alg", "kid", "typ"}:
        raise ValueError("invalid header")
    if header.get("alg") != "ES256" or header.get("typ") != TYPE:
        raise ValueError("invalid header")
    kid = header.get("kid")
    if not isinstance(kid, str) or not _ID_RE.fullmatch(kid):
        raise ValueError("invalid key id")
    entry = issuer_keys.get(kid)
    if not isinstance(entry, IssuerKey) or entry.purpose != PAIRING_KEY_PURPOSE:
        raise ValueError("unknown or wrong-purpose key")
    _verify_es256(
        entry.public_key,
        f"{header_segment}.{payload_segment}".encode("ascii"),
        signature,
    )
    if set(claims) != FIELDS:
        raise ValueError("invalid claims")
    if claims.get("iss") != expected_issuer or claims.get("aud") != AUDIENCE:
        raise ValueError("issuer or audience mismatch")
    return header, claims


def _valid_id(value):
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def _valid_jkt(value):
    try:
        return (
            isinstance(value, str)
            and len(value) == 43
            and len(_b64url_decode(value)) == 32
        )
    except ValueError:
        return False


class PairingGate:
    """In-memory atomic admission model; production requires durable transactions."""

    def __init__(
        self,
        expected,
        issuer_keys,
        *,
        highest_generation=0,
        revoked_through_generation=0,
        consumed_receipts=(),
    ):
        for value in (highest_generation, revoked_through_generation):
            if type(value) is not int or not 0 <= value < 2**53:
                raise ValueError("invalid durable generation state")
        consumed = set(consumed_receipts)
        if any(not isinstance(value, str) or not _HEX32_RE.fullmatch(value) for value in consumed):
            raise ValueError("invalid consumed receipt state")
        self.expected = expected
        self.issuer_keys = dict(issuer_keys)
        self._lock = Lock()
        self._pins = None
        self._closed = False
        self._sessions = []
        self._highest_generation = highest_generation
        self._revoked_through_generation = revoked_through_generation
        self._consumed_receipts = consumed

    def durable_replay_state(self):
        """Return the state production must persist atomically with pin admission."""
        with self._lock:
            return (
                self._highest_generation,
                self._revoked_through_generation,
                frozenset(self._consumed_receipts),
            )

    def cancel_or_revoke(self):
        with self._lock:
            self._closed = True
            generation = (
                self._pins.generation
                if self._pins is not None
                else self.expected.generation
            )
            self._revoked_through_generation = max(self._revoked_through_generation, generation)
            self._pins = None
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def accept(self, token, now):
        # Verification occurs before a single atomic admission, so cancellation
        # while verifying cannot commit a pin after the cancellation fence.
        try:
            header, claims = _parse_and_verify(
                token, self.expected.issuer, self.issuer_keys
            )
            if claims["suite"] != PROFILE.decode():
                raise ValueError("invalid profile")
            for name in (
                "iat", "exp", "generation", "approval_revision", "offline_not_after"
            ):
                _u64(claims[name])
            if claims["generation"] < 1 or claims["approval_revision"] < 1:
                raise ValueError("invalid generation or approval revision")
            if type(now) is not int or not 0 <= now < 2**53:
                raise ValueError("invalid verifier time")
            if not (
                claims["iat"]
                <= now
                < claims["exp"]
                <= claims["iat"] + MAX_RECEIPT_LIFETIME
            ):
                raise ValueError("invalid receipt validity")
            if claims["offline_not_after"] < claims["exp"]:
                raise ValueError("invalid offline lifetime")
            if now >= self.expected.deadline:
                raise ValueError("pairing expired")
            for name in _CONTEXT_FIELDS:
                if claims[name] != getattr(self.expected, name):
                    raise ValueError("context mismatch")
            for name in (
                "jti", "pairing_id", "agent_nonce", "brain_nonce", "grant_digest"
            ):
                if (
                    not isinstance(claims[name], str)
                    or not _HEX32_RE.fullmatch(claims[name])
                ):
                    raise ValueError("invalid digest or challenge")
            if claims["agent_nonce"] == claims["brain_nonce"]:
                raise ValueError("challenge collision")
            for name in (
                "organization_id", "installation_id", "installation_key_id",
                "agent_id", "agent_identity_key_id", "account_id", "approval_id",
            ):
                if not _valid_id(claims[name]):
                    raise ValueError("invalid identifier")
            for name in ("installation_key_jkt", "agent_identity_key_jkt"):
                if not _valid_jkt(claims[name]):
                    raise ValueError("invalid key thumbprint")
            local = decode_key(
                claims["agent_key" if self.expected.initiator else "brain_key"]
            )
            peer = decode_key(
                claims["brain_key" if self.expected.initiator else "agent_key"]
            )
            if (
                not isinstance(self.expected.local_public_key, bytes)
                or len(self.expected.local_public_key) != 32
                or local != self.expected.local_public_key
                or local == peer
            ):
                raise ValueError("key substitution")
            binding = receipt_binding(claims)
            pins = Pins(
                peer, local, binding, claims["jti"], claims["pairing_id"],
                claims["generation"], claims["grant_digest"], claims["approval_id"],
                claims["approval_revision"], claims["iat"], claims["exp"],
                claims["offline_not_after"], header["kid"],
            )
        except Exception:
            raise SessionError("pairing_receipt_refused") from None

        with self._lock:
            if self._closed or self._pins is not None:
                raise SessionError("pairing_state_refused")
            if (
                pins.receipt_id in self._consumed_receipts
                or pins.generation <= self._highest_generation
                or pins.generation <= self._revoked_through_generation
            ):
                raise SessionError("pairing_state_refused")
            self._pins = pins
            self._consumed_receipts.add(pins.receipt_id)
            self._highest_generation = pins.generation
        return pins

    def new_session(self, private, now):
        with self._lock:
            if (
                self._closed or self._pins is None or type(now) is not int
                or now < self._pins.issued_at
                or now >= self._pins.offline_not_after
                or self._pins.generation <= self._revoked_through_generation
            ):
                raise SessionError("pairing_state_refused")
            try:
                actual = (
                    X25519PrivateKey.from_private_bytes(private)
                    .public_key()
                    .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                )
            except (TypeError, ValueError):
                raise SessionError("pairing_key_refused") from None
            if actual != self._pins.local_key:
                raise SessionError("pairing_key_refused")
            # One active owner per pairing; replacing it fences the old session.
            for previous in self._sessions:
                previous.close()
            self._sessions.clear()
            session = Session(
                private, self._pins.peer_key, self.expected.initiator, self._pins.binding
            )
            self._sessions.append(session)
            return session
