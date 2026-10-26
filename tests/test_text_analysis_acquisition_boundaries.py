"""Independent native acquisition scope, state and byte-boundary checks."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
import rfc8785

from app.security.text_analysis_acquisition import (
    AcquisitionContext, IssuanceContext, NativeConsent,
    build_issuance_request, build_prepare_request, build_read_request,
    build_submit_request, validate_acquisition_response, validate_authorized_confirmation,
)
from app.security.text_analysis_contract import TextAnalysisSchemas


pytestmark = [pytest.mark.ci_tier("fast")]
PREFIX = "urn:bridge-clean:text-analysis-"
ACTION = "acquire-text-analysis-preview"
OPERATION = "00000000-0000-7000-8000-000000000001"
AUTHORIZATION = "00000000-0000-7000-8000-000000000002"
OTHER = "00000000-0000-7000-8000-000000000003"
NOW = datetime(2026, 10, 25, 17, tzinfo=timezone.utc)
OPERATION_EXPIRY = "2026-10-25T17:30:00.000Z"
CONFIRMATION_EXPIRY = "2026-10-25T17:04:00.000Z"
ROOT = Path(__file__).resolve().parents[1] / "contracts"
PROFILES = tuple(json.loads((ROOT / "profiles/text-analysis-supported-profiles/v1/profile.json").read_bytes())["profiles"])


def encoded(value):
    return rfc8785.dumps(value)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def reference(value=b"boundary-reference"):
    return base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


def basis():
    binding = {"organization_id": "boundary-org", "installation_id": "boundary-install",
               "installation_key_id": "ik1." + "A" * 22, "installation_key_jkt": "A" * 43}
    scope = {"seat_id": "protected-seat", "seat_scope": "organization-installation-seat",
             "capability": "text-analysis", "licensed_major_version": 1,
             "compatible_artifact_family": "text-analysis-artifact", "update_rights": True,
             "fallback_major_versions": [], "execution_limit": 1,
             "output_policy": {"profile": PREFIX + "preview-output-policy:v1", "version": 1}}
    release = {"profile": PREFIX + "release-identity:v1", "product_id": "bridge-clean",
               "release_version": "2.4.0", "platform": "windows", "architecture": "x86_64",
               "artifact_sha256": "a" * 64, "contract_manifest_sha256": "b" * 64,
               "vector_manifest_sha256": "c" * 64, "trust_set_sha256": "d" * 64}
    prepare = {"profile": PREFIX + "preview-acquisition:v1", "operation_id": OPERATION,
               "binding": binding, "authority_profile": PREFIX + "preview:v1",
               "supported_profiles": {"profile": PREFIX + "supported-profiles:v1", "profiles": list(PROFILES)},
               "release_identity": release}
    return prepare, scope


def context(prepare=None, scope=None, *, confirmation_not_after=None):
    original, expected_scope = basis()
    return AcquisitionContext(encoded(original if prepare is None else prepare),
        encoded(expected_scope if scope is None else scope), OPERATION_EXPIRY,
        confirmation_not_after=confirmation_not_after)


def response(state="confirmation-required", *, entry=False):
    prepare, scope = basis()
    value = {key: copy.deepcopy(prepare[key]) for key in ("profile", "operation_id", "binding")}
    value["state"] = state
    if state == "awaiting-authentication":
        value["expires_at"] = OPERATION_EXPIRY
        if entry:
            value["entry_reference"] = reference(b"navigation")
    elif state in ("confirmation-required", "authorized"):
        confirmation = {key: copy.deepcopy(prepare[key]) for key in (
            "operation_id", "binding", "authority_profile", "supported_profiles", "release_identity")}
        confirmation.update(issuance_authorization_id=AUTHORIZATION, scope=scope)
        value.update(expires_at=CONFIRMATION_EXPIRY, confirmation=confirmation,
                     confirmation_digest=digest(confirmation))
        if state == "confirmation-required":
            value["decision"] = {"action": ACTION, "expires_at": CONFIRMATION_EXPIRY,
                "terms": {key: copy.deepcopy(child) for key, child in scope.items()
                          if key not in ("seat_id", "seat_scope")}}
            value.update(decision_reference=reference(), decision_digest=digest(value["decision"]))
    elif state == "expired":
        value["expires_at"] = CONFIRMATION_EXPIRY
    return value


def refresh_digests(value):
    if "confirmation" in value:
        value["confirmation_digest"] = digest(value["confirmation"])
    if "decision" in value:
        value["decision_digest"] = digest(value["decision"])


def observe(value, schemas, *, operation="read", original=None, now=NOW, previous=None):
    raw = value if type(value) is bytes else encoded(value)
    return validate_acquisition_response(raw, operation=operation, context=original or context(),
                                        schemas=schemas, server_now=now, previous=previous)


def consent(value):
    return NativeConsent(OPERATION, value["confirmation_digest"], value["decision_digest"],
                         value["decision_reference"], ACTION)


@pytest.mark.parametrize("operation", ["prepare", "read", "submit"])
@pytest.mark.parametrize("state", ["awaiting-authentication", "confirmation-required", "authorized", "expired", "unavailable"])
def test_each_closed_projection_has_only_its_native_transition(operation, state, schemas):
    wire = response(state)
    observed = observe(wire, schemas, operation=operation)
    assert observed.state == state and observed.response_bytes == encoded(wire)
    assert (observed.confirmation is not None) == (state in ("confirmation-required", "authorized"))
    assert (observed.decision is not None) == (state == "confirmation-required")
    assert observed.entry_reference is None
    if state != "authorized":
        with pytest.raises(ValueError):
            build_issuance_request(observed, authority_kind="preview", schemas=schemas, server_now=NOW)
    if state != "confirmation-required":
        with pytest.raises(ValueError):
            build_submit_request(observed, native_consent=consent(response()), schemas=schemas, server_now=NOW)


def test_navigation_reference_is_prepare_only_and_does_not_supply_consent(schemas):
    wire = response("awaiting-authentication", entry=True)
    observed = observe(wire, schemas, operation="prepare")
    assert observed.entry_reference == wire["entry_reference"]
    for operation in ("read", "submit"):
        with pytest.raises(ValueError):
            observe(wire, schemas, operation=operation)
    with pytest.raises(ValueError):
        build_submit_request(observed, native_consent=consent(response()), schemas=schemas, server_now=NOW)


def test_builders_keep_phase_digests_and_seat_out_of_native_consent(schemas):
    original, _ = basis()
    prepare = build_prepare_request(context(), schemas=schemas)
    read = build_read_request(context(), schemas=schemas)
    wire = response()
    checked = observe(wire, schemas)
    submit = build_submit_request(checked, native_consent=consent(wire), schemas=schemas, server_now=NOW)
    issued = build_issuance_request(observe(response("authorized"), schemas), authority_kind="preview",
                                    schemas=schemas, server_now=NOW)
    assert prepare == encoded(original)
    assert json.loads(read) == {key: original[key] for key in ("profile", "operation_id", "binding")}
    submitted, issuance = json.loads(submit), json.loads(issued)
    assert submitted["consent"] == ACTION
    assert submitted["confirmation_digest"] == wire["confirmation_digest"]
    assert issuance["profile"] == PREFIX + "authority-issuance:v1"
    assert issuance["issuance_authorization_id"] == AUTHORIZATION
    assert issuance["confirmation_digest"] == wire["confirmation_digest"]
    assert len({hashlib.sha256(raw).hexdigest() for raw in (prepare, submit, issued)}) == 3
    assert "seat_id" not in submitted and "seat_scope" not in submitted
    assert "seat_id" not in wire["decision"]["terms"] and "seat_scope" not in wire["decision"]["terms"]


@pytest.mark.parametrize("field", ["operation_id", "organization_id", "installation_id", "installation_key_id", "installation_key_jkt"])
def test_outer_scope_cannot_replace_original_binding(field, schemas):
    wire = response()
    if field == "operation_id":
        wire[field] = OTHER
    else:
        wire["binding"][field] = {"installation_key_id": "ik1." + "B" * 22,
                                  "installation_key_jkt": "B" * 42 + "A"}.get(field, "other")
    with pytest.raises(ValueError):
        observe(wire, schemas)


@pytest.mark.parametrize("field", ["operation_id", "binding", "authority_profile", "seat_id", "release_identity", "supported_profiles"])
def test_rehashed_confirmation_still_requires_independent_original_context(field, schemas):
    wire = response()
    confirmation = wire["confirmation"]
    if field == "operation_id":
        confirmation[field] = OTHER
    elif field == "binding":
        confirmation[field]["installation_id"] = "other-installation"
    elif field == "authority_profile":
        confirmation[field] = PREFIX + "license:v1"
    elif field == "seat_id":
        confirmation["scope"][field] = "other-seat"
    elif field == "release_identity":
        confirmation[field]["artifact_sha256"] = "e" * 64
    else:
        confirmation[field]["profiles"].remove(PREFIX + "license:v1")
    refresh_digests(wire)
    with pytest.raises(ValueError):
        observe(wire, schemas)


@pytest.mark.parametrize("mutation", ["confirmation_digest", "decision_digest", "action", "terms", "policy", "seat_display", "deadline"])
def test_confirmation_and_seat_free_decision_are_bound_together(mutation, schemas):
    wire = response()
    if mutation.endswith("digest"):
        wire[mutation] = "0" * 64
    else:
        if mutation == "action":
            wire["decision"]["action"] = "other-action"
        elif mutation == "terms":
            wire["decision"]["terms"]["update_rights"] = False
        elif mutation == "policy":
            wire["decision"]["terms"]["output_policy"]["version"] = 2
        elif mutation == "seat_display":
            wire["decision"]["terms"]["seat_id"] = "protected-seat"
        else:
            wire["decision"]["expires_at"] = "2026-10-25T17:03:00.000Z"
        refresh_digests(wire)
    with pytest.raises(ValueError):
        observe(wire, schemas)


@pytest.mark.parametrize("field", ["operation_id", "confirmation_digest", "decision_digest", "decision_reference", "action"])
def test_native_consent_must_bind_the_exact_validated_decision(field, schemas):
    wire = response()
    checked = observe(wire, schemas)
    values = dict(operation_id=OPERATION, confirmation_digest=wire["confirmation_digest"],
                  decision_digest=wire["decision_digest"], decision_reference=wire["decision_reference"], action=ACTION)
    values[field] = {"operation_id": OTHER, "action": "other-action",
                     "decision_reference": reference(b"another-decision")}.get(field, "0" * 64)
    with pytest.raises(ValueError):
        build_submit_request(checked, native_consent=NativeConsent(**values), schemas=schemas, server_now=NOW)


@pytest.mark.parametrize("missing", [None, False, True, ACTION])
def test_browser_or_candidate_state_does_not_manufacture_native_consent(missing, schemas):
    with pytest.raises(ValueError):
        build_submit_request(observe(response(), schemas), native_consent=missing, schemas=schemas, server_now=NOW)


def test_uuid_timestamp_is_identity_and_expired_authorized_read_does_not_renew(schemas):
    historical_now = NOW + timedelta(days=90)
    checked = observe(response("authorized"), schemas, now=historical_now)
    assert checked.state == "authorized" and checked.expires_at == CONFIRMATION_EXPIRY
    with pytest.raises(ValueError):
        build_issuance_request(checked, authority_kind="preview", schemas=schemas, server_now=historical_now)
    prepare = build_prepare_request(context(), schemas=schemas)
    assert json.loads(prepare)["operation_id"] == OPERATION
    with pytest.raises(ValueError):
        build_submit_request(observe(response(), schemas), native_consent=consent(response()),
                             schemas=schemas, server_now=NOW + timedelta(minutes=4))


def test_confirmation_cannot_exceed_original_or_shorter_deadline(schemas):
    with pytest.raises(ValueError):
        observe(response(), schemas, original=context(confirmation_not_after="2026-10-25T17:03:00.000Z"))
    for expiry in ("2026-10-25T17:05:00.001Z", "2026-10-25T17:31:00.000Z"):
        wire = response()
        wire["expires_at"] = wire["decision"]["expires_at"] = expiry
        refresh_digests(wire)
        with pytest.raises(ValueError):
            observe(wire, schemas)


@pytest.mark.parametrize("mutation", ["deadline", "authorization", "reference"])
def test_repeated_observation_cannot_renew_original_accepted_meaning(mutation, schemas):
    wire = response()
    previous = observe(wire, schemas)
    if mutation == "deadline":
        wire["expires_at"] = wire["decision"]["expires_at"] = "2026-10-25T17:05:00.000Z"
    elif mutation == "authorization":
        wire["confirmation"]["issuance_authorization_id"] = OTHER
    else:
        wire["decision_reference"] = reference(b"replacement")
    refresh_digests(wire)
    with pytest.raises(ValueError):
        observe(wire, schemas, previous=previous)


@pytest.mark.parametrize("state,extra", [("unavailable", "expires_at"), ("expired", "confirmation"),
    ("awaiting-authentication", "decision"), ("authorized", "decision_reference"), ("authorized", "signed_authority")])
def test_projection_fields_are_not_passed_through_between_states(state, extra, schemas):
    wire = response(state)
    wire[extra] = CONFIRMATION_EXPIRY if extra == "expires_at" else "injected"
    with pytest.raises(ValueError):
        observe(wire, schemas)


@pytest.mark.parametrize("field", ["entry_reference", "decision_reference"])
def test_reference_encoding_must_be_canonical_raw32(field, schemas):
    wire = response("awaiting-authentication", entry=True) if field == "entry_reference" else response()
    wire[field] = wire[field][:-1] + "B"
    with pytest.raises(ValueError):
        observe(wire, schemas, operation="prepare" if field == "entry_reference" else "read")


@pytest.mark.parametrize("stamp", ["2026-02-29T17:04:00.000Z", "2026-10-25T17:04:00Z",
    "2026-10-25T17:04:00.000+00:00", "2026-10-25T24:04:00.000Z", "2026-10-25T17:04:60.000Z"])
def test_deadline_calendar_and_wire_shape_are_strict(stamp, schemas):
    wire = response("awaiting-authentication")
    wire["expires_at"] = stamp
    with pytest.raises(ValueError):
        observe(wire, schemas)


@pytest.mark.parametrize("field", ["licensed_major_version", "execution_limit", "output_policy"])
@pytest.mark.parametrize("number", [True, 1.0])
def test_integer_scope_fields_do_not_accept_boolean_or_float_equivalence(field, number, schemas):
    wire = response()
    for scope in (wire["confirmation"]["scope"], wire["decision"]["terms"]):
        if field == "output_policy":
            scope[field]["version"] = number
        else:
            scope[field] = number
    refresh_digests(wire)
    raw = json.dumps(wire, separators=(",", ":")).encode()
    with pytest.raises(ValueError):
        observe(raw, schemas)


def test_duplicate_nonfinite_depth_and_oversize_input(schemas):
    wire = response()
    raw = encoded(wire)
    duplicate = raw.replace(b'"operation_id":', b'"operation_id":"' + OTHER.encode() + b'","operation_id":', 1)
    nested = raw.replace(b'"seat_id":"protected-seat"', b'"seat_id":"protected-seat","seat_id":"protected-seat"')
    for invalid in (b"", b"\xff", b"\xef\xbb\xbf" + raw, duplicate, nested, b" " * 16385,
                    b"[" * 32 + b"0" + b"]" * 32,
                    raw.replace(b'"licensed_major_version":1', b'"licensed_major_version":NaN'),
                    raw.replace(b'"licensed_major_version":1', b'"licensed_major_version":1e999')):
        with pytest.raises(ValueError):
            observe(invalid, schemas)


def test_response_utf8_ceiling_is_inclusive_and_preserves_received_bytes(schemas):
    raw = encoded(response("authorized"))
    boundary = raw + b" " * (16384 - len(raw))
    assert observe(boundary, schemas).response_bytes == boundary
    for invalid in (boundary + b" ", bytearray(raw), b"null", b"[]"):
        with pytest.raises(ValueError):
            validate_acquisition_response(invalid, operation="read", context=context(),
                                          schemas=schemas, server_now=NOW)


@pytest.mark.parametrize("field", ["contract_manifest_sha256", "vector_manifest_sha256", "trust_set_sha256", "release_version"])
def test_each_retained_release_pin_is_independently_compared(field, schemas):
    wire = response()
    wire["confirmation"]["release_identity"][field] = "e" * 64 if field.endswith("sha256") else "2.4.1"
    refresh_digests(wire)
    with pytest.raises(ValueError):
        observe(wire, schemas)


def test_unknown_state_and_invalid_original_context_remain_unusable(schemas):
    wire = response("unavailable")
    wire["state"] = "ready"
    with pytest.raises(ValueError):
        observe(wire, schemas)
    original, scope = basis()
    for invalid in (b" " * 8193, encoded(dict(original, session="not-a-native-field"))):
        retained = AcquisitionContext(invalid, encoded(scope), OPERATION_EXPIRY)
        with pytest.raises(ValueError):
            build_prepare_request(retained, schemas=schemas)
    with pytest.raises(ValueError):
        observe(response(), schemas, now=NOW.replace(tzinfo=None))


def test_validated_bytes_are_detached_from_mutable_candidate(schemas):
    wire = response()
    checked = observe(wire, schemas)
    original_confirmation = encoded(wire["confirmation"])
    wire["confirmation"]["scope"]["seat_id"] = "changed"
    wire["decision"]["terms"]["update_rights"] = False
    assert checked.confirmation == original_confirmation
    assert json.loads(checked.decision)["terms"]["update_rights"] is True


@pytest.mark.parametrize("kind", ["license", "preview"])
def test_generic_authorized_confirmation_requires_exact_kind_and_protected_context(kind, schemas):
    prepare, scope = basis()
    if kind == "license":
        scope.pop("execution_limit")
        scope.pop("output_policy")
    confirmation = {"operation_id": OPERATION, "issuance_authorization_id": AUTHORIZATION,
        "binding": prepare["binding"], "authority_profile": PREFIX + kind + ":v1", "scope": scope,
        "supported_profiles": prepare["supported_profiles"], "release_identity": prepare["release_identity"]}
    retained = IssuanceContext(kind, OPERATION, AUTHORIZATION, encoded(prepare["binding"]), encoded(scope),
                               PROFILES, encoded(prepare["release_identity"]), CONFIRMATION_EXPIRY)
    checked = validate_authorized_confirmation(encoded(confirmation), context=retained, schemas=schemas)
    assert checked.confirmation_digest == digest(confirmation)
    request = json.loads(build_issuance_request(checked, authority_kind=kind, schemas=schemas, server_now=NOW))
    assert request["authority_profile"] == PREFIX + kind + ":v1"
    assert request["confirmation_digest"] == digest(confirmation)
    with pytest.raises(ValueError):
        build_issuance_request(checked, authority_kind="preview" if kind == "license" else "license",
                               schemas=schemas, server_now=NOW)
    raw = encoded(confirmation)
    boundary = raw + b" " * (4096 - len(raw))
    assert validate_authorized_confirmation(boundary, context=retained, schemas=schemas).confirmation_bytes == raw
    with pytest.raises(ValueError):
        validate_authorized_confirmation(boundary + b" ", context=retained, schemas=schemas)
    confirmation["scope"]["seat_id"] = "other-seat"
    with pytest.raises(ValueError):
        validate_authorized_confirmation(encoded(confirmation), context=retained, schemas=schemas)
