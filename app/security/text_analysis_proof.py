"""Offline native request framing and installation-key proofs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
import struct
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from app.security.signed_objects import decode, encode, public_key
from app.security.text_analysis_contract import (
    PREFIX, TextAnalysisSchemas, canonical, parse_json, supported_profiles,
)

_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")


class TextAnalysisProofError(ValueError):
    """A native request, challenge or installation signature was rejected."""


@dataclass(frozen=True)
class _Operation:
    family: str
    action: str
    authority: str | None
    request_limit: int = 8192
    challenge_limit: int = 12288

    @property
    def profile(self):
        return PREFIX + self.family + "-proof:v1"

    @property
    def purpose(self):
        family = {"license-issuance": "license", "preview-issuance": "preview",
                  "authority-delivery": "authority"}.get(self.family, self.family)
        return "text-analysis-" + family + "-" + self.action

    @property
    def path(self):
        plural = "authority-deliveries" if self.family == "authority-delivery" else self.family + "s"
        return "/v1/text-analysis-" + plural + ":" + self.action

    @property
    def audience(self):
        return "urn:bridge-clean:commercial-control-plane:text-analysis-" + self.family

    @property
    def domain(self):
        return ("BRIDGE-CLEAN-TEXT-ANALYSIS-" + self.family.upper() + "-PROOF-V1\0").encode("ascii")

    @property
    def schema_root(self):
        family = self.family if self.family == "preview-acquisition" else self.family + "-proof"
        return "schemas/text-analysis-" + family + "/v1/"

    @property
    def challenge_schema(self):
        prefix = "proof-" if self.family == "preview-acquisition" else ""
        return self.schema_root + prefix + "challenge-response.schema.json"


_OPERATIONS = {
    "license-issue": _Operation("license-issuance", "issue", "license"),
    "preview-issue": _Operation("preview-issuance", "issue", "preview"),
    "authority-deliver": _Operation("authority-delivery", "deliver", None, 4096, 8192),
    "authority-recover": _Operation("authority-delivery", "recover", None, 4096, 8192),
    "preview-prepare": _Operation("preview-acquisition", "prepare", "preview"),
    "preview-read": _Operation("preview-acquisition", "read", "preview"),
    "preview-submit": _Operation("preview-acquisition", "submit", "preview"),
}


@dataclass(frozen=True)
class ProofContext:
    """Independently retained request and original installation-key bindings."""
    operation: str
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    public_key_jwk: bytes = field(repr=False)
    expected_request: bytes = field(repr=False)


@dataclass(frozen=True)
class PreparedProofRequest:
    operation: str
    canonical_request: bytes = field(repr=False)
    request_digest: bytes = field(repr=False)
    challenge_request: bytes = field(repr=False)


def _need(ok, reason):
    if not ok:
        raise TextAnalysisProofError(reason)


def _operation(name):
    _need(type(name) is str and name in _OPERATIONS, "unsupported_operation")
    return _OPERATIONS[name]


def _json(raw, limit):
    try:
        result = parse_json(raw, maximum=limit)
        _need(type(result) is dict, "invalid_object")
        return result
    except ValueError:
        raise TextAnalysisProofError("invalid_object") from None


def _key(context):
    jwk = _json(context.public_key_jwk, 1024)
    _need(set(jwk) == {"kty", "crv", "x", "y", "kid"}, "invalid_key")
    material = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    try:
        key = public_key(material)
        thumbprint = hashlib.sha256(canonical(material)).digest()
        _need(jwk["kid"] == context.installation_key_id == "ik1." + encode(thumbprint[:16])
              and context.installation_key_jkt == encode(thumbprint), "key_binding_mismatch")
        return key, canonical(jwk)
    except (ValueError, TypeError):
        raise TextAnalysisProofError("invalid_key") from None


def _validate_request(value, context, operation, schemas):
    prefix = "proof-" if operation.family == "preview-acquisition" else ""
    envelope = {"profile": operation.profile, "purpose": operation.purpose, "request": value}
    schemas.require(operation.schema_root + prefix + "challenge-request.schema.json", envelope)
    binding = {name: getattr(context, name) for name in
               ("organization_id", "installation_id", "installation_key_id", "installation_key_jkt")}
    _need(value["binding"] == binding, "request_binding_mismatch")
    if "supported_profiles" in value:
        profiles = value["supported_profiles"]["profiles"]
        supported_profiles(canonical(value["supported_profiles"]), schemas=schemas,
                           authority=operation.authority, expected_profiles=tuple(profiles))
    return envelope


def prepare_request(raw: bytes, *, context: ProofContext, schemas: TextAnalysisSchemas) -> PreparedProofRequest:
    _need(type(context) is ProofContext, "invalid_context")
    operation = _operation(context.operation)
    _need(all(type(value) is str and _ID.fullmatch(value) for value in
              (context.organization_id, context.installation_id)), "invalid_context")
    _key(context)
    expected = _json(context.expected_request, operation.request_limit)
    _validate_request(expected, context, operation, schemas)
    value = _json(raw, operation.request_limit)
    envelope = _validate_request(value, context, operation, schemas)
    encoded = canonical(value)
    _need(encoded == canonical(expected), "request_context_mismatch")
    challenge = canonical(envelope)
    _need(len(challenge) <= operation.challenge_limit, "challenge_request_size")
    return PreparedProofRequest(context.operation, encoded, hashlib.sha256(encoded).digest(), challenge)


def proof_bytes(prepared: PreparedProofRequest, *, challenge_id: str, challenge: str) -> bytes:
    """Encode a proof message; performs no hosted state or replay checks."""
    _need(type(prepared) is PreparedProofRequest, "invalid_request")
    operation = _operation(prepared.operation)
    _need(type(challenge_id) is str and _UUID.fullmatch(challenge_id), "invalid_challenge")
    try:
        raw_challenge = decode(challenge, 32)
        _need(len(raw_challenge) == 32, "invalid_challenge")
        request = _json(prepared.canonical_request, operation.request_limit)
        _need(prepared.request_digest == hashlib.sha256(prepared.canonical_request).digest(), "invalid_digest")
        binding = request["binding"]
        fields = (challenge_id.encode(), raw_challenge, b"POST", operation.path.encode(),
                  prepared.request_digest, operation.purpose.encode(), binding["installation_id"].encode(),
                  binding["organization_id"].encode(), binding["installation_key_id"].encode(), operation.audience.encode())
        return operation.domain + b"".join(struct.pack(">I", len(value)) + value for value in fields)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise TextAnalysisProofError("invalid_proof_input") from None


def _now(value):
    _need(type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0), "invalid_clock")
    return value


def _timestamp(value):
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        _need(result.strftime("%Y-%m-%dT%H:%M:%S.") + f"{result.microsecond // 1000:03d}Z" == value, "invalid_timestamp")
        return result
    except (ValueError, TypeError):
        raise TextAnalysisProofError("invalid_timestamp") from None


def validate_challenge(raw: bytes, *, prepared: PreparedProofRequest, schemas: TextAnalysisSchemas,
                       now: datetime) -> dict:
    operation = _operation(prepared.operation)
    value = _json(raw, 4096)
    schemas.require(operation.challenge_schema, value)
    _need(value["purpose"] == operation.purpose and value["audience"] == operation.audience
          and value["request_digest"] == prepared.request_digest.hex(), "challenge_binding_mismatch")
    try:
        _need(len(decode(value["challenge"], 32)) == 32, "invalid_challenge")
    except ValueError:
        raise TextAnalysisProofError("invalid_challenge") from None
    issued, expires = _timestamp(value["issued_at"]), _timestamp(value["expires_at"])
    _need(expires - issued == timedelta(seconds=60), "challenge_lifetime")
    _need(issued <= _now(now) < expires, "challenge_expired")
    return value


def verify_proof(signature: str, *, prepared: PreparedProofRequest, challenge_id: str,
                 challenge: str, context: ProofContext, schemas: TextAnalysisSchemas) -> bool:
    """Verify framing and cryptography without asserting hosted registration."""
    try:
        _need(type(prepared) is PreparedProofRequest, "invalid_request")
        admitted = prepare_request(prepared.canonical_request, context=context, schemas=schemas)
        _need(admitted == prepared, "request_context_mismatch")
        raw = decode(signature, 64)
        _need(len(raw) == 64, "invalid_signature")
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        _need(0 < r < _ORDER and 0 < s <= _ORDER // 2, "invalid_signature")
        key, _ = _key(context)
        key.verify(utils.encode_dss_signature(r, s), proof_bytes(prepared, challenge_id=challenge_id, challenge=challenge),
                   ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature, TypeError):
        return False


def sign_request(raw: bytes, challenge_raw: bytes, *, context: ProofContext, key_authority,
                 schemas: TextAnalysisSchemas, clock=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic) -> bytes:
    """Sign using the existing active key; no key creation or transport occurs."""
    started = monotonic()
    _need(type(started) in (int, float) and math.isfinite(started), "invalid_clock")
    prepared = prepare_request(raw, context=context, schemas=schemas)
    accepted_at = _now(clock())
    challenge = validate_challenge(challenge_raw, prepared=prepared, schemas=schemas, now=accepted_at)
    budget = (_timestamp(challenge["expires_at"]) - accepted_at).total_seconds()
    _, original_public = _key(context)
    def check():
        elapsed = monotonic() - started
        _need(math.isfinite(elapsed) and 0 <= elapsed < budget, "proof_deadline")
        now = _now(clock())
        _need(_timestamp(challenge["issued_at"]) <= now < _timestamp(challenge["expires_at"]), "challenge_expired")
    def reopen():
        reference = key_authority.reopen_existing()
        _need(reference.installation_key_id == context.installation_key_id
              and reference.installation_key_jkt == context.installation_key_jkt
              and canonical(_json(reference.public_key_jwk.encode("utf-8"), 1024)) == original_public,
              "key_changed")
        check()
    check()
    reopen()
    message = proof_bytes(prepared, challenge_id=challenge["challenge_id"], challenge=challenge["challenge"])
    proof = key_authority.sign_challenge(message)
    check()
    _need(proof.installation_key_id == context.installation_key_id and proof.algorithm == "ES256"
          and type(proof.signature) is bytes, "key_changed")
    signature = encode(proof.signature)
    _need(verify_proof(signature, prepared=prepared, challenge_id=challenge["challenge_id"],
                       challenge=challenge["challenge"], context=context, schemas=schemas), "invalid_signature")
    reopen()
    result = canonical({"request": _json(prepared.canonical_request, _operation(context.operation).request_limit),
                        "proof": {"challenge_id": challenge["challenge_id"], "challenge": challenge["challenge"],
                                  "signature": signature}})
    _need(len(result) <= _operation(context.operation).request_limit, "request_size")
    check()
    return result
