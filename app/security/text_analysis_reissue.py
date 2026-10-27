"""Native reissue requests and protected original-result validation."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
import time

from app.security.text_analysis_authority import TextAnalysisTrust, VerifiedTextAnalysisAuthority, verify_authority
from app.security.text_analysis_contract import (
    PREFIX, TextAnalysisContractError, TextAnalysisSchemas,
    canonical, reissue_object, reissue_supported_profiles, release_identity,
)
from app.security.text_analysis_proof import ProofContext, prepare_request, sign_request, validate_challenge

PROFILE = PREFIX + "license-reissue:v2"
PROOF_PROFILE = PREFIX + "license-reissue-proof:v2"
CHALLENGE_PATH = "/v2/text-analysis-license-reissue-proof-challenges"
PATH_PREFIX = "/v2/text-analysis-license-reissues:"
SCHEMA_ROOT = "schemas/license-reissue-text-analysis/v2/"
_DOCUMENT_LIMITS = {
    SCHEMA_ROOT + name + ".schema.json": limit for name, limit in (
        ("prepare-request", 8192), ("read-request", 8192), ("finalize-request", 8192),
        ("recover-request", 8192), ("prepare-response", 4096), ("read-response", 24576),
        ("confirmation", 8192), ("result", 24576), ("error", 4096))
}
_DOCUMENT_LIMITS.update({
    "schemas/license-reissue-text-analysis" + suffix + "/v2/" + name + ".schema.json": limit
    for suffix, entries in (
        ("-proof", (("challenge-request", 12288), ("challenge-response", 4096), ("error", 4096))),
        ("-authorization", (("authorize-request", 2048), ("authorize-response", 4096), ("error", 4096))),
        ("-supported-profiles", (("supported-profiles", 3072),)))
    for name, limit in entries
})


class TextAnalysisReissueError(TextAnalysisContractError):
    def __init__(self, code: str, *, retry_at: str | None = None):
        self.code, self.retry_at = code, retry_at
        super().__init__("Text-analysis reissue was refused")


def _need(ok, code):
    if not ok:
        raise TextAnalysisReissueError(code)


def _object(raw, maximum):
    try:
        return reissue_object(raw, maximum=maximum)
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise TextAnalysisReissueError("invalid_object") from None


def _time(value):
    _need(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", value), "invalid_timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise TextAnalysisReissueError("invalid_timestamp") from None


def _clock(value):
    _need(type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0), "invalid_clock")
    return value


def decode_reissue_document(raw: bytes, *, schema: str, schemas: TextAnalysisSchemas) -> dict:
    _need(schema in _DOCUMENT_LIMITS, "unsupported_schema")
    maximum = _DOCUMENT_LIMITS[schema]
    if type(raw) is bytes and len(raw) > maximum:
        raise TextAnalysisReissueError("invalid/413")
    try:
        value = reissue_object(raw, maximum=maximum)
        schemas.require(schema, value)
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError) as error:
        code = "invalid/413" if str(error) in ("json_size",) else "invalid/400"
        raise TextAnalysisReissueError(code) from None


@dataclass(frozen=True)
class ReissueContext:
    original_prepare: bytes = field(repr=False)
    public_key_jwk: bytes = field(repr=False)
    operation_expires_at: str | None = None

    @property
    def operation_id(self):
        return _object(self.original_prepare, 8192)["operation_id"]

    @property
    def binding(self):
        return _object(self.original_prepare, 8192)["binding"]


@dataclass(frozen=True)
class ReissueConfirmation:
    confirmation_bytes: bytes = field(repr=False)
    context: ReissueContext = field(repr=False)
    confirmation_digest: str
    expires_at: str


@dataclass(frozen=True)
class ValidatedReissueProjection:
    state: str
    response_bytes: bytes = field(repr=False)
    context: ReissueContext = field(repr=False)
    operation: str
    expires_at: str
    entry_reference: str | None = field(default=None, repr=False)
    confirmation: ReissueConfirmation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ValidatedReissueResult:
    response_bytes: bytes = field(repr=False)
    original_finalization_request: bytes = field(repr=False)
    confirmation: ReissueConfirmation = field(repr=False)
    authority: VerifiedTextAnalysisAuthority = field(repr=False)
    committed_at: str

    @property
    def operation_id(self):
        return self.confirmation.context.operation_id

    @property
    def license_id(self):
        return self.authority.payload["license_id"]

    @property
    def issuance_id(self):
        return self.authority.payload["issuance_id"]


def _proof_context(context, operation, raw):
    return ProofContext("reissue-v2-" + operation, **context.binding,
                        public_key_jwk=context.public_key_jwk, expected_request=raw)


def validate_context(context: ReissueContext, *, schemas: TextAnalysisSchemas) -> ReissueContext:
    _need(type(context) is ReissueContext, "invalid_context")
    original = _object(context.original_prepare, 8192)
    _need(canonical(original) == context.original_prepare, "noncanonical_context")
    try:
        reissue_supported_profiles(canonical(original.get("supported_profiles")), schemas=schemas)
        release_identity(canonical(original.get("release_identity")), schemas=schemas,
                         expected=original.get("release_identity"))
        prepare_request(context.original_prepare, context=_proof_context(context, "prepare", context.original_prepare), schemas=schemas)
    except (ValueError, KeyError, TypeError):
        raise TextAnalysisReissueError("invalid_context") from None
    if context.operation_expires_at is not None:
        _time(context.operation_expires_at)
    return context


def build_prepare_request(context: ReissueContext, *, schemas: TextAnalysisSchemas) -> bytes:
    return validate_context(context, schemas=schemas).original_prepare


def _inner(context, operation, schemas):
    validate_context(context, schemas=schemas)
    value = {"profile": PROFILE, "operation_id": context.operation_id, "binding": context.binding}
    encoded = canonical(value)
    prepare_request(encoded, context=_proof_context(context, operation, encoded), schemas=schemas)
    return encoded


def build_read_request(context: ReissueContext, *, schemas: TextAnalysisSchemas) -> bytes:
    return _inner(context, "read", schemas)


def build_recover_request(context: ReissueContext, *, schemas: TextAnalysisSchemas) -> bytes:
    return _inner(context, "recover", schemas)


def _scope(value):
    major, fallbacks = value["licensed_major_version"], value["fallback_major_versions"]
    _need(type(major) is int and type(value["update_rights"]) is bool
          and all(type(x) is int and 0 < x < major for x in fallbacks)
          and fallbacks == sorted(set(fallbacks)), "invalid_scope")


def validate_confirmation(raw: bytes, *, context: ReissueContext, schemas: TextAnalysisSchemas) -> ReissueConfirmation:
    validate_context(context, schemas=schemas)
    _need(context.operation_expires_at is not None, "operation_deadline_unavailable")
    value = decode_reissue_document(raw, schema=SCHEMA_ROOT + "confirmation.schema.json", schemas=schemas)
    original = _object(context.original_prepare, 8192)
    _need(all(value[k] == original[k] for k in ("profile", "operation_id", "binding", "supported_profiles", "release_identity")), "confirmation_context_mismatch")
    _need(_time(value["expires_at"]) <= _time(context.operation_expires_at), "deadline_extended")
    _scope(value["scope"])
    encoded = canonical(value)
    _need(len(encoded) <= 8192, "confirmation_size")
    return ReissueConfirmation(encoded, context, hashlib.sha256(encoded).hexdigest(), value["expires_at"])


def _confirmation(confirmation, schemas):
    _need(type(confirmation) is ReissueConfirmation, "confirmation_unavailable")
    checked = validate_confirmation(confirmation.confirmation_bytes, context=confirmation.context, schemas=schemas)
    _need(checked == confirmation, "confirmation_context_mismatch")
    return checked


def _finalize(confirmation, schemas):
    checked = _confirmation(confirmation, schemas)
    value = _object(checked.confirmation_bytes, 8192)
    request = {key: value[key] for key in ("profile", "operation_id", "binding", "reissue_authorization_id")}
    request["confirmation_digest"] = checked.confirmation_digest
    raw = canonical(request)
    prepare_request(raw, context=_proof_context(checked.context, "finalize", raw), schemas=schemas)
    return raw


def build_finalize_request(confirmation: ReissueConfirmation, *, schemas: TextAnalysisSchemas, now: datetime) -> bytes:
    raw = _finalize(confirmation, schemas)
    _need(_clock(now) < _time(confirmation.expires_at), "authorization_expired")
    return raw


def validate_reissue_projection(raw: bytes, *, operation: str, context: ReissueContext,
                                schemas: TextAnalysisSchemas, now: datetime,
                                previous: ValidatedReissueProjection | None = None,
                                confirmation: ReissueConfirmation | None = None) -> ValidatedReissueProjection:
    validate_context(context, schemas=schemas)
    _need(operation in ("prepare", "read"), "unsupported_operation")
    now = _clock(now)
    value = decode_reissue_document(raw, schema=SCHEMA_ROOT + operation + "-response.schema.json", schemas=schemas)
    _need(value["state"] != "completed", "result_context_unavailable")
    _need(value["operation_id"] == context.operation_id and value["binding"] == context.binding, "response_context_mismatch")
    expires = _time(value["expires_at"])
    if context.operation_expires_at is None:
        _need(operation == "prepare" and now < expires <= now + timedelta(seconds=1800), "operation_deadline_invalid")
        context = replace(context, operation_expires_at=value["expires_at"])
    else:
        _need(expires <= _time(context.operation_expires_at), "deadline_extended")
        if operation == "prepare":
            _need(value["expires_at"] == context.operation_expires_at, "preparation_changed")
    retained = _confirmation(confirmation, schemas) if confirmation is not None else None
    if retained is not None:
        _need(retained.context == context, "confirmation_context_mismatch")
    current = None
    if value["state"] == "authorized":
        current = validate_confirmation(canonical(value["confirmation"]), context=context, schemas=schemas)
        _need(value["confirmation_digest"] == current.confirmation_digest
              and value["expires_at"] == current.expires_at, "confirmation_digest_mismatch")
        _need(expires <= now + timedelta(seconds=300), "authorization_deadline_invalid")
        _need(retained is None or retained == current, "confirmation_changed")
    if previous is not None:
        _need(type(previous) is ValidatedReissueProjection and previous.context == context, "previous_context_mismatch")
        old_confirmation = _confirmation(previous.confirmation, schemas) if previous.confirmation is not None else None
        observed = validate_reissue_projection(previous.response_bytes, operation=previous.operation,
            context=context, schemas=schemas, now=now, confirmation=old_confirmation)
        _need(previous == observed, "previous_context_mismatch")
        if observed.entry_reference is not None and "entry_reference" in value:
            _need(value["entry_reference"] == observed.entry_reference, "preparation_changed")
        if observed.confirmation is not None:
            _need(current is None or current == observed.confirmation, "confirmation_changed")
            _need(value["state"] != "awaiting-authorization", "authorization_regressed")
            _need(retained is None or retained == observed.confirmation, "confirmation_changed")
            retained = observed.confirmation
    if retained is not None:
        _need(value["state"] != "awaiting-authorization", "authorization_regressed")
        _need(expires <= _time(retained.expires_at), "deadline_extended")
    return ValidatedReissueProjection(value["state"], raw, context, operation,
        value["expires_at"], value.get("entry_reference"), current or retained)


def validate_reissue_result(raw: bytes, *, confirmation: ReissueConfirmation,
                            original_finalization_request: bytes, authority_context: dict,
                            trust: TextAnalysisTrust, schemas: TextAnalysisSchemas,
                            previous_response: bytes | None = None) -> ValidatedReissueResult:
    checked = _confirmation(confirmation, schemas)
    expected_request = _finalize(checked, schemas)
    _need(type(original_finalization_request) is bytes and original_finalization_request == expected_request, "finalization_context_mismatch")
    value = decode_reissue_document(raw, schema=SCHEMA_ROOT + "result.schema.json", schemas=schemas)
    original = _object(checked.confirmation_bytes, 8192)
    _need(all(value[k] == original[k] for k in ("operation_id", "binding", "reissue_authorization_id", "prior_license_id", "prior_issuance_id", "authority_profile")), "result_context_mismatch")
    _need(value["finalization_request_digest"] == hashlib.sha256(expected_request).hexdigest(), "finalization_digest_mismatch")
    scope = original["scope"]
    _need(value["seat_id"] == scope["seat_id"] and value["seat_scope"] == scope["seat_scope"]
          and value["license_id"] != value["prior_license_id"]
          and value["issuance_id"] != value["prior_issuance_id"], "result_scope_mismatch")
    try:
        decision = verify_authority(value["signed_authority"], kind="license", context=authority_context, trust=trust, schemas=schemas)
    except ValueError:
        raise TextAnalysisReissueError("authority_context_invalid") from None
    _need(decision.accepted, "signed_authority_invalid")
    authority = decision.authority
    _need(all(authority.payload[k] == (tuple(v) if type(v) is list else v)
              for k, v in {**scope, **value["binding"]}.items())
          and all(value[k] == authority.payload[k] for k in ("license_id", "issuance_id")), "signed_scope_mismatch")
    committed = _time(value["committed_at"])
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    _need(committed - epoch >= timedelta(seconds=authority.payload["iat"]), "commit_before_issuance")
    if previous_response is not None:
        _need(type(previous_response) is bytes and 0 < len(previous_response) <= 24576
              and raw == previous_response, "result_bytes_changed")
    return ValidatedReissueResult(raw, expected_request, checked, authority, value["committed_at"])


_ERRORS = {
    "invalid": {400, 413}, "rate_limited": {429}, "temporarily_unavailable": {503},
    "proof_invalid": {401}, "request_conflict": {409}, "expired": {410},
    "unsupported_profile": {409}, "unsupported_release": {409},
    "authority_unavailable": {403}, "not_completed": {409},
}
_REASONS = {
    "challenge": {"invalid", "rate_limited", "temporarily_unavailable"},
    "prepare": {"invalid", "rate_limited", "temporarily_unavailable", "proof_invalid", "request_conflict", "expired", "unsupported_profile", "unsupported_release"},
    "read": {"invalid", "rate_limited", "temporarily_unavailable", "proof_invalid", "request_conflict"},
    "finalize": {"invalid", "rate_limited", "temporarily_unavailable", "proof_invalid", "request_conflict", "authority_unavailable", "unsupported_profile", "unsupported_release"},
    "recover": {"invalid", "rate_limited", "temporarily_unavailable", "proof_invalid", "request_conflict", "not_completed"},
}


class TextAnalysisReissueClient:
    """One explicit exchange per action, with new proof on every attempt."""
    def __init__(self, transport, key_authority, schemas, trust, *,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.transport, self.key_authority, self.schemas, self.trust = transport, key_authority, schemas, trust
        self.clock, self.monotonic = clock, monotonic

    def _response(self, response, action):
        _need(type(response.status_code) is int and type(response.body) is bytes
              and all(type(x) is str for x in (response.content_type, response.cache_control, response.content_encoding)), "invalid_transport_response")
        _need(response.content_type.split(";", 1)[0].strip().lower() == "application/json"
              and response.cache_control.strip().lower() == "no-store"
              and response.content_encoding.strip().lower() in ("", "identity"), "invalid_transport_response")
        statuses = {200, 201} if action == "finalize" else {200}
        if response.status_code not in statuses:
            value = _object(response.body, 4096)
            root = "schemas/license-reissue-text-analysis-proof/v2/" if action == "challenge" else SCHEMA_ROOT
            try:
                self.schemas.require(root + "error.schema.json", value)
            except ValueError:
                raise TextAnalysisReissueError("invalid_error") from None
            reason = value["reason"]
            _need(reason in _REASONS[action] and response.status_code in _ERRORS[reason], "invalid_error_status")
            if reason == "rate_limited":
                _time(value["retry_at"])
            raise TextAnalysisReissueError(reason, retry_at=value.get("retry_at"))
        return response.body

    def _exchange(self, context, action, raw, before_send, after_receive=None):
        validate_context(context, schemas=self.schemas)
        proof_context = _proof_context(context, action, raw)
        prepared = prepare_request(raw, context=proof_context, schemas=self.schemas)
        if before_send is not None:
            before_send()
        response = self.transport.request(CHALLENGE_PATH, prepared.challenge_request, maximum=4096, before_send=before_send)
        challenge = self._response(response, "challenge")
        accepted_at = self.monotonic()
        _need(type(accepted_at) in (int, float) and math.isfinite(accepted_at), "invalid_clock")
        issued = validate_challenge(challenge, prepared=prepared, schemas=self.schemas, now=self.clock())
        budget = (_time(issued["expires_at"]) - _clock(self.clock())).total_seconds()
        envelope = sign_request(raw, challenge, context=proof_context, key_authority=self.key_authority,
            schemas=self.schemas, clock=self.clock, monotonic=self.monotonic)
        if before_send is not None:
            before_send()
        reopened = self.key_authority.reopen_existing()
        _need(reopened.installation_key_id == context.binding["installation_key_id"]
              and reopened.installation_key_jkt == context.binding["installation_key_jkt"]
              and canonical(_object(reopened.public_key_jwk.encode(), 1024)) == canonical(_object(context.public_key_jwk, 1024)), "key_changed")
        maximum = 4096 if action == "prepare" else 24576
        def current():
            if before_send is not None:
                before_send()
            elapsed = self.monotonic() - accepted_at
            _need(math.isfinite(elapsed) and 0 <= elapsed < budget, "proof_deadline")
            validate_challenge(challenge, prepared=prepared, schemas=self.schemas, now=self.clock())
        response = self.transport.request(PATH_PREFIX + action, envelope,
            maximum=maximum, before_send=current)
        if after_receive is not None:
            after_receive()
        return self._response(response, action)

    def prepare(self, context, *, before_send=None):
        raw = self._exchange(context, "prepare", build_prepare_request(context, schemas=self.schemas), before_send, before_send)
        return validate_reissue_projection(raw, operation="prepare", context=context, schemas=self.schemas, now=self.clock())

    def read(self, context, *, confirmation=None, authority_context=None,
             original_finalization_request=None, previous_response=None, previous=None, before_send=None):
        raw = self._exchange(context, "read", build_read_request(context, schemas=self.schemas), before_send, before_send)
        if _object(raw, 24576).get("state") == "completed":
            return self._result(raw, context, confirmation, authority_context, original_finalization_request, previous_response)
        return validate_reissue_projection(raw, operation="read", context=context, schemas=self.schemas,
            now=self.clock(), previous=previous, confirmation=confirmation)

    def _result(self, raw, context, confirmation, authority_context, original, previous):
        checked = _confirmation(confirmation, self.schemas)
        _need(checked.context == context, "confirmation_context_mismatch")
        return validate_reissue_result(raw, confirmation=checked,
            original_finalization_request=_finalize(checked, self.schemas) if original is None else original,
            authority_context=authority_context, trust=self.trust, schemas=self.schemas, previous_response=previous)

    def finalize(self, confirmation, *, authority_context, original_finalization_request=None,
                 previous_response=None, before_send=None):
        confirmation = _confirmation(confirmation, self.schemas)
        committed = previous_response is not None
        if committed:
            self._result(previous_response, confirmation.context, confirmation, authority_context,
                         original_finalization_request, previous_response)
        raw_request = (_finalize(confirmation, self.schemas) if committed else
                       build_finalize_request(confirmation, schemas=self.schemas, now=self.clock()))
        _need(original_finalization_request is None or original_finalization_request == raw_request, "finalization_context_mismatch")
        def current():
            if before_send is not None:
                before_send()
            if not committed:
                build_finalize_request(confirmation, schemas=self.schemas, now=self.clock())
        raw = self._exchange(confirmation.context, "finalize", raw_request, current, before_send)
        return self._result(raw, confirmation.context, confirmation, authority_context, raw_request, previous_response)

    def recover(self, context, *, confirmation, authority_context,
                original_finalization_request=None, previous_response=None, before_send=None):
        raw = self._exchange(context, "recover", build_recover_request(context, schemas=self.schemas), before_send, before_send)
        return self._result(raw, context, confirmation, authority_context, original_finalization_request, previous_response)
