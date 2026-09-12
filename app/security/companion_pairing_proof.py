"""Canonical local companion pairing proofs under ADR 0024.

These operations bind already verified grant references and public pairing
material. They do not authorize pairing: callers must enforce grant currentness,
revocation, account approval, deadlines, and the durable confirmation CAS.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import struct
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.persistence.auth import InstallationKeyReference, VerifiedGrantReference
from app.security.grant_types import MAX_GRANT_CHARACTERS
from app.security.installation_key import (
    INSTALLATION_PROOF_ALGORITHM,
    InstallationKeyAuthority,
    InstallationKeyError,
)

SUITE = "Noise_KK_25519_ChaChaPoly_SHA256"
SESSION_PROFILE = b"ofca-companion-session/v1;agent-to-brain;no-early-data"
_GRANTS_DOMAIN = b"OFCA-LOCAL-PAIRING-GRANTS-V1\x00"
_TRANSCRIPT_DOMAIN = b"OFCA-LOCAL-PAIRING-TRANSCRIPT-V1\x00"
_PROOF_DOMAIN = b"OFCA-LOCAL-PAIRING-PROOF-V1\x00"
_CODE_DOMAIN = b"OFCA-LOCAL-PAIRING-CODE-V1\x00"
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_JWS = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_SMALL_ORDER_POINTS = frozenset(
    bytes.fromhex(value)
    for value in (
        "00" * 32,
        "01" + "00" * 31,
        "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
        "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
        "ec" + "ff" * 30 + "7f",
        "ed" + "ff" * 30 + "7f",
        "ee" + "ff" * 30 + "7f",
    )
)
_REFUSAL_CODES = frozenset(
    {
        "pairing_message_invalid",
        "pairing_grant_refused",
        "pairing_key_refused",
        "pairing_nonce_refused",
        "pairing_generation_refused",
        "pairing_proof_refused",
    }
)


class CompanionPairingProofError(RuntimeError):
    """A fixed, payload-free refusal suitable for the pairing boundary."""

    def __init__(self, code: str = "pairing_proof_refused") -> None:
        self.code = code if code in _REFUSAL_CODES else "pairing_proof_refused"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class PairingTranscript:
    """Public transcript material; omit identifying material from diagnostics."""

    pairing_id: bytes
    generation: int
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    creator_account_id: str
    agent_installation_id: str
    agent_identity_key_jkt: str
    agent_noise_key: bytes
    brain_noise_key: bytes
    agent_nonce: bytes
    brain_nonce: bytes
    grant_digest: bytes

    def __post_init__(self) -> None:
        for value in (
            self.pairing_id,
            self.agent_noise_key,
            self.brain_noise_key,
            self.agent_nonce,
            self.brain_nonce,
            self.grant_digest,
        ):
            _require_bytes32(value)
        if type(self.generation) is not int or not 1 <= self.generation < 2**53:
            raise CompanionPairingProofError("pairing_generation_refused")
        for value in (
            self.organization_id,
            self.installation_id,
            self.installation_key_id,
            self.creator_account_id,
            self.agent_installation_id,
        ):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise CompanionPairingProofError("pairing_message_invalid")
        _decode(self.installation_key_jkt, 32)
        _decode(self.agent_identity_key_jkt, 32)
        for value in (self.agent_noise_key, self.brain_noise_key):
            if value[:31] + bytes([value[31] & 0x7F]) in _SMALL_ORDER_POINTS:
                raise CompanionPairingProofError("pairing_key_refused")
        if self.agent_noise_key == self.brain_noise_key:
            raise CompanionPairingProofError("pairing_key_refused")
        if self.agent_nonce == self.brain_nonce:
            raise CompanionPairingProofError("pairing_nonce_refused")


def build_pairing_transcript(
    *,
    installation_grant: VerifiedGrantReference,
    creator_account_binding: VerifiedGrantReference,
    installation_key: InstallationKeyReference,
    pairing_id: bytes,
    generation: int,
    agent_installation_id: str,
    agent_identity_jwk: dict[str, str],
    agent_noise_key: bytes,
    brain_noise_key: bytes,
    agent_nonce: bytes,
    brain_nonce: bytes,
) -> PairingTranscript:
    """Bind retained, verified references to the exact key and pairing material.

    The references must be selected under the caller's authorization snapshot;
    this helper does not replace rechecking that snapshot at commit time.
    """
    installation = installation_grant
    binding = creator_account_binding
    if (
        installation.grant_type != "installation_grant"
        or binding.grant_type != "creator_account_binding"
    ):
        raise CompanionPairingProofError("pairing_grant_refused")
    for name in (
        "organization_id",
        "installation_id",
        "installation_key_id",
        "installation_key_jkt",
        "issuer",
        "subject",
    ):
        if not getattr(installation, name) or getattr(installation, name) != getattr(
            binding, name
        ):
            raise CompanionPairingProofError("pairing_grant_refused")
    if (
        installation.creator_account_id is not None
        or not binding.creator_account_id
        or installation.installation_key_id != installation_key.installation_key_id
        or installation.installation_key_jkt != installation_key.installation_key_jkt
    ):
        raise CompanionPairingProofError("pairing_grant_refused")
    _installation_jwk(installation_key)
    grant_hashes = []
    for grant in (binding, installation):
        token = grant.compact_jws
        if (
            not isinstance(token, str)
            or not token.isascii()
            or len(token) > MAX_GRANT_CHARACTERS
            or not _JWS.fullmatch(token)
        ):
            raise CompanionPairingProofError("pairing_grant_refused")
        digest = hashlib.sha256(token.encode("ascii")).digest()
        if (
            not isinstance(grant.grant_digest, str)
            or not grant.grant_digest.isascii()
            or not secrets.compare_digest(digest.hex(), grant.grant_digest)
        ):
            raise CompanionPairingProofError("pairing_grant_refused")
        grant_hashes.extend((grant.grant_type, digest))
    digest = hashlib.sha256(_frame(_GRANTS_DOMAIN, tuple(grant_hashes))).digest()
    return PairingTranscript(
        pairing_id=pairing_id,
        generation=generation,
        organization_id=installation.organization_id,
        installation_id=installation.installation_id,
        installation_key_id=installation.installation_key_id,
        installation_key_jkt=installation.installation_key_jkt,
        creator_account_id=binding.creator_account_id,
        agent_installation_id=agent_installation_id,
        agent_identity_key_jkt=jwk_thumbprint(agent_identity_jwk),
        agent_noise_key=agent_noise_key,
        brain_noise_key=brain_noise_key,
        agent_nonce=agent_nonce,
        brain_nonce=brain_nonce,
        grant_digest=digest,
    )


def pairing_transcript(transcript: PairingTranscript) -> bytes:
    """Return the fixed suite and contract field order, never JSON encoding."""
    return _frame(
        _TRANSCRIPT_DOMAIN,
        (
            SUITE,
            transcript.pairing_id,
            struct.pack("!Q", transcript.generation),
            transcript.organization_id,
            transcript.installation_id,
            transcript.installation_key_id,
            transcript.installation_key_jkt,
            transcript.creator_account_id,
            transcript.agent_installation_id,
            transcript.agent_identity_key_jkt,
            transcript.agent_noise_key,
            transcript.brain_noise_key,
            transcript.agent_nonce,
            transcript.brain_nonce,
            transcript.grant_digest,
        ),
    )


def pairing_digest(transcript: PairingTranscript) -> bytes:
    return hashlib.sha256(pairing_transcript(transcript)).digest()


def proof_message(role: Literal["brain", "agent"], digest: bytes) -> bytes:
    _require_bytes32(digest)
    if role not in ("brain", "agent"):
        raise CompanionPairingProofError()
    return _frame(_PROOF_DOMAIN, (role, digest))


def comparison_code(digest: bytes) -> str:
    _require_bytes32(digest)
    hashed = hashlib.sha256(_frame(_CODE_DOMAIN, (digest,))).digest()
    return f"{int.from_bytes(hashed[:4], 'big') % 1_000_000:06d}"


def session_prologue(digest: bytes) -> bytes:
    _require_bytes32(digest)
    return SESSION_PROFILE + b"\x00" + digest


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """Validate a closed public P-256 JWK and derive its RFC 7638 thumbprint."""
    _public_key(jwk)
    canonical = json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _encode(hashlib.sha256(canonical).digest())


def sign_brain_pairing_proof(
    authority: InstallationKeyAuthority,
    installation_key: InstallationKeyReference,
    transcript: PairingTranscript,
) -> str:
    """Sign only the Brain pairing purpose through the existing key authority."""
    jwk = _installation_jwk(installation_key)
    if (
        transcript.installation_key_id != installation_key.installation_key_id
        or transcript.installation_key_jkt != installation_key.installation_key_jkt
    ):
        raise CompanionPairingProofError("pairing_key_refused")
    digest = pairing_digest(transcript)
    try:
        proof = authority.sign_challenge(proof_message("brain", digest))
    except InstallationKeyError:
        raise CompanionPairingProofError() from None
    if (
        proof.installation_key_id != installation_key.installation_key_id
        or proof.algorithm != INSTALLATION_PROOF_ALGORITHM
    ):
        raise CompanionPairingProofError()
    encoded = _encode(proof.signature)
    if not verify_pairing_proof(jwk, "brain", digest, encoded):
        raise CompanionPairingProofError()
    return encoded


def verify_pairing_proof(
    public_jwk: dict[str, str],
    role: Literal["brain", "agent"],
    digest: bytes,
    signature: str,
) -> bool:
    """Verify a canonical unpadded, low-S ES256 P1363 pairing signature."""
    try:
        key = _public_key(public_jwk)
        raw = _decode(signature, 64)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not 0 < r < _P256_ORDER or not 0 < s <= _P256_ORDER // 2:
            return False
        key.verify(
            encode_dss_signature(r, s),
            proof_message(role, digest),
            ec.ECDSA(hashes.SHA256()),
        )
        return True
    except (CompanionPairingProofError, InvalidSignature):
        return False


def _installation_jwk(reference: InstallationKeyReference) -> dict[str, str]:
    try:
        jwk = json.loads(reference.public_key_jwk)
    except (TypeError, ValueError):
        raise CompanionPairingProofError("pairing_key_refused") from None
    if (
        not isinstance(jwk, dict)
        or set(jwk) != {"crv", "kty", "kid", "x", "y"}
        or jwk.pop("kid") != reference.installation_key_id
        or jwk_thumbprint(jwk) != reference.installation_key_jkt
    ):
        raise CompanionPairingProofError("pairing_key_refused")
    return jwk


def _public_key(jwk: dict[str, str]) -> ec.EllipticCurvePublicKey:
    if (
        not isinstance(jwk, dict)
        or set(jwk) != {"crv", "kty", "x", "y"}
        or jwk["crv"] != "P-256"
        or jwk["kty"] != "EC"
    ):
        raise CompanionPairingProofError("pairing_key_refused")
    x, y = _decode(jwk["x"], 32), _decode(jwk["y"], 32)
    try:
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
        ).public_key()
    except ValueError:
        raise CompanionPairingProofError("pairing_key_refused") from None


def _require_bytes32(value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise CompanionPairingProofError("pairing_message_invalid")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str, size: int) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) != (size * 8 + 5) // 6
        or not _BASE64URL.fullmatch(value)
    ):
        raise CompanionPairingProofError("pairing_message_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error):
        raise CompanionPairingProofError("pairing_message_invalid") from None
    if len(raw) != size or _encode(raw) != value:
        raise CompanionPairingProofError("pairing_message_invalid")
    return raw


def _frame(domain: bytes, fields: tuple[bytes | str, ...]) -> bytes:
    result = bytearray(domain)
    for field in fields:
        encoded = field.encode("utf-8") if isinstance(field, str) else field
        result.extend(struct.pack("!I", len(encoded)))
        result.extend(encoded)
    return bytes(result)
