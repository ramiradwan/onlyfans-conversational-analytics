"""Review prototype: install Noise pins from a purpose-specific signed receipt.

No issuer service or production trust anchors are defined here. The caller
supplies independently authenticated local expectations and pinned issuer keys.
"""

import base64
from dataclasses import dataclass
import hashlib
import json
import re
from threading import Lock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from session import PROFILE, Session, SessionError

TYPE = "ofca-companion-pairing+jwt"
AUDIENCE = "urn:ofca:companion-pairing:v1"
FIELDS = {
    "iss", "aud", "iat", "exp", "jti", "suite", "installation_id", "agent_id",
    "account_id", "agent_key", "brain_key", "agent_nonce", "brain_nonce",
    "generation", "grant_digest",
}


def encode_key(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError("invalid key encoding")
    raw = base64.urlsafe_b64decode(value + "=")
    if len(raw) != 32 or encode_key(raw) != value:
        raise ValueError("invalid key encoding")
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


@dataclass(frozen=True)
class Expectations:
    issuer: str
    installation_id: str
    agent_id: str
    account_id: str
    agent_nonce: str
    brain_nonce: str
    generation: int
    grant_digest: str
    local_public_key: bytes
    initiator: bool
    deadline: int


@dataclass(frozen=True)
class Pins:
    peer_key: bytes
    local_key: bytes
    binding: bytes
    receipt_id: str
    expires: int


class PairingGate:
    """In-memory atomic admission model; production requires durable transactions."""
    def __init__(self, expected, issuer_keys):
        self.expected = expected
        self.issuer_keys = dict(issuer_keys)
        self._lock = Lock()
        self._pins = None
        self._closed = False
        self._sessions = []

    def cancel_or_revoke(self):
        with self._lock:
            self._closed = True
            self._pins = None
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def accept(self, token, now):
        # Verification occurs before a single atomic admission, so cancellation
        # while verifying cannot commit a pin after the cancellation fence.
        try:
            if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 2:
                raise ValueError("invalid receipt")
            header = jwt.get_unverified_header(token)
            if set(header) != {"alg", "kid", "typ"} or header["alg"] != "ES256" or header["typ"] != TYPE:
                raise ValueError("invalid header")
            key = self.issuer_keys[header["kid"]]
            # Never follow jku/x5u or derive an algorithm from the receipt.
            claims = jwt.decode(token, key, algorithms=["ES256"],
                                audience=AUDIENCE, issuer=self.expected.issuer,
                                options={"require": list(FIELDS), "verify_exp": False,
                                         "verify_iat": False, "verify_nbf": False,
                                         "strict_aud": True})
            for segment in token.split(".")[:2]:
                json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)), object_pairs_hook=unique_object)
            if set(claims) != FIELDS or claims["suite"] != PROFILE.decode():
                raise ValueError("invalid profile")
            for name in ("iat", "exp", "generation"):
                if type(claims[name]) is not int or not 0 <= claims[name] < 2**53:
                    raise ValueError("invalid integer")
            if not claims["iat"] <= now < claims["exp"] <= claims["iat"] + 300:
                raise ValueError("invalid validity")
            if type(now) is not int or now >= self.expected.deadline:
                raise ValueError("pairing expired")
            for name in ("installation_id", "agent_id", "account_id", "agent_nonce", "brain_nonce", "generation", "grant_digest"):
                if claims[name] != getattr(self.expected, name):
                    raise ValueError("context mismatch")
            for name in ("jti", "agent_nonce", "brain_nonce", "grant_digest"):
                if not isinstance(claims[name], str) or not re.fullmatch(r"[0-9a-f]{64}", claims[name]):
                    raise ValueError("invalid digest or challenge")
            for name in ("installation_id", "agent_id", "account_id"):
                if not isinstance(claims[name], str) or not 1 <= len(claims[name]) <= 200:
                    raise ValueError("invalid identifier")
            local = decode_key(claims["agent_key" if self.expected.initiator else "brain_key"])
            peer = decode_key(claims["brain_key" if self.expected.initiator else "agent_key"])
            if local != self.expected.local_public_key or local == peer:
                raise ValueError("key substitution")
            # Signature-independent context avoids ECDSA encoding variants
            # creating different Noise transcripts for the same signed claims.
            binding = hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).digest()
            pins = Pins(peer, local, binding, claims["jti"], claims["exp"])
        except Exception:
            raise SessionError("pairing_receipt_refused") from None
        with self._lock:
            if self._closed or self._pins is not None:
                raise SessionError("pairing_state_refused")
            self._pins = pins
        return pins

    def new_session(self, private, now):
        with self._lock:
            if self._closed or self._pins is None or now >= self._pins.expires:
                raise SessionError("pairing_state_refused")
            actual = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            if actual != self._pins.local_key:
                raise SessionError("pairing_key_refused")
            # One active owner per pairing; replacing it fences the old session.
            for previous in self._sessions:
                previous.close()
            self._sessions.clear()
            session = Session(private, self._pins.peer_key, self.expected.initiator, self._pins.binding)
            self._sessions.append(session)
            return session
