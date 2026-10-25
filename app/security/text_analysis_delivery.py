"""Offline validation of original text-analysis authority delivery bytes."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import re
from types import MappingProxyType
from typing import Mapping

from app.security.text_analysis_authority import (
    TextAnalysisConfigurationError,
    TextAnalysisTrust,
    VerifiedTextAnalysisAuthority,
    verify_authority,
)
from app.security.text_analysis_contract import (
    PREFIX,
    TextAnalysisContractError,
    TextAnalysisSchemas,
    canonical,
    parse_json,
    release_identity,
    supported_profiles,
)


RESPONSE_MAX_BYTES = 24576
ISSUANCE_REQUEST_MAX_BYTES = 8192
_ISSUANCE_DEFINITIONS = "schemas/text-analysis-authority-issuance/v1/definitions.schema.json"
_RESPONSE_SCHEMA = "schemas/text-analysis-authority-delivery/v1/response.schema.json"
_BINDING_FIELDS = ("organization_id", "installation_id", "installation_key_id", "installation_key_jkt")


class TextAnalysisDeliveryError(TextAnalysisContractError):
    """Delivery bytes do not match their independently retained original basis."""


@dataclass(frozen=True)
class ValidatedTextAnalysisDelivery:
    """Verified delivery bytes, without persistence or execution authority."""

    response_bytes: bytes = field(repr=False)
    original_issuance_request: bytes = field(repr=False)
    authority: VerifiedTextAnalysisAuthority = field(repr=False)
    operation_id: str = field(repr=False)
    issuance_authorization_id: str = field(repr=False)
    issuance_request_digest: str
    committed_at: str
    issued_at: int
    original_supported_profiles: tuple[str, ...]
    original_release_identity: Mapping[str, str] = field(repr=False)


def _object(raw: bytes, maximum: int, *, canonical_required: bool = False) -> dict:
    try:
        value = parse_json(raw, maximum=maximum, require_canonical=canonical_required)
    except ValueError:
        raise TextAnalysisDeliveryError("invalid_delivery_bytes") from None
    if type(value) is not dict:
        raise TextAnalysisDeliveryError("invalid_delivery_object")
    return value


def validate_delivery_response(
    raw: bytes,
    *,
    original_issuance_request: bytes,
    expected_seat_id: str,
    authority_context: dict,
    trust: TextAnalysisTrust,
    schemas: TextAnalysisSchemas,
    previous_response: bytes | None = None,
) -> ValidatedTextAnalysisDelivery:
    """Bind D to canonical original I and independent original seat/token context.

    The caller retains I and establishes context independently of the response.
    Recovery supplies the original context and, when available, original D bytes;
    it does not select a current release or negotiate another supported set.
    Hosted registration, protected transport and durable persistence are separate.
    """
    response = _object(raw, RESPONSE_MAX_BYTES)
    schemas.require(_RESPONSE_SCHEMA, response)
    original = _object(original_issuance_request, ISSUANCE_REQUEST_MAX_BYTES,
                       canonical_required=True)
    profile = original.get("authority_profile")
    kind = ({PREFIX + "license:v1": "license", PREFIX + "preview:v1": "preview"}.get(profile)
            if type(profile) is str else None)
    if kind is None:
        raise TextAnalysisDeliveryError("unsupported_authority")
    schemas.require_definition(_ISSUANCE_DEFINITIONS, kind + "Request", original)
    # These are the original retained statements, not current release admission.
    profiles = supported_profiles(canonical(original["supported_profiles"]), schemas=schemas,
        authority=kind, expected_profiles=tuple(original["supported_profiles"]["profiles"]))
    release = release_identity(canonical(original["release_identity"]), schemas=schemas,
                               expected=original["release_identity"])
    if (type(authority_context) is not dict or type(expected_seat_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", expected_seat_id) is None):
        raise TextAnalysisConfigurationError("invalid_delivery_context")
    authority_context = copy.deepcopy(authority_context)
    binding = original["binding"]
    if any(binding[name] != authority_context.get("expected_" + name) for name in _BINDING_FIELDS):
        raise TextAnalysisConfigurationError("delivery_context_mismatch")
    subject = (f"organization:{binding['organization_id']}:installation:{binding['installation_id']}"
               f":seat:{expected_seat_id}:capability:text-analysis")
    if authority_context.get("expected_subject") != subject:
        raise TextAnalysisConfigurationError("delivery_seat_context_mismatch")
    digest = hashlib.sha256(original_issuance_request).hexdigest()
    for name in ("operation_id", "issuance_authorization_id", "authority_profile", "binding"):
        if response[name] != original[name]:
            raise TextAnalysisDeliveryError("delivery_original_mismatch")
    if response["issuance_request_digest"] != digest:
        raise TextAnalysisDeliveryError("issuance_request_digest_mismatch")
    if (response["seat_id"] != expected_seat_id
            or response["seat_scope"] != authority_context.get("expected_seat_scope")):
        raise TextAnalysisDeliveryError("delivery_seat_mismatch")
    decision = verify_authority(response["signed_authority"], kind=kind,
        context=authority_context, trust=trust, schemas=schemas)
    if not decision.accepted:
        raise TextAnalysisDeliveryError("signed_authority_" + decision.reason)
    authority = decision.authority
    identifier = "license_id" if kind == "license" else "permit_id"
    for name in ("issuance_id", identifier, "seat_id", "seat_scope"):
        if response[name] != authority.payload[name]:
            raise TextAnalysisDeliveryError("delivery_signed_scope_mismatch")
    if previous_response is not None:
        if (type(previous_response) is not bytes or not 0 < len(previous_response) <= RESPONSE_MAX_BYTES
                or raw != previous_response):
            raise TextAnalysisDeliveryError("delivery_recovery_bytes_mismatch")
    # committed_at is original delivery metadata; it need not equal signed iat.
    return ValidatedTextAnalysisDelivery(raw, original_issuance_request, authority,
        response["operation_id"], response["issuance_authorization_id"], digest,
        response["committed_at"], authority.payload["iat"], profiles,
        MappingProxyType(dict(release)))
