"""Independent offline verification for text-analysis license and preview tokens."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from app.security.signed_objects import decode, encode, public_key, SignedObjectRejected
from app.security.text_analysis_contract import canonical, parse_json, PREFIX, OUTPUT_POLICY, TextAnalysisSchemas
from contracts.loader import load_trust_set

ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_KEY_ID = re.compile(r"ik1\.[A-Za-z0-9_-]{22}\Z")
_JKT = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SUBJECT = re.compile(r"organization:[A-Za-z0-9._~-]+:installation:[A-Za-z0-9._~-]+:seat:[A-Za-z0-9._~-]+:capability:[a-z][a-z0-9-]{0,63}\Z")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_CONTEXT = frozenset({"expected_subject", "expected_organization_id", "expected_installation_id",
    "expected_installation_key_id", "expected_installation_key_jkt", "expected_seat_scope",
    "expected_capability", "expected_target_major", "expected_artifact_family",
    "requested_update_mode", "expected_fallback_major"})


class TextAnalysisConfigurationError(ValueError):
    """Independent local context or dedicated public trust is invalid."""


@dataclass(frozen=True)
class TextAnalysisKey:
    purpose: str
    public_key: ec.EllipticCurvePublicKey


@dataclass(frozen=True)
class TextAnalysisTrust:
    keys: Mapping[str, TextAnalysisKey] = field(repr=False)
    fixture_only: bool

    def __post_init__(self):
        if type(self.fixture_only) is not bool or not isinstance(self.keys, Mapping) or not 1 <= len(self.keys) <= 8:
            raise TextAnalysisConfigurationError("invalid_trust")
        admitted = {}
        for kid, key in self.keys.items():
            if type(key) is not TextAnalysisKey or not isinstance(key.public_key, ec.EllipticCurvePublicKey):
                raise TextAnalysisConfigurationError("invalid_trust")
            code = {"text-analysis-license": "tl", "text-analysis-preview": "tp"}.get(key.purpose)
            if code is None or not isinstance(key.public_key.curve, ec.SECP256R1):
                raise TextAnalysisConfigurationError("invalid_trust")
            point = key.public_key.public_numbers()
            jwk = {"kty": "EC", "crv": "P-256", "x": encode(point.x.to_bytes(32, "big")),
                   "y": encode(point.y.to_bytes(32, "big"))}
            expected = "bc1." + code + "." + encode(hashlib.sha256(canonical(jwk)).digest()[:16])
            if kid != expected:
                raise TextAnalysisConfigurationError("invalid_trust")
            admitted[kid] = key
        object.__setattr__(self, "keys", MappingProxyType(admitted))


def fixture_trust(*, environment: str) -> TextAnalysisTrust:
    """Load only the separately pinned, non-production conformance keys."""
    if environment not in {"development", "dev", "local", "test"}:
        raise TextAnalysisConfigurationError("fixture_trust_unavailable")
    value = load_trust_set("text-analysis-authority-v1/trust-set.json", environment="development")
    return _fixture_keys(value)


def _fixture_keys(value: dict) -> TextAnalysisTrust:
    try:
        if (type(value) is not dict or set(value) != {"environment", "keys", "production_usable", "profile"}
                or value["production_usable"] is not False
                or value["environment"] != "TEST-VECTORS-ONLY-NOT-A-PRODUCTION-TRUST-SET"
                or value["profile"] != "urn:bridge-clean:text-analysis-authority-test-trust:v1"
                or type(value["keys"]) is not list or len(value["keys"]) != 2):
            raise ValueError()
        keys, purposes = {}, set()
        for item in value["keys"]:
            if type(item) is not dict or set(item) != {"fixture_only", "jwk", "purpose"} or item["fixture_only"] is not True:
                raise ValueError()
            purpose = item["purpose"]
            code = {"text-analysis-license": "tl", "text-analysis-preview": "tp"}.get(purpose)
            jwk = item["jwk"]
            if code is None or purpose in purposes or type(jwk) is not dict or set(jwk) != {"kty", "crv", "x", "y", "kid"}:
                raise ValueError()
            material = {key: jwk[key] for key in ("kty", "crv", "x", "y")}
            thumbprint = hashlib.sha256(canonical(material)).digest()
            kid = "bc1." + code + "." + encode(thumbprint[:16])
            if jwk["kid"] != kid or kid in keys:
                raise ValueError()
            keys[kid] = TextAnalysisKey(purpose, public_key(material))
            purposes.add(purpose)
        return TextAnalysisTrust(MappingProxyType(keys), fixture_only=True)
    except (ValueError, TypeError, KeyError):
        raise TextAnalysisConfigurationError("invalid_fixture_trust") from None


@dataclass(frozen=True)
class TextAnalysisDecision:
    reason: str
    authority: "VerifiedTextAnalysisAuthority | None" = None

    @property
    def accepted(self) -> bool:
        return self.authority is not None


@dataclass(frozen=True)
class VerifiedTextAnalysisAuthority:
    """Verified bytes and scope; creates no local execution or spend state."""
    kind: str
    compact_jws: str = field(repr=False)
    payload_sha256: str
    payload: Mapping = field(repr=False)
    fixture_only: bool


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _major(value):
    return type(value) is int and 1 <= value <= 2147483647


def _validate_context(context, kind):
    expected = _CONTEXT | ({"expected_output_policy"} if kind == "preview" else set())
    if type(context) is not dict or set(context) != expected:
        raise TextAnalysisConfigurationError("invalid_context")
    valid = (
        all(_matches(context[key], _ID) for key in ("expected_organization_id", "expected_installation_id"))
        and _matches(context["expected_installation_key_id"], _KEY_ID)
        and _matches(context["expected_installation_key_jkt"], _JKT)
        and _matches(context["expected_subject"], _SUBJECT) and len(context["expected_subject"]) <= 512
        and type(context["expected_seat_scope"]) is str and 1 <= len(context["expected_seat_scope"]) <= 128
        and _matches(context["expected_capability"], _NAME)
        and _matches(context["expected_artifact_family"], _NAME)
        and _major(context["expected_target_major"]) and type(context["requested_update_mode"]) is bool
        and (context["expected_fallback_major"] is None or _major(context["expected_fallback_major"]))
    )
    if kind == "preview":
        policy = context["expected_output_policy"]
        valid = valid and type(policy) is dict and policy == OUTPUT_POLICY and type(policy.get("version")) is int
    if not valid:
        raise TextAnalysisConfigurationError("invalid_context")


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if type(value) is list:
        return tuple(_freeze(child) for child in value)
    return value


def verify_authority(token: str, *, kind: str, context: dict, trust: TextAnalysisTrust,
                     schemas: TextAnalysisSchemas) -> TextAnalysisDecision:
    """Verify one explicitly selected type against independently supplied context."""
    if kind not in ("license", "preview"):
        raise TextAnalysisConfigurationError("unsupported_authority")
    _validate_context(context, kind)
    if type(trust) is not TextAnalysisTrust or not trust.keys:
        raise TextAnalysisConfigurationError("invalid_trust")
    def reject(reason):
        return TextAnalysisDecision(reason)
    try:
        if type(token) is not str or not 0 < len(token.encode("ascii")) <= 16384:
            return reject("invalid_compact_jws")
        segments = token.split(".")
        if len(segments) != 3:
            return reject("invalid_compact_jws")
        header_raw = decode(segments[0], 16384)
        payload_raw = decode(segments[1], 16384)
        signature = decode(segments[2], 16384)
    except (SignedObjectRejected, UnicodeError):
        return reject("invalid_compact_jws")
    try:
        # Transport/JSON syntax precedes canonical representation policy.
        json.loads(header_raw.decode("utf-8"))
        json.loads(payload_raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return reject("invalid_compact_jws")
    try:
        header = parse_json(header_raw, maximum=16384, require_canonical=True)
        payload = parse_json(payload_raw, maximum=16384, require_canonical=True)
    except ValueError:
        return reject("noncanonical_json")
    if (type(header) is not dict or set(header) != {"alg", "kid", "typ"}
            or header["alg"] != "ES256" or type(header["kid"]) is not str
            or re.fullmatch(r"bc1\.[a-z]{2}\.[A-Za-z0-9_-]{22}", header["kid"]) is None
            or type(header["typ"]) is not str):
        return reject("invalid_header")
    profile = PREFIX + kind + ":v1"
    if header["typ"] != profile:
        return reject("typ_mismatch")
    audience = "urn:bridge-clean:local-brain:text-analysis-" + kind
    if type(payload) is not dict or payload.get("aud") != audience:
        return reject("audience_mismatch")
    key = trust.keys.get(header["kid"])
    if key is None:
        return reject("unknown_kid")
    if key.purpose != "text-analysis-" + kind:
        return reject("wrong_key_purpose")
    if len(signature) != 64:
        return reject("invalid_signature")
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if not (0 < r < ORDER and 0 < s <= ORDER // 2):
        return reject("invalid_signature")
    try:
        key.public_key.verify(utils.encode_dss_signature(r, s),
            (segments[0] + "." + segments[1]).encode("ascii"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return reject("invalid_signature")
    schema = "schemas/text-analysis-" + kind + "/v1/payload.schema.json"
    if not schemas.valid(schema, payload):
        return reject("schema_invalid")
    fallbacks = payload["fallback_major_versions"]
    if (type(payload["licensed_major_version"]) is not int or type(payload["iat"]) is not int
            or any(type(major) is not int for major in fallbacks)
            or fallbacks != sorted(set(fallbacks))
            or any(major >= payload["licensed_major_version"] for major in fallbacks)
            or (kind == "preview" and (type(payload["execution_limit"]) is not int
                or type(payload["output_policy"]["version"]) is not int))):
        return reject("schema_invalid")
    reconstructed = ("organization:" + payload["organization_id"] + ":installation:" + payload["installation_id"]
                     + ":seat:" + payload["seat_id"] + ":capability:text-analysis")
    if payload["sub"] != reconstructed or payload["sub"] != context["expected_subject"]:
        return reject("subject_mismatch")
    for field, expected, reason in (
        ("organization_id", "expected_organization_id", "organization_mismatch"),
        ("installation_id", "expected_installation_id", "installation_mismatch"),
        ("installation_key_id", "expected_installation_key_id", "installation_key_mismatch"),
        ("installation_key_jkt", "expected_installation_key_jkt", "installation_key_mismatch"),
        ("seat_scope", "expected_seat_scope", "seat_scope_mismatch"),
        ("capability", "expected_capability", "capability_mismatch"),
        ("licensed_major_version", "expected_target_major", "major_version_mismatch"),
        ("compatible_artifact_family", "expected_artifact_family", "artifact_family_mismatch")):
        if payload[field] != context[expected]:
            return reject(reason)
    if context["requested_update_mode"] and not payload["update_rights"]:
        return reject("update_rights_not_authorized")
    if context["expected_fallback_major"] is not None and context["expected_fallback_major"] not in fallbacks:
        return reject("fallback_version_not_authorized")
    if kind == "preview" and payload["output_policy"] != context["expected_output_policy"]:
        return reject("output_policy_mismatch")
    authority = VerifiedTextAnalysisAuthority(kind, token, hashlib.sha256(payload_raw).hexdigest(),
                                            _freeze(payload), trust.fixture_only)
    return TextAnalysisDecision("accepted", authority)
