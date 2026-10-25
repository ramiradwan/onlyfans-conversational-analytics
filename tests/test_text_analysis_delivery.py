"""Original-byte text-analysis delivery validation without hosted operations."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

from app.security.text_analysis_authority import TextAnalysisConfigurationError, fixture_trust
from app.security.text_analysis_contract import (
    PREFIX, TextAnalysisContractError, TextAnalysisSchemas, canonical,
)
from app.security.text_analysis_delivery import (
    ISSUANCE_REQUEST_MAX_BYTES, RESPONSE_MAX_BYTES,
    TextAnalysisDeliveryError, validate_delivery_response,
)


pytestmark = [pytest.mark.ci_tier("fast")]
ROOT = Path(__file__).resolve().parents[1] / "contracts" / "text-analysis-authority-v1"
SIGNED = json.loads((ROOT / "signed-cases.json").read_bytes())
SCHEMAS = json.loads((ROOT / "schema-cases.json").read_bytes())
OTHER_ID = "019a1957-0000-7000-8000-000000000099"


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


@pytest.fixture(scope="module")
def trust():
    return fixture_trust(environment="test")


def example(kind):
    signed = next(case for case in SIGNED if case["case_id"] == kind + "-accepted")
    schema = f"schemas/text-analysis-authority-issuance/v1/{kind}-request.schema.json"
    original = copy.deepcopy(next(case["value"]["request"] for case in SCHEMAS
                                  if case["schema"] == schema and case["valid"]))
    context = copy.deepcopy(signed["context"])
    original["binding"] = {name: context["expected_" + name] for name in (
        "organization_id", "installation_id", "installation_key_id", "installation_key_jkt")}
    original["release_identity"]["release_version"] = "1.0.0"
    identifier = "license_id" if kind == "license" else "permit_id"
    response = {"profile": PREFIX + "authority-delivery:v1", "state": "completed",
        "operation_id": original["operation_id"],
        "issuance_authorization_id": original["issuance_authorization_id"],
        "issuance_request_digest": hashlib.sha256(canonical(original)).hexdigest(),
        "binding": copy.deepcopy(original["binding"]), "seat_id": "fixture-seat",
        "seat_scope": "organization-installation-seat", "authority_profile": original["authority_profile"],
        "issuance_id": signed["payload"]["issuance_id"], identifier: signed["payload"][identifier],
        "signed_authority": signed["token"], "committed_at": "2026-10-25T00:00:03.000Z"}
    return original, response, context


def validate(original, response, context, schemas, trust, **kwargs):
    return validate_delivery_response(canonical(response), original_issuance_request=canonical(original),
        expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust, **kwargs)


@pytest.mark.parametrize("kind", ["license", "preview"])
def test_exact_signed_delivery_and_original_release_are_retained(kind, schemas, trust):
    original, response, context = example(kind)
    result = validate(original, response, context, schemas, trust)
    assert result.response_bytes == canonical(response)
    assert result.original_issuance_request == canonical(original)
    assert result.issuance_request_digest == response["issuance_request_digest"]
    assert result.authority.compact_jws == response["signed_authority"]
    assert result.authority.kind == kind and result.authority.fixture_only
    assert result.original_supported_profiles == tuple(original["supported_profiles"]["profiles"])
    assert result.original_release_identity == original["release_identity"]
    assert result.issued_at == 1792886400
    assert result.committed_at == "2026-10-25T00:00:03.000Z"
    assert validate(original, response, context, schemas, trust,
                    previous_response=result.response_bytes) == result
    original["release_identity"]["artifact_sha256"] = "a" * 64
    original["supported_profiles"]["profiles"].clear()
    response["signed_authority"] = "changed"
    context["expected_subject"] = "changed"
    assert result.original_release_identity["artifact_sha256"] != "a" * 64
    assert len(result.original_supported_profiles) == 12
    assert result.authority.compact_jws != "changed"
    with pytest.raises(TypeError):
        result.original_release_identity["artifact_sha256"] = "a" * 64
    with pytest.raises(FrozenInstanceError):
        result.committed_at = "changed"


@pytest.mark.parametrize("field", ["operation_id", "issuance_authorization_id", "issuance_request_digest"])
def test_original_operation_coordinates_are_independent(field, schemas, trust):
    original, response, context = example("license")
    response[field] = "a" * 64 if field.endswith("digest") else OTHER_ID
    with pytest.raises(TextAnalysisDeliveryError, match="mismatch"):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("field,value", [
    ("organization_id", "other-organization"), ("installation_id", "other-installation"),
    ("installation_key_id", "ik1." + "A" * 22), ("installation_key_jkt", "A" * 43),
])
def test_delivery_binding_cannot_select_another_registration(field, value, schemas, trust):
    original, response, context = example("preview")
    response["binding"][field] = value
    with pytest.raises(TextAnalysisDeliveryError, match="original_mismatch"):
        validate(original, response, context, schemas, trust)
    response["binding"][field] = original["binding"][field]
    context["expected_" + field] = value
    with pytest.raises(TextAnalysisConfigurationError, match="context_mismatch"):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("kind,field", [
    ("license", "license_id"), ("license", "issuance_id"),
    ("preview", "permit_id"), ("preview", "issuance_id"),
])
def test_response_identifiers_must_equal_verified_token(kind, field, schemas, trust):
    original, response, context = example(kind)
    response[field] = OTHER_ID
    with pytest.raises(TextAnalysisDeliveryError, match="signed_scope_mismatch"):
        validate(original, response, context, schemas, trust)


def test_seat_is_not_taken_from_response_or_signed_payload(schemas, trust):
    original, response, context = example("license")
    response["seat_id"] = "other-seat"
    with pytest.raises(TextAnalysisDeliveryError, match="seat_mismatch"):
        validate(original, response, context, schemas, trust)
    response["seat_id"] = "fixture-seat"
    with pytest.raises(TextAnalysisConfigurationError, match="seat_context_mismatch"):
        validate_delivery_response(canonical(response), original_issuance_request=canonical(original),
            expected_seat_id="other-seat", authority_context=context, schemas=schemas, trust=trust)
    context["expected_subject"] = context["expected_subject"].replace("fixture-seat", "other-seat")
    with pytest.raises(TextAnalysisConfigurationError, match="seat_context_mismatch"):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("kind", ["license", "preview"])
def test_cross_kind_response_and_token_substitution(kind, schemas, trust):
    original, response, context = example(kind)
    _, other_response, _ = example("preview" if kind == "license" else "license")
    with pytest.raises(TextAnalysisDeliveryError, match="original_mismatch"):
        validate(original, other_response, context, schemas, trust)
    response["signed_authority"] = other_response["signed_authority"]
    with pytest.raises(TextAnalysisDeliveryError, match="signed_authority_typ_mismatch"):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("mutation", ["signature", "payload", "empty"])
def test_token_bytes_are_verified_without_reconstruction(mutation, schemas, trust):
    original, response, context = example("license")
    token = response["signed_authority"].split(".")
    if mutation == "empty":
        response["signed_authority"] = ""
    else:
        index = 2 if mutation == "signature" else 1
        token[index] = ("A" if token[index][0] != "A" else "B") + token[index][1:]
        response["signed_authority"] = ".".join(token)
    with pytest.raises(TextAnalysisContractError):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("mutation", ["unknown", "nested_unknown", "missing", "both_identifiers", "state"])
def test_response_is_recursively_closed(mutation, schemas, trust):
    original, response, context = example("preview")
    if mutation == "unknown":
        response["metadata"] = {}
    elif mutation == "nested_unknown":
        response["binding"]["seat_id"] = "fixture-seat"
    elif mutation == "missing":
        del response["issuance_id"]
    elif mutation == "both_identifiers":
        response["license_id"] = response["permit_id"]
    else:
        response["state"] = "pending"
    with pytest.raises(TextAnalysisContractError, match="schema_invalid"):
        validate(original, response, context, schemas, trust)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_members_are_rejected_even_with_equal_values(nested, schemas, trust):
    original, response, context = example("license")
    raw = canonical(response)
    name = b'"organization_id"' if nested else b'"operation_id"'
    value = canonical(response["binding"]["organization_id"] if nested else response["operation_id"])
    raw = raw.replace(name + b":" + value, name + b":" + value + b"," + name + b":" + value, 1)
    with pytest.raises(TextAnalysisDeliveryError, match="invalid_delivery_bytes"):
        validate_delivery_response(raw, original_issuance_request=canonical(original),
            expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)


@pytest.mark.parametrize("number", [b"true", b"1.0", b"9007199254740993", b"NaN", b"Infinity", b"1e999"])
def test_numeric_and_nonfinite_coordinates_are_rejected(number, schemas, trust):
    original, response, context = example("license")
    raw = canonical(response).replace(canonical(response["operation_id"]), number)
    with pytest.raises(TextAnalysisContractError):
        validate_delivery_response(raw, original_issuance_request=canonical(original),
            expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)


@pytest.mark.parametrize("stamp", ["2026-02-30T00:00:03.000Z", "2026-10-25T24:00:03.000Z",
    "2026-10-25T00:00:03Z", "2026-10-25T00:00:03.000+00:00", "2026-10-25T00:00:60.000Z", 1792886400])
def test_committed_timestamp_has_exact_calendar_and_transport_shape(stamp, schemas, trust):
    original, response, context = example("preview")
    response["committed_at"] = stamp
    with pytest.raises(TextAnalysisContractError, match="schema_invalid"):
        validate(original, response, context, schemas, trust)


def test_utf8_response_limit_and_noncanonical_transport_bytes(schemas, trust):
    original, response, context = example("license")
    raw = canonical(response)
    boundary = raw + b" " * (RESPONSE_MAX_BYTES - len(raw))
    result = validate_delivery_response(boundary, original_issuance_request=canonical(original),
        expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)
    assert result.response_bytes == boundary
    for candidate in (boundary + b" ", b"", b"\xff", b"\xef\xbb\xbf" + raw, bytearray(raw)):
        with pytest.raises(TextAnalysisDeliveryError):
            validate_delivery_response(candidate, original_issuance_request=canonical(original),
                expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)
    with pytest.raises(TextAnalysisDeliveryError):
        validate_delivery_response(raw, original_issuance_request=b" " * (ISSUANCE_REQUEST_MAX_BYTES + 1),
            expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)


@pytest.mark.parametrize("mutation", ["timestamp", "whitespace"])
def test_recovery_keeps_exact_original_result_bytes(mutation, schemas, trust):
    original, response, context = example("preview")
    previous = canonical(response)
    if mutation == "timestamp":
        response["committed_at"] = "2026-10-25T00:00:04.000Z"
        raw = canonical(response)
    else:
        raw = previous + b" "
    with pytest.raises(TextAnalysisDeliveryError, match="recovery_bytes_mismatch"):
        validate_delivery_response(raw, original_issuance_request=canonical(original),
            expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust,
            previous_response=previous)


@pytest.mark.parametrize("field", ["supported_profiles", "release_identity", "confirmation_digest"])
def test_changed_original_source_statements_do_not_match_retained_digest(field, schemas, trust):
    original, response, context = example("license")
    if field == "supported_profiles":
        original[field]["profiles"].remove(PREFIX + "preview:v1")
    elif field == "release_identity":
        original[field]["artifact_sha256"] = "a" * 64
    else:
        original[field] = "a" * 64
    with pytest.raises(TextAnalysisDeliveryError, match="request_digest_mismatch"):
        validate(original, response, context, schemas, trust)


def test_retained_original_is_closed_canonical_and_operation_supported(schemas, trust):
    original, response, context = example("preview")
    for raw in (b" " + canonical(original), canonical(dict(original, metadata={})),
                canonical(dict(original, authority_profile="urn:bridge-clean:capability-license:v1")),
                canonical(dict(original, authority_profile=[]))):
        with pytest.raises(TextAnalysisContractError):
            validate_delivery_response(canonical(response), original_issuance_request=raw,
                expected_seat_id="fixture-seat", authority_context=context, schemas=schemas, trust=trust)
    original["supported_profiles"]["profiles"].remove(PREFIX + "preview-acquisition:v1")
    with pytest.raises(TextAnalysisContractError, match="unsupported_profile"):
        validate(original, response, context, schemas, trust)
