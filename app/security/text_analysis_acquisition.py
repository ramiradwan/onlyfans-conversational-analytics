"""Pure native validation of acquisition projections and issuance inputs."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import re

from app.security.signed_objects import decode
from app.security.text_analysis_contract import (
    PREFIX, OUTPUT_POLICY, TextAnalysisContractError, TextAnalysisSchemas,
    canonical, parse_json, release_identity, supported_profiles,
)

_X = PREFIX + "preview-acquisition:v1"
_I = PREFIX + "authority-issuance:v1"
_ACTION = "acquire-text-analysis-preview"
_X_DEFINITIONS = "schemas/text-analysis-preview-acquisition/v1/definitions.schema.json"
_I_DEFINITIONS = "schemas/text-analysis-authority-issuance/v1/definitions.schema.json"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class TextAnalysisAcquisitionError(TextAnalysisContractError):
    """Protected bytes or their independently retained meaning differ."""


@dataclass(frozen=True)
class AcquisitionContext:
    original_prepare: bytes = field(repr=False)
    expected_scope: bytes = field(repr=False)
    operation_expires_at: str
    confirmation_not_after: str | None = None


@dataclass(frozen=True)
class IssuanceContext:
    """Independently retained authorized context, without a service route."""
    authority_kind: str
    operation_id: str
    issuance_authorization_id: str
    binding: bytes = field(repr=False)
    expected_scope: bytes = field(repr=False)
    supported_profiles: tuple[str, ...]
    release_identity: bytes = field(repr=False)
    expires_at: str


@dataclass(frozen=True)
class NativeConsent:
    operation_id: str
    confirmation_digest: str
    decision_digest: str
    decision_reference: str
    action: str


@dataclass(frozen=True)
class AuthorizedConfirmation:
    """Validated carrier; does not establish hosted authorization or permission."""
    confirmation_bytes: bytes = field(repr=False)
    context: IssuanceContext = field(repr=False)
    confirmation_digest: str


@dataclass(frozen=True)
class _Retained:
    """Bounded original pins, distinct from the current readable projection."""
    expires_at: str | None = None
    confirmation: bytes | None = field(default=None, repr=False)
    confirmation_expires_at: str | None = None
    decision: bytes | None = field(default=None, repr=False)
    decision_reference: str | None = None
    entry_reference: str | None = None
    authorized: bool = False


@dataclass(frozen=True)
class ValidatedAcquisition:
    state: str
    response_bytes: bytes = field(repr=False)
    context: AcquisitionContext = field(repr=False)
    operation: str
    validated_at: datetime
    expires_at: str | None = None
    confirmation: bytes | None = field(default=None, repr=False)
    confirmation_digest: str | None = None
    decision: bytes | None = field(default=None, repr=False)
    decision_digest: str | None = None
    decision_reference: str | None = None
    entry_reference: str | None = None
    _retained: _Retained | None = field(default=None, repr=False)


def _need(value, reason):
    if not value:
        raise TextAnalysisAcquisitionError(reason)


def _object(raw, maximum):
    try:
        value = parse_json(raw, maximum=maximum)
        _need(type(value) is dict, "invalid_object")
        return value
    except ValueError:
        raise TextAnalysisAcquisitionError("invalid_object") from None


def _timestamp(value):
    _need(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", value), "invalid_timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise TextAnalysisAcquisitionError("invalid_timestamp") from None


def _clock(value):
    _need(type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0), "invalid_clock")
    return value


def _reference(value):
    try:
        _need(len(decode(value, 32)) == 32, "invalid_reference")
    except ValueError:
        raise TextAnalysisAcquisitionError("invalid_reference") from None


def _binding(value):
    names = {"organization_id", "installation_id", "installation_key_id", "installation_key_jkt"}
    _need(type(value) is dict and set(value) == names, "invalid_binding")
    _need(all(type(value[k]) is str and _ID.fullmatch(value[k]) for k in ("organization_id", "installation_id")), "invalid_binding")
    _need(type(value["installation_key_id"]) is str and re.fullmatch(r"ik1\.[A-Za-z0-9_-]{22}", value["installation_key_id"]), "invalid_binding")
    _need(type(value["installation_key_jkt"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{43}", value["installation_key_jkt"]), "invalid_binding")


def _scope(value, kind):
    fields = {"seat_id", "seat_scope", "capability", "licensed_major_version", "compatible_artifact_family", "update_rights", "fallback_major_versions"}
    _need(kind in ("license", "preview"), "unsupported_authority")
    if kind == "preview":
        fields |= {"execution_limit", "output_policy"}
    _need(type(value) is dict and set(value) == fields, "invalid_scope")
    _need(type(value["seat_id"]) is str and _ID.fullmatch(value["seat_id"])
          and value["seat_scope"] == "organization-installation-seat"
          and value["capability"] == "text-analysis" and value["compatible_artifact_family"] == "text-analysis-artifact", "invalid_scope")
    major, fallbacks = value["licensed_major_version"], value["fallback_major_versions"]
    _need(type(major) is int and 1 <= major <= 2147483647 and type(value["update_rights"]) is bool, "invalid_scope")
    _need(type(fallbacks) is list and len(fallbacks) <= 128 and all(type(v) is int and 1 <= v < major for v in fallbacks), "invalid_scope")
    _need(fallbacks == sorted(set(fallbacks)), "invalid_scope")
    if kind == "preview":
        _need(type(value["execution_limit"]) is int and value["execution_limit"] == 1
              and type(value["output_policy"]) is dict and value["output_policy"] == OUTPUT_POLICY
              and type(value["output_policy"].get("version")) is int, "invalid_scope")


def _base(context, schemas):
    _need(type(context) is AcquisitionContext, "invalid_context")
    original = _object(context.original_prepare, 8192)
    schemas.require_definition(_X_DEFINITIONS, "prepareRequest", original)
    _binding(original["binding"])
    profiles = original["supported_profiles"]
    supported_profiles(canonical(profiles), schemas=schemas, authority="preview", expected_profiles=tuple(profiles["profiles"]))
    release_identity(canonical(original["release_identity"]), schemas=schemas, expected=original["release_identity"])
    scope = _object(context.expected_scope, 4096)
    _scope(scope, "preview")
    _timestamp(context.operation_expires_at)
    if context.confirmation_not_after is not None:
        _need(_timestamp(context.confirmation_not_after) <= _timestamp(context.operation_expires_at), "invalid_deadline_context")
    return original, scope


def validate_authorized_confirmation(raw: bytes, *, context: IssuanceContext,
                                     schemas: TextAnalysisSchemas) -> AuthorizedConfirmation:
    """Compare a protected confirmation with an independent authorized basis."""
    _need(type(context) is IssuanceContext and context.authority_kind in ("license", "preview"), "invalid_issuance_context")
    value = _object(raw, 4096)
    schemas.require_definition(_I_DEFINITIONS, context.authority_kind + "Confirmation", value)
    binding = _object(context.binding, 1024)
    _binding(binding)
    scope = _object(context.expected_scope, 4096)
    _scope(scope, context.authority_kind)
    release = _object(context.release_identity, 1024)
    release_identity(context.release_identity, schemas=schemas, expected=release)
    support = {"profile": PREFIX + "supported-profiles:v1", "profiles": list(context.supported_profiles)}
    supported_profiles(canonical(support), schemas=schemas, authority=context.authority_kind, expected_profiles=context.supported_profiles)
    _need(type(context.operation_id) is str and _UUID.fullmatch(context.operation_id)
          and type(context.issuance_authorization_id) is str and _UUID.fullmatch(context.issuance_authorization_id), "invalid_issuance_context")
    _timestamp(context.expires_at)
    expected = {"operation_id": context.operation_id, "issuance_authorization_id": context.issuance_authorization_id,
                "binding": binding, "authority_profile": PREFIX + context.authority_kind + ":v1", "scope": scope,
                "supported_profiles": support, "release_identity": release}
    _need(value == expected, "confirmation_context_mismatch")
    _scope(value["scope"], context.authority_kind)
    encoded = canonical(value)
    _need(len(encoded) <= 4096, "confirmation_size")
    return AuthorizedConfirmation(encoded, context, hashlib.sha256(encoded).hexdigest())


def _confirmation(value, context, original, scope, expires, schemas):
    _need(type(value) is dict, "invalid_confirmation")
    _scope(value.get("scope"), "preview")
    basis = IssuanceContext("preview", original["operation_id"], value.get("issuance_authorization_id"),
        canonical(original["binding"]), canonical(scope), tuple(original["supported_profiles"]["profiles"]),
        canonical(original["release_identity"]), expires)
    return validate_authorized_confirmation(canonical(value), context=basis, schemas=schemas)


def _remember(retained, current):
    retained = _Retained() if retained is None else retained
    _need(type(retained) is _Retained, "invalid_retained_context")
    expiry = retained.expires_at
    if current.expires_at is not None:
        if expiry is not None:
            _need(_timestamp(current.expires_at) <= _timestamp(expiry), "retained_deadline_extended")
        expiry = current.expires_at
    if retained.confirmation is not None and current.confirmation is not None:
        _need(current.confirmation == retained.confirmation
              and current.expires_at == retained.confirmation_expires_at, "retained_confirmation_changed")
    if retained.decision is not None and current.decision is not None:
        _need(current.decision == retained.decision
              and current.decision_reference == retained.decision_reference, "retained_decision_changed")
    if retained.entry_reference is not None and current.entry_reference is not None:
        _need(current.entry_reference == retained.entry_reference, "retained_entry_changed")
    if retained.authorized:
        _need(current.state in ("authorized", "unavailable"), "authorized_result_replaced")
    return _Retained(expiry,
        retained.confirmation if retained.confirmation is not None else current.confirmation,
        retained.confirmation_expires_at if retained.confirmation is not None else
            (current.expires_at if current.confirmation is not None else None),
        retained.decision if retained.decision is not None else current.decision,
        retained.decision_reference if retained.decision is not None else current.decision_reference,
        retained.entry_reference if retained.entry_reference is not None else current.entry_reference,
        retained.authorized or current.state == "authorized")


def validate_acquisition_response(raw: bytes, *, operation: str, context: AcquisitionContext,
                                  schemas: TextAnalysisSchemas, server_now: datetime,
                                  previous: ValidatedAcquisition | None = None) -> ValidatedAcquisition:
    """Validate protected X bytes without allocating or renewing hosted state."""
    _need(operation in ("prepare", "read", "submit"), "unsupported_operation")
    now = _clock(server_now)
    original, scope = _base(context, schemas)
    value = _object(raw, 16384)
    schemas.require("schemas/text-analysis-preview-acquisition/v1/" + operation + "-response.schema.json", value)
    _need(value["operation_id"] == original["operation_id"] and value["binding"] == original["binding"], "response_context_mismatch")
    _binding(value["binding"])
    state, expiry = value["state"], value.get("expires_at")
    confirmation = decision = confirmation_digest = decision_digest = decision_reference = None
    if expiry is not None:
        _need(_timestamp(expiry) <= _timestamp(context.operation_expires_at), "operation_deadline_extended")
    if "entry_reference" in value:
        _reference(value["entry_reference"])
    if state in ("confirmation-required", "authorized"):
        bound = _timestamp(context.confirmation_not_after or context.operation_expires_at)
        _need(_timestamp(expiry) <= min(bound, now + timedelta(seconds=300)), "confirmation_deadline_extended")
        checked = _confirmation(value["confirmation"], context, original, scope, expiry, schemas)
        confirmation, confirmation_digest = checked.confirmation_bytes, checked.confirmation_digest
        _need(value["confirmation_digest"] == confirmation_digest, "confirmation_digest_mismatch")
        if state == "confirmation-required":
            _scope({**value["decision"]["terms"], "seat_id": scope["seat_id"],
                    "seat_scope": scope["seat_scope"]}, "preview")
            decision = canonical(value["decision"])
            _need(len(decision) <= 4096, "decision_size")
            terms = {key: child for key, child in scope.items() if key not in ("seat_id", "seat_scope")}
            _need(value["decision"] == {"action": _ACTION, "expires_at": expiry, "terms": terms}, "decision_projection_mismatch")
            _need(_timestamp(expiry) > now, "decision_expired")
            decision_digest = hashlib.sha256(decision).hexdigest()
            _need(value["decision_digest"] == decision_digest, "decision_digest_mismatch")
            decision_reference = value["decision_reference"]
            _reference(decision_reference)
    retained = None
    if previous is not None:
        _need(type(previous) is ValidatedAcquisition and previous.context == context, "previous_context_mismatch")
        observed = validate_acquisition_response(previous.response_bytes, operation=previous.operation,
            context=context, schemas=schemas, server_now=previous.validated_at)
        retained = _remember(previous._retained, observed)
    current = ValidatedAcquisition(state, raw, context, operation, now, expiry, confirmation, confirmation_digest,
                                   decision, decision_digest, decision_reference, value.get("entry_reference"))
    return replace(current, _retained=_remember(retained, current))


def build_prepare_request(context: AcquisitionContext, *, schemas: TextAnalysisSchemas) -> bytes:
    original, _ = _base(context, schemas)
    return canonical(original)


def build_read_request(context: AcquisitionContext, *, schemas: TextAnalysisSchemas) -> bytes:
    original, _ = _base(context, schemas)
    return canonical({"profile": _X, "operation_id": original["operation_id"], "binding": original["binding"]})


def _refresh(result, schemas):
    _need(type(result) is ValidatedAcquisition, "invalid_projection")
    current = validate_acquisition_response(result.response_bytes, operation=result.operation, context=result.context,
                                             schemas=schemas, server_now=result.validated_at)
    return replace(current, _retained=_remember(result._retained, current))


def build_submit_request(result: ValidatedAcquisition, *, native_consent: NativeConsent,
                          schemas: TextAnalysisSchemas, server_now: datetime) -> bytes:
    checked = _refresh(result, schemas)
    _need(checked.state == "confirmation-required" and _timestamp(checked.expires_at) > _clock(server_now), "consent_unavailable")
    original, _ = _base(checked.context, schemas)
    expected = NativeConsent(original["operation_id"], checked.confirmation_digest, checked.decision_digest,
                             checked.decision_reference, _ACTION)
    _need(type(native_consent) is NativeConsent and native_consent == expected, "native_consent_mismatch")
    value = {"profile": _X, "operation_id": original["operation_id"], "binding": original["binding"],
             "confirmation_digest": checked.confirmation_digest, "decision_digest": checked.decision_digest,
             "decision_reference": checked.decision_reference, "consent": _ACTION}
    schemas.require_definition(_X_DEFINITIONS, "submitRequest", value)
    encoded = canonical(value)
    _need(len(encoded) <= 8192, "request_size")
    return encoded


def build_issuance_request(authorized: ValidatedAcquisition | AuthorizedConfirmation, *, authority_kind: str,
                            schemas: TextAnalysisSchemas, server_now: datetime) -> bytes:
    _need(authority_kind in ("license", "preview"), "unsupported_authority")
    now = _clock(server_now)
    if type(authorized) is ValidatedAcquisition:
        checked = _refresh(authorized, schemas)
        _need(checked.state == "authorized" and authority_kind == "preview", "issuance_unavailable")
        original, scope = _base(checked.context, schemas)
        value = _object(checked.confirmation, 4096)
        checked_confirmation = _confirmation(value, checked.context, original, scope, checked.expires_at, schemas)
    else:
        _need(type(authorized) is AuthorizedConfirmation, "issuance_unavailable")
        checked_confirmation = validate_authorized_confirmation(authorized.confirmation_bytes, context=authorized.context, schemas=schemas)
    basis = checked_confirmation.context
    _need(basis.authority_kind == authority_kind and _timestamp(basis.expires_at) > now, "issuance_expired")
    confirmation = _object(checked_confirmation.confirmation_bytes, 4096)
    value = {key: confirmation[key] for key in ("operation_id", "issuance_authorization_id", "binding",
                                               "authority_profile", "supported_profiles", "release_identity")}
    value.update(profile=_I, confirmation_digest=checked_confirmation.confirmation_digest)
    schemas.require_definition(_I_DEFINITIONS, authority_kind + "Request", value)
    encoded = canonical(value)
    _need(len(encoded) <= 8192, "request_size")
    return encoded
