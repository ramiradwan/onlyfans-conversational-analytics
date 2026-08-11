"""Offline verification for grant-profile-v1 compact JWS grants."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
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
_UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_ISSUER = "urn:bridge-clean:commercial-control-plane"
_PROFILE = "urn:bridge-clean:grant-profile:v1"
_PURPOSE_CODES = {"installation-binding": "ib", "membership": "ms", "license": "le"}
_PROFILES = {
    "installation_grant": {
        "typ": "urn:bridge-clean:grant:installation:v1",
        "purpose": "installation-binding",
        "lifetime": 2_592_000,
        "grace": 604_800,
    },
    "creator_account_binding": {
        "typ": "urn:bridge-clean:grant:creator-binding:v1",
        "purpose": "installation-binding",
        "lifetime": 604_800,
        "grace": 259_200,
    },
    "membership_snapshot": {
        "typ": "urn:bridge-clean:grant:membership:v1",
        "purpose": "membership",
        "lifetime": 86_400,
        "grace": 259_200,
    },
    "license_entitlement": {
        "typ": "urn:bridge-clean:grant:license:v1",
        "purpose": "license",
        "lifetime": 86_400,
        "grace": 604_800,
    },
}
_ROLE_SCOPES = {
    "administrator": {
        "creator-binding:approve", "creator-binding:revoke", "installation-claim:issue",
        "installation:provision", "membership:manage", "provisioning:milestone",
        "runtime:existing-data", "runtime:licensed-processing",
    },
    "creator_operator": {"runtime:existing-data", "runtime:licensed-processing"},
    "delegated_installer": {"installation-claim:issue", "installation:provision", "provisioning:milestone"},
    "owner": {
        "commercial:manage", "creator-binding:approve", "creator-binding:revoke",
        "installation-claim:issue", "installation:provision", "membership:manage",
        "organization:manage", "provisioning:milestone", "runtime:existing-data",
        "runtime:licensed-processing",
    },
}
_COMMON_CLAIMS = {
    "aud", "exp", "grant_type", "iat", "installation_id", "installation_key_id",
    "installation_key_jkt", "iss", "jti", "nbf", "organization_id", "profile", "sub",
}
_EXTRA_CLAIMS = {
    "installation_grant": set(),
    "creator_account_binding": {"approval_id", "approval_revision", "creator_account_id"},
    "membership_snapshot": {
        "allowed_creator_account_ids", "ciam_issuer", "ciam_subject", "membership_id", "roles", "scopes",
    },
    "license_entitlement": {"entitlement_id", "features", "product_id"},
}


@dataclass(frozen=True, slots=True)
class GrantVerificationContext:
    """Local values required to verify one grant."""

    expected_grant_type: str
    expected_audience: str
    expected_organization_id: str
    expected_installation_id: str
    expected_installation_key_id: str
    expected_installation_key_jkt: str
    expected_subject: str
    verifier_time: int
    tombstones: Sequence[Mapping[str, Any]] = ()
    requested_operation: str = ""
    requested_creator_account_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class GrantVerification:
    """A named grant-verification outcome."""

    valid: bool
    result: str
    time_state: str


def load_pinned_trust_set(
    path: str, *, environment: str = "production"
) -> dict[str, Any]:
    """Load the manifest-pinned trust set for grant verification."""

    return load_trust_set(path, environment=environment)


def trusted_signing_keys(trust_set: Mapping[str, Any]) -> dict[str, str]:
    """Return the purpose of every usable key in a trust set, keyed by identifier.

    Raises ValueError when the trust set does not provide usable signing keys.
    """

    return {kid: purpose for kid, (purpose, _) in _trusted_keys(trust_set).items()}


def _outcome(valid: bool, result: str, time_state: str = "invalid") -> GrantVerification:
    return GrantVerification(valid=valid, result=result, time_state=time_state)


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


def _strict_json(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_member")
            result[key] = value
        return result

    try:
        decoded = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(decoded, dict):
        raise ValueError("schema_invalid")
    return decoded


def _jwk_thumbprint(jwk: Mapping[str, Any]) -> bytes:
    bare = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    return hashlib.sha256(_canonical_json(bare)).digest()


def _public_key(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    if set(jwk) != {"crv", "kid", "kty", "x", "y"}:
        raise ValueError("invalid_trust_set")
    if jwk.get("crv") != "P-256" or jwk.get("kty") != "EC":
        raise ValueError("invalid_trust_set")
    x = int.from_bytes(_b64u_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64u_decode(jwk["y"]), "big")
    try:
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise ValueError("invalid_trust_set") from exc


def _trusted_keys(trust_set: Mapping[str, Any]) -> dict[str, tuple[str, ec.EllipticCurvePublicKey]]:
    entries = trust_set.get("keys")
    if not isinstance(entries, list) or not entries:
        raise ValueError("invalid_trust_set")
    keys: dict[str, tuple[str, ec.EllipticCurvePublicKey]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid_trust_set")
        purpose = entry.get("purpose")
        jwk = entry.get("jwk")
        thumbprint = entry.get("thumbprint")
        if purpose not in _PURPOSE_CODES or not isinstance(jwk, Mapping) or not isinstance(thumbprint, str):
            raise ValueError("invalid_trust_set")
        public_key = _public_key(jwk)
        thumbprint_bytes = _jwk_thumbprint(jwk)
        computed = base64.urlsafe_b64encode(thumbprint_bytes).rstrip(b"=").decode("ascii")
        kid = jwk.get("kid")
        expected_kid = (
            f"bc1.{_PURPOSE_CODES[purpose]}."
            f"{base64.urlsafe_b64encode(thumbprint_bytes[:16]).rstrip(b'=').decode('ascii')}"
        )
        if kid != expected_kid or thumbprint != computed or kid in keys:
            raise ValueError("invalid_trust_set")
        keys[kid] = (purpose, public_key)
    return keys


def _set_error(values: Any) -> str | None:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        return "schema_invalid"
    if len(values) != len(set(values)):
        return "duplicate_set_value"
    if values != sorted(values, key=lambda item: item.encode("utf-8")):
        return "unsorted_set"
    return None


def verify_grant(
    token: str,
    *,
    context: GrantVerificationContext,
    trust_set: Mapping[str, Any],
) -> GrantVerification:
    """Verify one compact JWS grant against pinned local trust and bindings."""

    profile = _PROFILES.get(context.expected_grant_type)
    if profile is None:
        return _outcome(False, "unsupported_grant_type")
    if not isinstance(token, str) or len(token) > 16_384 or token.count(".") != 2:
        return _outcome(False, "invalid_compact_jws")
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        if len(header_segment) > 512:
            raise ValueError("header_too_large")
        header_bytes = _b64u_decode(header_segment)
        payload_bytes = _b64u_decode(payload_segment)
        signature = _b64u_decode(signature_segment)
        if len(payload_bytes) > 12_288:
            raise ValueError("payload_too_large")
        header = _strict_json(header_bytes)
        payload = _strict_json(payload_bytes)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return _outcome(False, str(exc))
    try:
        if _canonical_json(header) != header_bytes:
            return _outcome(False, "noncanonical_header")
        if _canonical_json(payload) != payload_bytes:
            return _outcome(False, "noncanonical_payload")
    except ValueError as exc:
        return _outcome(False, str(exc))
    if set(header) != {"alg", "kid", "typ"} or header.get("alg") != "ES256":
        return _outcome(False, "invalid_header")
    if payload.get("grant_type") != context.expected_grant_type:
        return _outcome(False, "grant_type_mismatch")
    if header.get("typ") != profile["typ"]:
        return _outcome(False, "typ_mismatch")
    if payload.get("aud") != context.expected_audience:
        return _outcome(False, "audience_mismatch")
    try:
        keys = _trusted_keys(trust_set)
    except ValueError as exc:
        return _outcome(False, str(exc))
    entry = keys.get(header.get("kid"))
    if entry is None:
        return _outcome(False, "unknown_kid")
    purpose, public_key = entry
    if purpose != profile["purpose"]:
        return _outcome(False, "wrong_key_purpose")
    if len(signature) == 64 and int.from_bytes(signature[32:], "big") > _P256_ORDER // 2:
        return _outcome(False, "high_s_signature")
    if not _verify_signature(public_key, f"{header_segment}.{payload_segment}".encode("ascii"), signature):
        return _outcome(False, "invalid_signature")
    if set(payload) != _COMMON_CLAIMS | _EXTRA_CLAIMS[context.expected_grant_type]:
        return _outcome(False, "schema_invalid")
    if payload["profile"] != _PROFILE or payload["iss"] != _ISSUER:
        return _outcome(False, "issuer_or_profile_mismatch")
    if not _UUIDV7_RE.fullmatch(str(payload["jti"])):
        return _outcome(False, "invalid_jti")
    if not all(isinstance(payload[name], int) and not isinstance(payload[name], bool) for name in ("iat", "nbf", "exp")):
        return _outcome(False, "invalid_numeric_date")
    if payload["iat"] != payload["nbf"] or payload["exp"] != payload["iat"] + profile["lifetime"]:
        return _outcome(False, "invalid_time_contract")
    for field in ("organization_id", "installation_id", "installation_key_id"):
        if not isinstance(payload[field], str) or not _ID_RE.fullmatch(payload[field]):
            return _outcome(False, "schema_invalid")
    if payload["organization_id"] != context.expected_organization_id:
        return _outcome(False, "organization_mismatch")
    if payload["installation_id"] != context.expected_installation_id:
        return _outcome(False, "installation_mismatch")
    if (
        payload["installation_key_id"] != context.expected_installation_key_id
        or payload["installation_key_jkt"] != context.expected_installation_key_jkt
    ):
        return _outcome(False, "installation_key_mismatch")
    if payload["sub"] != context.expected_subject:
        return _outcome(False, "subject_mismatch")
    type_outcome = _verify_type_specific(payload, context)
    if type_outcome is not None:
        return type_outcome
    if context.verifier_time + 60 < payload["nbf"]:
        return _outcome(False, "not_yet_valid")
    for tombstone in context.tombstones:
        if (
            tombstone.get("scope_type") == "jti"
            and tombstone.get("grant_jti") == payload["jti"]
            and tombstone.get("effective_at", context.verifier_time + 1) <= context.verifier_time
        ):
            return _outcome(False, "revoked")
    if context.verifier_time < payload["exp"]:
        return _outcome(True, "accepted", "current")
    if context.verifier_time < payload["exp"] + profile["grace"]:
        return _outcome(True, "accepted", "grace")
    return _outcome(False, "expired")


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
    except InvalidSignature:
        return False
    return True


def _verify_type_specific(
    payload: Mapping[str, Any], context: GrantVerificationContext
) -> GrantVerification | None:
    if context.expected_grant_type == "creator_account_binding":
        if not isinstance(payload["approval_revision"], int) or payload["approval_revision"] <= 0:
            return _outcome(False, "schema_invalid")
    elif context.expected_grant_type == "membership_snapshot":
        for field in ("roles", "scopes", "allowed_creator_account_ids"):
            error = _set_error(payload[field])
            if error:
                return _outcome(False, error)
        if len(payload["allowed_creator_account_ids"]) > 64:
            return _outcome(False, "account_cap_exceeded")
        if any(role not in _ROLE_SCOPES for role in payload["roles"]):
            return _outcome(False, "unknown_role")
        exact_scopes = sorted(set().union(*(_ROLE_SCOPES[role] for role in payload["roles"])))
        if payload["scopes"] != exact_scopes:
            return _outcome(False, "role_scope_mismatch")
        if context.requested_operation.startswith("runtime:") and payload["roles"] == ["delegated_installer"]:
            return _outcome(False, "runtime_role_denied")
        if any(account not in payload["allowed_creator_account_ids"] for account in context.requested_creator_account_ids):
            return _outcome(False, "creator_account_not_allowed")
    elif context.expected_grant_type == "license_entitlement":
        error = _set_error(payload["features"])
        if error:
            return _outcome(False, error)
        allowed = {"agent-capture", "analytics-processing", "command-execution", "ingestion"}
        if not payload["features"] or not set(payload["features"]) <= allowed or payload["product_id"] != "bridge-clean":
            return _outcome(False, "schema_invalid")
    return None
