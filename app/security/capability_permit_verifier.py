"""Offline verification for capability-permit-v1 compact JWS permits."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from contracts.loader import load_trust_set


_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_CROSS_PLANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_INSTALLATION_KEY_ID_RE = re.compile(r"^ik1\.[A-Za-z0-9_-]{22}$")
_INSTALLATION_KEY_JKT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SUBJECT_RE = re.compile(
    r"^organization:[A-Za-z0-9._~-]+:installation:[A-Za-z0-9._~-]+:"
    r"capability:[a-z][a-z0-9-]{0,63}$"
)
_CATALOG_VERSION_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_EXPECTED_TYP = "urn:bridge-clean:capability-permit:v1"
_EXPECTED_AUDIENCE = "urn:bridge-clean:local-brain:capability-permit"
_EXPECTED_PROFILE = "urn:bridge-clean:capability-permit:v1"
_EXPECTED_PERMIT_TYPE = "capability_permit"
_EXPECTED_ISSUER = "urn:bridge-clean:commercial-control-plane"
_PAYLOAD_FIELDS = frozenset(
    {
        "profile",
        "permit_type",
        "iss",
        "aud",
        "sub",
        "permit_id",
        "issuance_id",
        "iat",
        "organization_id",
        "installation_id",
        "installation_key_id",
        "installation_key_jkt",
        "capability",
        "catalog_version",
        "credit_cost",
        "execution_limit",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityPermitVerificationContext:
    """Locally maintained bindings for one capability exercise."""

    expected_subject: str
    expected_organization_id: str
    expected_installation_id: str
    expected_capability: str
    expected_installation_key_id: str
    expected_installation_key_jkt: str


@dataclass(frozen=True, slots=True)
class CapabilityPermitVerification:
    """A named capability-permit verification outcome."""

    valid: bool
    result: str


def load_pinned_trust_set(
    path: str, *, environment: str = "production"
) -> dict[str, Any]:
    """Load the manifest-pinned permit trust set."""

    return load_trust_set(path, environment=environment)


def verify_capability_permit(
    token: str,
    *,
    context: CapabilityPermitVerificationContext,
    trust_set: Mapping[str, Any],
) -> CapabilityPermitVerification:
    """Verify one permit against local trust material and local bindings."""

    if not isinstance(token, str) or token.count(".") != 2:
        return _outcome(False, "invalid_compact_jws")
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header_bytes = _b64u_decode(header_segment)
        payload_bytes = _b64u_decode(payload_segment)
        signature = _b64u_decode(signature_segment)
        header = json.loads(header_bytes)
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return _outcome(False, "invalid_compact_jws")

    try:
        if _canonical_json(header) != header_bytes or _canonical_json(payload) != payload_bytes:
            return _outcome(False, "noncanonical_json")
    except ValueError:
        return _outcome(False, "noncanonical_json")
    if (
        not isinstance(header, dict)
        or set(header) != {"alg", "kid", "typ"}
        or header.get("alg") != "ES256"
    ):
        return _outcome(False, "invalid_header")
    if header.get("typ") != _EXPECTED_TYP:
        return _outcome(False, "typ_mismatch")
    if not isinstance(payload, dict) or payload.get("aud") != _EXPECTED_AUDIENCE:
        return _outcome(False, "audience_mismatch")

    keys = _trusted_keys(trust_set)
    kid = header.get("kid")
    entry = keys.get(kid) if isinstance(kid, str) else None
    if entry is None:
        return _outcome(False, "unknown_kid")
    purpose, public_key = entry
    if purpose != "capability-permit":
        return _outcome(False, "wrong_key_purpose")
    if len(signature) != 64 or int.from_bytes(signature[32:], "big") > _P256_ORDER // 2:
        return _outcome(False, "invalid_signature")
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    if not _verify_signature(public_key, signing_input, signature):
        return _outcome(False, "invalid_signature")

    if not _valid_payload(payload):
        return _outcome(False, "schema_invalid")
    if payload["sub"] != context.expected_subject:
        return _outcome(False, "subject_mismatch")
    if payload["organization_id"] != context.expected_organization_id:
        return _outcome(False, "organization_mismatch")
    if payload["installation_id"] != context.expected_installation_id:
        return _outcome(False, "installation_mismatch")
    if payload["capability"] != context.expected_capability:
        return _outcome(False, "capability_mismatch")
    if (
        payload["installation_key_id"] != context.expected_installation_key_id
        or payload["installation_key_jkt"] != context.expected_installation_key_jkt
    ):
        return _outcome(False, "installation_key_mismatch")
    return _outcome(True, "accepted")


def _outcome(valid: bool, result: str) -> CapabilityPermitVerification:
    return CapabilityPermitVerification(valid=valid, result=result)


def _b64u_decode(value: object) -> bytes:
    if not isinstance(value, str) or not _B64U_RE.fullmatch(value) or "=" in value:
        raise ValueError("noncanonical_base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("noncanonical_base64url") from exc
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError("noncanonical_base64url")
    return decoded


def _canonical_json(value: Any) -> bytes:
    def check(item: Any) -> None:
        if item is None or isinstance(item, float):
            raise ValueError("unsupported_json_value")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("non_string_key")
            for key, child in item.items():
                check(key)
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif not isinstance(item, (str, int, bool)):
            raise ValueError("unsupported_json_value")

    check(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _trusted_keys(
    trust_set: Mapping[str, Any],
) -> dict[str, tuple[str, ec.EllipticCurvePublicKey]]:
    entries = trust_set.get("keys")
    if not isinstance(entries, list):
        raise ValueError("invalid permit trust set")
    keys: dict[str, tuple[str, ec.EllipticCurvePublicKey]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid permit trust set")
        purpose = entry.get("purpose")
        jwk = entry.get("jwk")
        if not isinstance(purpose, str) or not isinstance(jwk, Mapping):
            raise ValueError("invalid permit trust set")
        kid = jwk.get("kid")
        if not isinstance(kid, str) or kid in keys:
            raise ValueError("invalid permit trust set")
        keys[kid] = (purpose, _public_key(jwk))
    return keys


def _public_key(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    if set(jwk) != {"crv", "kid", "kty", "x", "y"}:
        raise ValueError("invalid permit trust set")
    if jwk.get("crv") != "P-256" or jwk.get("kty") != "EC":
        raise ValueError("invalid permit trust set")
    try:
        x_bytes = _b64u_decode(jwk["x"])
        y_bytes = _b64u_decode(jwk["y"])
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid permit trust set") from exc
    if len(x_bytes) != 32 or len(y_bytes) != 32:
        raise ValueError("invalid permit trust set")
    try:
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x_bytes, "big"), int.from_bytes(y_bytes, "big"), ec.SECP256R1()
        ).public_key()
    except ValueError as exc:
        raise ValueError("invalid permit trust set") from exc


def _verify_signature(
    public_key: ec.EllipticCurvePublicKey, signing_input: bytes, signature: bytes
) -> bool:
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 1 <= r < _P256_ORDER or not 1 <= s < _P256_ORDER:
        return False
    try:
        public_key.verify(
            encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def _valid_payload(payload: Mapping[str, Any]) -> bool:
    return (
        set(payload) == _PAYLOAD_FIELDS
        and payload["profile"] == _EXPECTED_PROFILE
        and payload["permit_type"] == _EXPECTED_PERMIT_TYPE
        and payload["iss"] == _EXPECTED_ISSUER
        and payload["aud"] == _EXPECTED_AUDIENCE
        and _matches(payload["sub"], _SUBJECT_RE, 1, 384)
        and _matches(payload["permit_id"], _UUIDV7_RE, 36, 36)
        and _matches(payload["issuance_id"], _UUIDV7_RE, 36, 36)
        and _integer_in_range(payload["iat"], 0, 4_102_444_800)
        and _matches(payload["organization_id"], _CROSS_PLANE_ID_RE, 1, 128)
        and _matches(payload["installation_id"], _CROSS_PLANE_ID_RE, 1, 128)
        and _matches(payload["installation_key_id"], _INSTALLATION_KEY_ID_RE, 26, 26)
        and _matches(payload["installation_key_jkt"], _INSTALLATION_KEY_JKT_RE, 43, 43)
        and payload["capability"] == "analysis-run"
        and _matches(payload["catalog_version"], _CATALOG_VERSION_RE, 1, 128)
        and _integer_in_range(payload["credit_cost"], 1, 1_000_000)
        and payload["execution_limit"] == 1
    )


def _matches(value: object, pattern: re.Pattern[str], minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and pattern.fullmatch(value) is not None
    )


def _integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum
