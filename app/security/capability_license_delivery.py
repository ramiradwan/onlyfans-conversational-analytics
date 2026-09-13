"""Hosted CapabilityLicense activation and replacement delivery clients."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import struct
from dataclasses import dataclass, fields
from typing import Literal, Mapping

from app.persistence.auth import InstallationKeyReference, VerifiedCapabilityLicenseReference
from app.security.capability_license_verifier import (
    CapabilityLicenseAuthorityService,
    CapabilityLicenseVerificationContext,
)
from app.security.hosted_grants import (
    HostedTransport,
    InstallationProofAuthority,
    TransportResponse,
)

_ACTIVATION_PACKAGE_PROFILE = "urn:bridge-clean:capability-license-activation-package:v1"
_ACTIVATION_PROFILE = "urn:bridge-clean:capability-license-activation:v1"
_PROOF_PROFILE = "urn:bridge-clean:capability-license-activation-proof:v1"
_PROOF_AUDIENCE = "urn:bridge-clean:commercial-control-plane:capability-license-activation"
_REISSUE_AUTH_PROFILE = "urn:bridge-clean:capability-license-reissue-authorization:v1"
_REISSUE_PACKAGE_PROFILE = "urn:bridge-clean:capability-license-reissue-package:v1"
_REISSUE_PROFILE = "urn:bridge-clean:capability-license-reissue:v1"
_PROOF_DOMAIN = b"BRIDGE-CLEAN-CAPABILITY-LICENSE-ACTIVATION-PROOF-V1\x00"
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_B64U = re.compile(r"^[A-Za-z0-9_-]+$")
_UUIDV7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_KEY_ID = re.compile(r"^ik1\.[A-Za-z0-9_-]{22}$")
_JKT = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
_REASONS = frozenset(
    {
        "replacement-installation",
        "lost-or-destroyed-installation",
        "inaccessible-original-hardware",
    }
)


class CapabilityLicenseDeliveryError(RuntimeError):
    """Base hosted-delivery failure with no signed authority implication."""


class CapabilityLicenseDeliveryUnavailable(CapabilityLicenseDeliveryError):
    """Transport or hosted availability did not produce an authoritative result."""


class CapabilityLicenseDeliveryRefused(CapabilityLicenseDeliveryError):
    """Hosted control plane authoritatively refused a delivery operation."""

    def __init__(self, result: str) -> None:
        super().__init__("CapabilityLicense delivery was refused")
        self.result = result


@dataclass(frozen=True, slots=True)
class ActivationPackage:
    profile: str
    exchange_id: str
    organization_id: str
    installation_id: str
    capability: str
    authority_target: str
    proof_profile: str
    proof_purpose: str
    proof_challenge_path: str
    activation_path: str


@dataclass(frozen=True, slots=True)
class ReissuePackage:
    profile: str
    reissue_authorization_profile: str
    reissue_authorization_id: str
    organization_id: str
    prior_license_id: str
    prior_issuance_id: str
    replacement_installation_id: str
    reissue_reason: str
    proof_profile: str
    proof_purpose: str
    finalization_profile: str
    proof_challenge_path: str
    finalization_path: str


@dataclass(frozen=True, slots=True)
class ReissueAuthorization:
    package: ReissuePackage
    package_encoded: str
    customer_step_up_ref: str
    decision_audit_ref: str


class CapabilityLicenseDeliveryClient:
    """Perform hosted delivery while treating local signature verification as authority."""

    def __init__(
        self,
        transport: HostedTransport,
        installation_key: InstallationProofAuthority,
        authority: CapabilityLicenseAuthorityService,
    ) -> None:
        self._transport = transport
        self._installation_key = installation_key
        self._authority = authority

    def activate(
        self,
        encoded_package: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> VerifiedCapabilityLicenseReference:
        package = decode_activation_package(encoded_package)
        self._require_context_package_binding(
            organization_id=package.organization_id,
            installation_id=package.installation_id,
            capability=package.capability,
            context=context,
        )
        key = self._installation_key.ensure_ready()
        self._require_key_binding(key, context)
        challenge = self._proof_challenge(
            path=package.proof_challenge_path,
            purpose="capability-license-activate",
            organization_id=package.organization_id,
            authority_reference_id=package.exchange_id,
            installation_id=package.installation_id,
        )
        request: dict[str, object] = {
            "profile": _ACTIVATION_PROFILE,
            "exchange_id": package.exchange_id,
            "organization_id": package.organization_id,
            "installation_id": package.installation_id,
        }
        response = self._request(
            package.activation_path,
            {
                "request": request,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request,
                    path=package.activation_path,
                    purpose="capability-license-activate",
                    organization_id=package.organization_id,
                    installation_id=package.installation_id,
                    key=key,
                ),
            },
        )
        if _retryable(response.status_code):
            raise CapabilityLicenseDeliveryUnavailable("Activation result is unavailable")
        if response.status_code != 201:
            raise CapabilityLicenseDeliveryRefused("activation_refused")
        document = _response_object(response)
        token = self._validate_activation_response(
            document, package=package, key=key, context=context
        )
        reference = self._authority.verify(token, context=context)
        if (
            document["license_id"] != reference.license_id
            or document["issuance_id"] != reference.issuance_id
            or document["seat_id"] != reference.seat_id
            or document["seat_scope"] != reference.seat_scope
            or document["licensed_major_version"] != reference.licensed_major_version
        ):
            raise CapabilityLicenseDeliveryUnavailable(
                "Activation response and signed CapabilityLicense disagree"
            )
        return self._authority.persist(reference)

    def authorize_reissue(
        self,
        prior: VerifiedCapabilityLicenseReference,
        *,
        replacement_installation_id: str,
        customer_step_up_ref: str,
        reissue_reason: str = "replacement-installation",
    ) -> ReissueAuthorization:
        if not _ID.fullmatch(replacement_installation_id) or not _ID.fullmatch(customer_step_up_ref):
            raise ValueError("Reissue authorization bindings are invalid")
        if reissue_reason not in _REASONS:
            raise ValueError("Reissue reason is invalid")
        path = f"/v1/capability-licenses/{prior.license_id}/reissue-authorizations"
        request: dict[str, object] = {
            "profile": _REISSUE_AUTH_PROFILE,
            "organization_id": prior.organization_id,
            "prior_license_id": prior.license_id,
            "prior_issuance_id": prior.issuance_id,
            "replacement_installation_id": replacement_installation_id,
            "customer_step_up_ref": customer_step_up_ref,
            "reissue_reason": reissue_reason,
        }
        response = self._request(path, request)
        if _retryable(response.status_code):
            raise CapabilityLicenseDeliveryUnavailable("Reissue authorization is unavailable")
        if response.status_code != 201:
            raise CapabilityLicenseDeliveryRefused("reissue_authorization_refused")
        document = _response_object(response)
        expected = {
            "profile", "reissue_authorization_id", "authorization_state", "authorized_at",
            "organization_id", "prior_license_id", "prior_issuance_id",
            "replacement_installation_id", "reissue_reason", "customer_step_up_ref",
            "decision_audit_ref", "handoff_package", "handoff_package_encoded",
        }
        if (
            set(document) != expected
            or document.get("profile") != _REISSUE_AUTH_PROFILE
            or document.get("authorization_state") != "pending"
            or document.get("organization_id") != prior.organization_id
            or document.get("prior_license_id") != prior.license_id
            or document.get("prior_issuance_id") != prior.issuance_id
            or document.get("replacement_installation_id") != replacement_installation_id
            or document.get("reissue_reason") != reissue_reason
            or document.get("customer_step_up_ref") != customer_step_up_ref
            or not _timestamp(document.get("authorized_at"))
            or not _uuid(document.get("reissue_authorization_id"))
            or not _identifier(document.get("decision_audit_ref"))
            or not isinstance(document.get("handoff_package"), dict)
            or not isinstance(document.get("handoff_package_encoded"), str)
        ):
            raise CapabilityLicenseDeliveryUnavailable("Reissue authorization response is invalid")
        package = decode_reissue_package(str(document["handoff_package_encoded"]))
        if _dataclass_mapping(package) != document["handoff_package"]:
            raise CapabilityLicenseDeliveryUnavailable("Reissue handoff encodings disagree")
        if (
            package.reissue_authorization_id != document["reissue_authorization_id"]
            or package.organization_id != prior.organization_id
            or package.prior_license_id != prior.license_id
            or package.prior_issuance_id != prior.issuance_id
            or package.replacement_installation_id != replacement_installation_id
            or package.reissue_reason != reissue_reason
        ):
            raise CapabilityLicenseDeliveryUnavailable("Reissue handoff bindings are invalid")
        return ReissueAuthorization(
            package=package,
            package_encoded=str(document["handoff_package_encoded"]),
            customer_step_up_ref=customer_step_up_ref,
            decision_audit_ref=str(document["decision_audit_ref"]),
        )

    def finalize_reissue(
        self,
        encoded_package: str,
        *,
        context: CapabilityLicenseVerificationContext,
    ) -> VerifiedCapabilityLicenseReference:
        package = decode_reissue_package(encoded_package)
        self._require_context_package_binding(
            organization_id=package.organization_id,
            installation_id=package.replacement_installation_id,
            capability=context.expected_capability,
            context=context,
        )
        key = self._installation_key.ensure_ready()
        self._require_key_binding(key, context)
        challenge = self._proof_challenge(
            path=package.proof_challenge_path,
            purpose="capability-license-reissue",
            organization_id=package.organization_id,
            authority_reference_id=package.reissue_authorization_id,
            installation_id=package.replacement_installation_id,
        )
        request: dict[str, object] = {
            "profile": _REISSUE_PROFILE,
            "reissue_authorization_id": package.reissue_authorization_id,
            "organization_id": package.organization_id,
            "prior_license_id": package.prior_license_id,
            "prior_issuance_id": package.prior_issuance_id,
            "replacement_installation_id": package.replacement_installation_id,
            "reissue_reason": package.reissue_reason,
        }
        response = self._request(
            package.finalization_path,
            {
                "request": request,
                "proof": self._proof(
                    challenge=challenge,
                    request_body=request,
                    path=package.finalization_path,
                    purpose="capability-license-reissue",
                    organization_id=package.organization_id,
                    installation_id=package.replacement_installation_id,
                    key=key,
                ),
            },
        )
        if _retryable(response.status_code):
            raise CapabilityLicenseDeliveryUnavailable("Reissue finalization is unavailable")
        if response.status_code != 201:
            raise CapabilityLicenseDeliveryRefused("reissue_finalization_refused")
        document = _response_object(response)
        token = self._validate_reissue_response(
            document, package=package, key=key, context=context
        )
        reference = self._authority.verify(token, context=context)
        replacement = document["replacement_issuance"]
        if not isinstance(replacement, dict) or (
            replacement.get("license_id") != reference.license_id
            or replacement.get("issuance_id") != reference.issuance_id
            or replacement.get("installation_id") != reference.installation_id
            or replacement.get("installation_key_id") != reference.installation_key_id
            or replacement.get("installation_key_jkt") != reference.installation_key_jkt
            or document["seat_id"] != reference.seat_id
            or document["seat_scope"] != reference.seat_scope
            or document["licensed_major_version"] != reference.licensed_major_version
        ):
            raise CapabilityLicenseDeliveryUnavailable(
                "Reissue response and signed CapabilityLicense disagree"
            )
        return self._authority.persist(reference)

    def _proof_challenge(
        self,
        *,
        path: str,
        purpose: Literal["capability-license-activate", "capability-license-reissue"],
        organization_id: str,
        authority_reference_id: str,
        installation_id: str,
    ) -> str:
        response = self._request(
            path,
            {
                "profile": _PROOF_PROFILE,
                "purpose": purpose,
                "organization_id": organization_id,
                "authority_reference_id": authority_reference_id,
            },
        )
        if _retryable(response.status_code):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense proof challenge is unavailable")
        if response.status_code != 201:
            raise CapabilityLicenseDeliveryRefused("proof_challenge_refused")
        document = _response_object(response)
        expected = {
            "profile", "purpose", "organization_id", "authority_reference_id",
            "installation_id", "proof_challenge_id", "challenge", "audience",
            "issued_at", "expires_at",
        }
        challenge = document.get("challenge")
        if (
            set(document) != expected
            or document.get("profile") != _PROOF_PROFILE
            or document.get("purpose") != purpose
            or document.get("organization_id") != organization_id
            or document.get("authority_reference_id") != authority_reference_id
            or document.get("installation_id") != installation_id
            or document.get("audience") != _PROOF_AUDIENCE
            or not _uuid(document.get("proof_challenge_id"))
            or not isinstance(challenge, str)
            or len(_b64u_decode(challenge)) != 32
            or not _timestamp(document.get("issued_at"))
            or not _timestamp(document.get("expires_at"))
        ):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense proof challenge is invalid")
        return challenge

    def _proof(
        self,
        *,
        challenge: str,
        request_body: Mapping[str, object],
        path: str,
        purpose: str,
        organization_id: str,
        installation_id: str,
        key: InstallationKeyReference,
    ) -> dict[str, str]:
        values = (
            _b64u_decode(challenge),
            b"POST",
            path.encode("ascii"),
            hashlib.sha256(_canonical_json(request_body)).digest(),
            purpose.encode("ascii"),
            installation_id.encode("utf-8"),
            organization_id.encode("utf-8"),
            key.installation_key_id.encode("utf-8"),
            _PROOF_AUDIENCE.encode("ascii"),
        )
        canonical = bytearray(_PROOF_DOMAIN)
        for value in values:
            canonical += struct.pack("!I", len(value)) + value
        proof = self._installation_key.sign_challenge(bytes(canonical))
        if (
            proof.installation_key_id != key.installation_key_id
            or proof.algorithm != "ES256"
            or len(proof.signature) != 64
            or int.from_bytes(proof.signature[32:], "big") > _P256_ORDER // 2
        ):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense installation proof is invalid")
        return {
            "challenge": challenge,
            "key_id": key.installation_key_id,
            "signature": _b64u(proof.signature),
        }

    def _request(self, path: str, body: Mapping[str, object]) -> TransportResponse:
        try:
            response = self._transport.request("POST", path, json_body=body)
        except Exception as error:
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense transport failed") from error
        if (
            not isinstance(response, TransportResponse)
            or not isinstance(response.status_code, int)
            or isinstance(response.status_code, bool)
            or not isinstance(response.body, bytes)
            or len(response.body) > 65_536
            or response.content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense transport response is invalid")
        return response

    @staticmethod
    def _require_context_package_binding(
        *,
        organization_id: str,
        installation_id: str,
        capability: str,
        context: CapabilityLicenseVerificationContext,
    ) -> None:
        if (
            organization_id != context.expected_organization_id
            or installation_id != context.expected_installation_id
            or capability != context.expected_capability
        ):
            raise CapabilityLicenseDeliveryRefused("package_binding_mismatch")

    @staticmethod
    def _require_key_binding(
        key: InstallationKeyReference, context: CapabilityLicenseVerificationContext
    ) -> None:
        if (
            key.installation_key_id != context.expected_installation_key_id
            or key.installation_key_jkt != context.expected_installation_key_jkt
        ):
            raise CapabilityLicenseDeliveryRefused("installation_key_mismatch")

    @staticmethod
    def _validate_activation_response(
        document: Mapping[str, object],
        *,
        package: ActivationPackage,
        key: InstallationKeyReference,
        context: CapabilityLicenseVerificationContext,
    ) -> str:
        expected = {
            "profile", "exchange_id", "activation_id", "organization_id",
            "installation_id", "installation_key_id", "installation_key_jkt",
            "seat_id", "seat_scope", "capability", "licensed_major_version",
            "license_id", "issuance_id", "issuance_state", "activated_at",
            "capability_license",
        }
        token = document.get("capability_license")
        if (
            set(document) != expected
            or document.get("profile") != _ACTIVATION_PROFILE
            or document.get("exchange_id") != package.exchange_id
            or document.get("organization_id") != package.organization_id
            or document.get("installation_id") != package.installation_id
            or document.get("installation_key_id") != key.installation_key_id
            or document.get("installation_key_jkt") != key.installation_key_jkt
            or document.get("seat_id") != context.expected_seat_id
            or document.get("seat_scope") != context.expected_seat_scope
            or document.get("capability") != context.expected_capability
            or document.get("licensed_major_version") != context.expected_target_major
            or document.get("issuance_state") != "current"
            or not _uuid(document.get("activation_id"))
            or not _uuid(document.get("license_id"))
            or not _uuid(document.get("issuance_id"))
            or not _timestamp(document.get("activated_at"))
            or not isinstance(token, str)
        ):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense activation response is invalid")
        return token

    @staticmethod
    def _validate_reissue_response(
        document: Mapping[str, object],
        *,
        package: ReissuePackage,
        key: InstallationKeyReference,
        context: CapabilityLicenseVerificationContext,
    ) -> str:
        expected = {
            "profile", "reissue_id", "reissue_authorization_id", "originating_exchange_id",
            "organization_id", "seat_id", "seat_scope", "capability",
            "licensed_major_version", "customer_step_up_ref", "decision_audit_ref",
            "replacement_proof_ref", "reissue_reason", "reissued_at", "prior_issuance",
            "replacement_issuance", "capability_license",
        }
        prior = document.get("prior_issuance")
        replacement = document.get("replacement_issuance")
        token = document.get("capability_license")
        if (
            set(document) != expected
            or document.get("profile") != _REISSUE_PROFILE
            or document.get("reissue_authorization_id") != package.reissue_authorization_id
            or document.get("organization_id") != package.organization_id
            or document.get("seat_id") != context.expected_seat_id
            or document.get("seat_scope") != context.expected_seat_scope
            or document.get("capability") != context.expected_capability
            or document.get("licensed_major_version") != context.expected_target_major
            or document.get("reissue_reason") != package.reissue_reason
            or not _uuid(document.get("reissue_id"))
            or not _uuid(document.get("originating_exchange_id"))
            or not _identifier(document.get("customer_step_up_ref"))
            or not _identifier(document.get("decision_audit_ref"))
            or not _identifier(document.get("replacement_proof_ref"))
            or not _timestamp(document.get("reissued_at"))
            or not isinstance(prior, dict)
            or set(prior) != {"license_id", "issuance_id", "installation_id", "installation_key_id", "installation_key_jkt", "state"}
            or prior.get("license_id") != package.prior_license_id
            or prior.get("issuance_id") != package.prior_issuance_id
            or prior.get("state") != "superseded"
            or not isinstance(replacement, dict)
            or set(replacement) != {"license_id", "issuance_id", "installation_id", "installation_key_id", "installation_key_jkt", "state"}
            or replacement.get("installation_id") != package.replacement_installation_id
            or replacement.get("installation_key_id") != key.installation_key_id
            or replacement.get("installation_key_jkt") != key.installation_key_jkt
            or replacement.get("state") != "current"
            or not _uuid(replacement.get("license_id"))
            or not _uuid(replacement.get("issuance_id"))
            or not isinstance(token, str)
        ):
            raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense reissue response is invalid")
        return token


def decode_activation_package(encoded: str) -> ActivationPackage:
    document = _decode_package(encoded)
    required = tuple(field.name for field in fields(ActivationPackage))
    if tuple(document) != required:
        raise CapabilityLicenseDeliveryRefused("activation_package_schema_invalid")
    package = ActivationPackage(**{name: document[name] for name in required})
    if not all(isinstance(getattr(package, name), str) for name in required):
        raise CapabilityLicenseDeliveryRefused("activation_package_schema_invalid")
    if (
        package.profile != _ACTIVATION_PACKAGE_PROFILE
        or not _uuid(package.exchange_id)
        or not _identifier(package.organization_id)
        or not _identifier(package.installation_id)
        or package.capability != "analysis-run"
        or package.authority_target != "capability-license"
        or package.proof_profile != _PROOF_PROFILE
        or package.proof_purpose != "capability-license-activate"
        or package.proof_challenge_path != f"/v1/installations/{package.installation_id}/capability-license-proof-challenges"
        or package.activation_path != f"/v1/capability-license-exchanges/{package.exchange_id}:activate"
    ):
        raise CapabilityLicenseDeliveryRefused("activation_package_binding_invalid")
    return package


def decode_reissue_package(encoded: str) -> ReissuePackage:
    document = _decode_package(encoded)
    required = tuple(field.name for field in fields(ReissuePackage))
    if tuple(document) != required:
        raise CapabilityLicenseDeliveryRefused("reissue_package_schema_invalid")
    package = ReissuePackage(**{name: document[name] for name in required})
    if not all(isinstance(getattr(package, name), str) for name in required):
        raise CapabilityLicenseDeliveryRefused("reissue_package_schema_invalid")
    if (
        package.profile != _REISSUE_PACKAGE_PROFILE
        or package.reissue_authorization_profile != _REISSUE_AUTH_PROFILE
        or not _uuid(package.reissue_authorization_id)
        or not _identifier(package.organization_id)
        or not _uuid(package.prior_license_id)
        or not _uuid(package.prior_issuance_id)
        or not _identifier(package.replacement_installation_id)
        or package.reissue_reason not in _REASONS
        or package.proof_profile != _PROOF_PROFILE
        or package.proof_purpose != "capability-license-reissue"
        or package.finalization_profile != _REISSUE_PROFILE
        or package.proof_challenge_path != f"/v1/installations/{package.replacement_installation_id}/capability-license-proof-challenges"
        or package.finalization_path != f"/v1/capability-license-reissue-authorizations/{package.reissue_authorization_id}:finalize"
    ):
        raise CapabilityLicenseDeliveryRefused("reissue_package_binding_invalid")
    return package


def _decode_package(encoded: str) -> dict[str, object]:
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= 1400 or not _B64U.fullmatch(encoded):
        raise CapabilityLicenseDeliveryRefused("package_encoding_invalid")
    try:
        raw = _b64u_decode(encoded)
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CapabilityLicenseDeliveryRefused("package_encoding_invalid") from None
    if not isinstance(document, dict):
        raise CapabilityLicenseDeliveryRefused("package_encoding_invalid")
    # Contract handoff packages use compact JSON in the published member order.
    if json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8") != raw:
        raise CapabilityLicenseDeliveryRefused("package_encoding_invalid")
    return document


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _response_object(response: TransportResponse) -> dict[str, object]:
    try:
        value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense response JSON is invalid") from None
    if not isinstance(value, dict):
        raise CapabilityLicenseDeliveryUnavailable("CapabilityLicense response JSON is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _b64u_decode(value: str) -> bytes:
    if not _B64U.fullmatch(value) or "=" in value:
        raise ValueError("invalid base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64url") from error
    if _b64u(raw) != value:
        raise ValueError("noncanonical base64url")
    return raw


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _retryable(status: int) -> bool:
    return status in {408, 425, 429} or status >= 500


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUIDV7.fullmatch(value) is not None


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _timestamp(value: object) -> bool:
    return isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None


def _dataclass_mapping(value: object) -> dict[str, object]:
    return {field.name: getattr(value, field.name) for field in fields(value)}
