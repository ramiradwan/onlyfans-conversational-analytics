"""Offline verification and trust selection for CapabilityLicense v1."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from contracts.loader import ContractsIntegrityError, load_trust_set

if TYPE_CHECKING:
    from datetime import datetime

    from app.persistence.auth import AuthenticationStore, VerifiedCapabilityLicenseReference
    from app.security.runtime_policy import AuthContext, RuntimePolicy


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
    r"seat:[A-Za-z0-9._~-]+:capability:[a-z][a-z0-9-]{0,63}$"
)
_EXPECTED_TYP = "urn:bridge-clean:capability-license:v1"
_EXPECTED_AUDIENCE = "urn:bridge-clean:local-brain:capability-license"
_EXPECTED_PROFILE = "urn:bridge-clean:capability-license:v1"
_EXPECTED_LICENSE_TYPE = "capability_license"
_EXPECTED_ISSUER = "urn:bridge-clean:commercial-control-plane"
_EXPECTED_CAPABILITY = "analysis-run"
_EXPECTED_SEAT_SCOPE = "organization-installation-seat"
_PRODUCTION_TRUST_SET = "production/capability-license-v1/trust-set.json"
_FIXTURE_TRUST_SET = "capability-license-v1/trust-set.json"
_MAX_TRUSTED_KEYS = 8
_PAYLOAD_FIELDS = frozenset(
    {
        "profile",
        "license_type",
        "iss",
        "aud",
        "sub",
        "license_id",
        "issuance_id",
        "iat",
        "organization_id",
        "installation_id",
        "installation_key_id",
        "installation_key_jkt",
        "seat_id",
        "seat_scope",
        "capability",
        "licensed_major_version",
        "compatible_artifact_family",
        "update_rights",
        "fallback_major_versions",
    }
)


class CapabilityLicenseTrustUnavailable(RuntimeError):
    """Production CapabilityLicense trust is unavailable or unusable."""


class CapabilityLicenseTrustProvider(Protocol):
    """Purpose-specific source of authenticated CapabilityLicense public trust."""

    def trust_set(self) -> Mapping[str, Any]: ...


class PackagedCapabilityLicenseTrustProvider:
    """Production trust provider fixed to the packaged, manifest-pinned trust set."""

    def trust_set(self) -> Mapping[str, Any]:
        try:
            trust_set = load_trust_set(_PRODUCTION_TRUST_SET, environment="production")
            _trusted_keys(trust_set)
        except (ContractsIntegrityError, ValueError) as error:
            raise CapabilityLicenseTrustUnavailable(
                "Packaged production CapabilityLicense trust is unavailable"
            ) from error
        return trust_set


class FixtureCapabilityLicenseTrustProvider:
    """Development/conformance-only provider for the published test-vector keys."""

    def __init__(self, *, environment: str = "development") -> None:
        if environment not in {"development", "dev", "local", "test"}:
            raise CapabilityLicenseTrustUnavailable(
                "CapabilityLicense fixture trust is development-only"
            )
        self._environment = environment

    def trust_set(self) -> Mapping[str, Any]:
        trust_set = load_trust_set(_FIXTURE_TRUST_SET, environment="development")
        if trust_set.get("production_usable") is not False:
            raise CapabilityLicenseTrustUnavailable(
                "CapabilityLicense fixture trust must not be production usable"
            )
        _trusted_keys(trust_set)
        return trust_set


@dataclass(frozen=True, slots=True)
class CapabilityLicenseVerificationContext:
    """Independently trusted local bindings for one CapabilityLicense decision."""

    expected_subject: str
    expected_organization_id: str
    expected_installation_id: str
    expected_installation_key_id: str
    expected_installation_key_jkt: str
    expected_seat_id: str
    expected_seat_scope: str
    expected_capability: str
    expected_target_major: int
    expected_artifact_family: str
    requested_update_mode: bool
    expected_fallback_major: int | None = None


@dataclass(frozen=True, slots=True)
class VerifiedCapabilityLicense:
    """Verified signed commercial authority without identity semantics."""

    compact_jws: str = field(repr=False, compare=False)
    object_digest: str
    signer_kid: str
    subject: str
    license_id: str
    issuance_id: str
    issued_at: int
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    seat_id: str
    seat_scope: str
    capability: str
    licensed_major_version: int
    compatible_artifact_family: str
    update_rights: bool
    fallback_major_versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CapabilityLicenseVerification:
    valid: bool
    result: str
    license: VerifiedCapabilityLicense | None = None


class CapabilityLicenseVerifier:
    """Verifier bound to one explicit trust provider."""

    def __init__(self, trust_provider: CapabilityLicenseTrustProvider) -> None:
        self._trust_provider = trust_provider

    @classmethod
    def production(cls) -> "CapabilityLicenseVerifier":
        provider = PackagedCapabilityLicenseTrustProvider()
        # Fail during production construction, rather than deferring trust absence
        # until an activation response has already been accepted by transport.
        provider.trust_set()
        return cls(provider)

    def verify(
        self, token: str, *, context: CapabilityLicenseVerificationContext
    ) -> CapabilityLicenseVerification:
        return verify_capability_license(
            token,
            context=context,
            trust_set=self._trust_provider.trust_set(),
        )


def verify_capability_license(
    token: str,
    *,
    context: CapabilityLicenseVerificationContext,
    trust_set: Mapping[str, Any],
) -> CapabilityLicenseVerification:
    """Verify one CapabilityLicense against authenticated trust and local bindings."""

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

    try:
        keys = _trusted_keys(trust_set)
    except ValueError:
        return _outcome(False, "unknown_kid")
    kid = header.get("kid")
    entry = keys.get(kid) if isinstance(kid, str) else None
    if entry is None:
        return _outcome(False, "unknown_kid")
    purpose, public_key = entry
    if purpose != "capability-license":
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
    if (
        payload["installation_key_id"] != context.expected_installation_key_id
        or payload["installation_key_jkt"] != context.expected_installation_key_jkt
    ):
        return _outcome(False, "installation_key_mismatch")
    if (
        payload["seat_id"] != context.expected_seat_id
        or payload["seat_scope"] != context.expected_seat_scope
    ):
        return _outcome(False, "seat_scope_mismatch")
    if payload["capability"] != context.expected_capability:
        return _outcome(False, "capability_mismatch")
    if payload["licensed_major_version"] != context.expected_target_major:
        return _outcome(False, "major_version_mismatch")
    if payload["compatible_artifact_family"] != context.expected_artifact_family:
        return _outcome(False, "artifact_family_mismatch")
    if context.requested_update_mode and payload["update_rights"] is not True:
        return _outcome(False, "update_rights_not_authorized")
    if (
        context.expected_fallback_major is not None
        and context.expected_fallback_major not in payload["fallback_major_versions"]
    ):
        return _outcome(False, "fallback_version_not_authorized")

    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    verified = VerifiedCapabilityLicense(
        compact_jws=token,
        object_digest=digest,
        signer_kid=header["kid"],
        subject=payload["sub"],
        license_id=payload["license_id"],
        issuance_id=payload["issuance_id"],
        issued_at=payload["iat"],
        organization_id=payload["organization_id"],
        installation_id=payload["installation_id"],
        installation_key_id=payload["installation_key_id"],
        installation_key_jkt=payload["installation_key_jkt"],
        seat_id=payload["seat_id"],
        seat_scope=payload["seat_scope"],
        capability=payload["capability"],
        licensed_major_version=payload["licensed_major_version"],
        compatible_artifact_family=payload["compatible_artifact_family"],
        update_rights=payload["update_rights"],
        fallback_major_versions=tuple(payload["fallback_major_versions"]),
    )
    return _outcome(True, "accepted", verified)


def _outcome(
    valid: bool,
    result: str,
    license: VerifiedCapabilityLicense | None = None,
) -> CapabilityLicenseVerification:
    return CapabilityLicenseVerification(valid=valid, result=result, license=license)


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
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _trusted_keys(
    trust_set: Mapping[str, Any],
) -> dict[str, tuple[str, ec.EllipticCurvePublicKey]]:
    entries = trust_set.get("keys")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_TRUSTED_KEYS:
        raise ValueError("invalid CapabilityLicense trust set")
    keys: dict[str, tuple[str, ec.EllipticCurvePublicKey]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid CapabilityLicense trust set")
        purpose = entry.get("purpose")
        jwk = entry.get("jwk")
        if not isinstance(purpose, str) or not isinstance(jwk, Mapping):
            raise ValueError("invalid CapabilityLicense trust set")
        kid = jwk.get("kid")
        if not isinstance(kid, str) or kid in keys:
            raise ValueError("invalid CapabilityLicense trust set")
        keys[kid] = (purpose, _public_key(jwk))
    return keys


def _public_key(jwk: Mapping[str, Any]) -> ec.EllipticCurvePublicKey:
    if set(jwk) != {"crv", "kid", "kty", "x", "y"}:
        raise ValueError("invalid CapabilityLicense trust set")
    if jwk.get("crv") != "P-256" or jwk.get("kty") != "EC":
        raise ValueError("invalid CapabilityLicense trust set")
    try:
        x_bytes = _b64u_decode(jwk["x"])
        y_bytes = _b64u_decode(jwk["y"])
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid CapabilityLicense trust set") from exc
    if len(x_bytes) != 32 or len(y_bytes) != 32:
        raise ValueError("invalid CapabilityLicense trust set")
    try:
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x_bytes, "big"),
            int.from_bytes(y_bytes, "big"),
            ec.SECP256R1(),
        ).public_key()
    except ValueError as exc:
        raise ValueError("invalid CapabilityLicense trust set") from exc


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
            encode_dss_signature(r, s),
            signing_input,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def _valid_payload(payload: Mapping[str, Any]) -> bool:
    fallback = payload.get("fallback_major_versions")
    return (
        set(payload) == _PAYLOAD_FIELDS
        and payload["profile"] == _EXPECTED_PROFILE
        and payload["license_type"] == _EXPECTED_LICENSE_TYPE
        and payload["iss"] == _EXPECTED_ISSUER
        and payload["aud"] == _EXPECTED_AUDIENCE
        and _matches(payload["sub"], _SUBJECT_RE, 1, 512)
        and _matches(payload["license_id"], _UUIDV7_RE, 36, 36)
        and _matches(payload["issuance_id"], _UUIDV7_RE, 36, 36)
        and _integer_in_range(payload["iat"], 0, 4_102_444_800)
        and _matches(payload["organization_id"], _CROSS_PLANE_ID_RE, 1, 128)
        and _matches(payload["installation_id"], _CROSS_PLANE_ID_RE, 1, 128)
        and _matches(payload["installation_key_id"], _INSTALLATION_KEY_ID_RE, 26, 26)
        and _matches(payload["installation_key_jkt"], _INSTALLATION_KEY_JKT_RE, 43, 43)
        and _matches(payload["seat_id"], _CROSS_PLANE_ID_RE, 1, 128)
        and payload["seat_scope"] == _EXPECTED_SEAT_SCOPE
        and payload["capability"] == _EXPECTED_CAPABILITY
        and _integer_in_range(payload["licensed_major_version"], 1, 2_147_483_647)
        and _matches(payload["compatible_artifact_family"], _CROSS_PLANE_ID_RE, 1, 128)
        and isinstance(payload["update_rights"], bool)
        and isinstance(fallback, list)
        and len(fallback) <= 128
        and len(set(fallback)) == len(fallback)
        and all(_integer_in_range(item, 1, 2_147_483_647) for item in fallback)
    )


def _matches(value: object, pattern: re.Pattern[str], minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and pattern.fullmatch(value) is not None
    )


def _integer_in_range(value: object, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )

class CapabilityLicenseVerificationError(ValueError):
    """Raised when signed commercial authority cannot be accepted locally."""

    def __init__(self, result: str) -> None:
        super().__init__(f"CapabilityLicense verification failed: {result}")
        self.result = result


class CapabilityLicenseAuthorityService:
    """Verify, persist, recover, and compose local CapabilityLicense authority."""

    def __init__(
        self,
        store: "AuthenticationStore",
        verifier: CapabilityLicenseVerifier,
        *,
        clock: "Callable[[], datetime] | None" = None,
        verification_source: "Literal['production', 'development', 'conformance']" = "production",
    ) -> None:
        from datetime import datetime, timezone

        self._store = store
        self._verifier = verifier
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verification_source = verification_source

    def verify(
        self,
        token: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> "VerifiedCapabilityLicenseReference":
        from app.persistence.auth import VerifiedCapabilityLicenseReference

        outcome = self._verifier.verify(token, context=context)
        if not outcome.valid or outcome.license is None:
            raise CapabilityLicenseVerificationError(outcome.result)
        verified = outcome.license
        return VerifiedCapabilityLicenseReference(
            reference_id=_reference_id(verified.object_digest),
            license_id=verified.license_id,
            issuance_id=verified.issuance_id,
            object_digest=verified.object_digest,
            compact_jws=verified.compact_jws,
            subject=verified.subject,
            organization_id=verified.organization_id,
            installation_id=verified.installation_id,
            installation_key_id=verified.installation_key_id,
            installation_key_jkt=verified.installation_key_jkt,
            seat_id=verified.seat_id,
            seat_scope=verified.seat_scope,
            capability=verified.capability,
            licensed_major_version=verified.licensed_major_version,
            compatible_artifact_family=verified.compatible_artifact_family,
            update_rights=verified.update_rights,
            fallback_major_versions=verified.fallback_major_versions,
            signer_kid=verified.signer_kid,
            verified_at=self._clock(),
            verification_source=self._verification_source,
        )

    def persist(
        self, reference: "VerifiedCapabilityLicenseReference"
    ) -> "VerifiedCapabilityLicenseReference":
        self._store.record_verified_capability_license(reference)
        return reference

    def accept(
        self,
        token: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> "VerifiedCapabilityLicenseReference":
        return self.persist(self.verify(token, context=context))

    def recover(
        self,
        reference_id: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> "VerifiedCapabilityLicenseReference":
        reference = self._store.verified_capability_license(reference_id)
        if reference is None:
            raise CapabilityLicenseVerificationError("reference_unavailable")
        outcome = self._verifier.verify(reference.compact_jws, context=context)
        if not outcome.valid or outcome.license is None:
            raise CapabilityLicenseVerificationError(outcome.result)
        verified = outcome.license
        durable = (
            reference.object_digest,
            reference.license_id,
            reference.issuance_id,
            reference.subject,
            reference.organization_id,
            reference.installation_id,
            reference.installation_key_id,
            reference.installation_key_jkt,
            reference.seat_id,
            reference.seat_scope,
            reference.capability,
            reference.licensed_major_version,
            reference.compatible_artifact_family,
            reference.update_rights,
            reference.fallback_major_versions,
            reference.signer_kid,
        )
        current = (
            verified.object_digest,
            verified.license_id,
            verified.issuance_id,
            verified.subject,
            verified.organization_id,
            verified.installation_id,
            verified.installation_key_id,
            verified.installation_key_jkt,
            verified.seat_id,
            verified.seat_scope,
            verified.capability,
            verified.licensed_major_version,
            verified.compatible_artifact_family,
            verified.update_rights,
            verified.fallback_major_versions,
            verified.signer_kid,
        )
        if durable != current:
            raise CapabilityLicenseVerificationError("persisted_authority_mismatch")
        return reference

    def runtime_policy(
        self,
        identity: "AuthContext",
        grant_reference_ids: tuple[str, ...],
        capability_reference_id: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> "RuntimePolicy":
        self.recover(capability_reference_id, context=context)
        return self._store.build_runtime_policy_with_capability(
            identity,
            grant_reference_ids,
            capability_reference_id,
        )


def _reference_id(object_digest: str) -> str:
    return "caplic." + object_digest
