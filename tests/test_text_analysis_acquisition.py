"""Native projection and phase-boundary tests."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
import rfc8785

from app.security.text_analysis_acquisition import (
    AcquisitionContext, IssuanceContext, NativeConsent,
    build_issuance_request, build_prepare_request, build_read_request, build_submit_request,
    validate_acquisition_response, validate_authorized_confirmation,
)
from app.security.text_analysis_contract import TextAnalysisSchemas

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1] / "contracts/text-analysis-authority-v1"
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
EXPIRY = "2026-10-25T00:05:00.000Z"


def encoded(value):
    return rfc8785.dumps(value)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


@pytest.fixture
def basis():
    cases = json.loads((ROOT / "proof-cases.json").read_bytes())
    original = next(case["request"] for case in cases if case["case_id"] == "XP-prepare")
    payloads = json.loads((ROOT / "signed-cases.json").read_bytes())
    payload = next(case["payload"] for case in payloads if case["case_id"] == "preview-accepted")
    fields = ("seat_id", "seat_scope", "capability", "licensed_major_version", "compatible_artifact_family",
              "update_rights", "fallback_major_versions", "execution_limit", "output_policy")
    scope = {name: payload[name] for name in fields}
    context = AcquisitionContext(encoded(original), encoded(scope), "2026-10-25T00:30:00.000Z")
    confirmation = {"operation_id": original["operation_id"],
                    "issuance_authorization_id": "019a1957-0000-7000-8000-000000000042",
                    "binding": original["binding"], "authority_profile": original["authority_profile"],
                    "scope": scope, "supported_profiles": original["supported_profiles"],
                    "release_identity": original["release_identity"]}
    decision = {"action": "acquire-text-analysis-preview", "expires_at": EXPIRY,
                "terms": {name: value for name, value in scope.items() if name not in ("seat_id", "seat_scope")}}
    response = {"profile": original["profile"], "operation_id": original["operation_id"], "binding": original["binding"],
                "state": "confirmation-required", "expires_at": EXPIRY, "confirmation": confirmation,
                "confirmation_digest": digest(confirmation), "decision": decision,
                "decision_digest": digest(decision), "decision_reference": "A" * 43}
    return context, response


def validated(basis, schemas, *, state="confirmation-required", now=NOW, previous=None):
    context, response = basis
    response = copy.deepcopy(response)
    response["state"] = state
    if state == "authorized":
        for name in ("decision", "decision_digest", "decision_reference"):
            response.pop(name)
    return validate_acquisition_response(encoded(response), operation="read", context=context,
                                         schemas=schemas, server_now=now, previous=previous)


def consent(result):
    return NativeConsent(json.loads(result.context.original_prepare)["operation_id"], result.confirmation_digest,
                         result.decision_digest, result.decision_reference, "acquire-text-analysis-preview")


def test_phase_requests_have_distinct_immutable_meanings(basis, schemas):
    context, _ = basis
    ready = validated(basis, schemas)
    accepted = validated(basis, schemas, state="authorized", previous=ready)
    requests = [build_prepare_request(context, schemas=schemas), build_read_request(context, schemas=schemas),
                build_submit_request(ready, native_consent=consent(ready), schemas=schemas, server_now=NOW),
                build_issuance_request(accepted, authority_kind="preview", schemas=schemas, server_now=NOW)]
    assert len(set(hashlib.sha256(raw).digest() for raw in requests)) == 4
    issuance = json.loads(requests[-1])
    assert issuance["confirmation_digest"] == ready.confirmation_digest
    assert "consent" not in issuance and "decision_reference" not in issuance
    assert json.loads(requests[2])["consent"] == "acquire-text-analysis-preview"
    assert "seat_id" not in json.loads(ready.decision)["terms"]
    assert "seat_id" in json.loads(accepted.confirmation)["scope"]


def test_builders_recheck_original_bytes_instead_of_public_fields(basis, schemas):
    ready = validated(basis, schemas)
    forged_state = replace(ready, state="authorized")
    with pytest.raises(ValueError):
        build_issuance_request(forged_state, authority_kind="preview", schemas=schemas, server_now=NOW)
    changed_digest = replace(ready, confirmation_digest="b" * 64)
    with pytest.raises(ValueError):
        build_submit_request(changed_digest, native_consent=consent(changed_digest), schemas=schemas, server_now=NOW)
    with pytest.raises(FrozenInstanceError):
        ready.state = "authorized"


@pytest.mark.parametrize("state", ["awaiting-authentication", "expired", "unavailable"])
def test_non_authority_states_cannot_build_consent_or_issuance(state, basis, schemas):
    context, response = basis
    value = {name: response[name] for name in ("profile", "operation_id", "binding")}
    value["state"] = state
    if state != "unavailable":
        value["expires_at"] = context.operation_expires_at
    result = validate_acquisition_response(encoded(value), operation="read", context=context, schemas=schemas, server_now=NOW)
    assert result.confirmation is None and result.decision is None
    with pytest.raises(ValueError):
        build_submit_request(result, native_consent=consent(result), schemas=schemas, server_now=NOW)
    with pytest.raises(ValueError):
        build_issuance_request(result, authority_kind="preview", schemas=schemas, server_now=NOW)


def test_historical_authorization_is_readable_without_new_issuance(basis, schemas):
    ready = validated(basis, schemas)
    accepted = validated(basis, schemas, state="authorized", previous=ready)
    historical = validated(basis, schemas, state="authorized", now=NOW + timedelta(hours=1), previous=accepted)
    assert historical.confirmation == accepted.confirmation and historical.expires_at == accepted.expires_at
    with pytest.raises(ValueError):
        build_issuance_request(historical, authority_kind="preview", schemas=schemas, server_now=NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        build_submit_request(ready, native_consent=consent(ready), schemas=schemas, server_now=NOW + timedelta(seconds=300))


def test_authorized_result_cannot_return_to_pending(basis, schemas):
    accepted = validated(basis, schemas, state="authorized")
    context, response = basis
    value = {name: response[name] for name in ("profile", "operation_id", "binding", "expires_at")}
    value["state"] = "expired"
    with pytest.raises(ValueError):
        validate_acquisition_response(encoded(value), operation="read", context=context, schemas=schemas,
                                       server_now=NOW + timedelta(hours=1), previous=accepted)


@pytest.mark.parametrize("kind", ["license", "preview"])
def test_independent_authorized_confirmation_supports_explicit_kind(kind, basis, schemas):
    _, response = basis
    value = copy.deepcopy(response["confirmation"])
    value["authority_profile"] = "urn:bridge-clean:text-analysis-" + kind + ":v1"
    if kind == "license":
        value["scope"].pop("execution_limit")
        value["scope"].pop("output_policy")
    context = IssuanceContext(kind, value["operation_id"], value["issuance_authorization_id"], encoded(value["binding"]),
                              encoded(value["scope"]), tuple(value["supported_profiles"]["profiles"]),
                              encoded(value["release_identity"]), EXPIRY)
    authorized = validate_authorized_confirmation(encoded(value), context=context, schemas=schemas)
    raw = build_issuance_request(authorized, authority_kind=kind, schemas=schemas, server_now=NOW)
    assert json.loads(raw)["confirmation_digest"] == digest(value)
    other = "license" if kind == "preview" else "preview"
    with pytest.raises(ValueError):
        build_issuance_request(authorized, authority_kind=other, schemas=schemas, server_now=NOW)


@pytest.mark.parametrize("fallbacks", [[2, 1], [1, 1], [3], [True], [1.0]])
def test_fallback_semantics_are_checked_before_canonical_encoding(fallbacks, basis, schemas):
    context, response = basis
    scope = json.loads(context.expected_scope)
    scope.update(licensed_major_version=3, fallback_major_versions=fallbacks)
    raw = json.dumps(scope).encode()
    context = replace(context, expected_scope=raw)
    response["confirmation"]["scope"] = scope
    response["confirmation_digest"] = digest(response["confirmation"])
    with pytest.raises(ValueError):
        validate_acquisition_response(json.dumps(response).encode(), operation="read", context=context,
                                       schemas=schemas, server_now=NOW)


def test_retries_preserve_entry_and_deadline(basis, schemas):
    context, response = basis
    initial = {name: response[name] for name in ("profile", "operation_id", "binding")}
    initial.update(state="awaiting-authentication", expires_at=context.operation_expires_at, entry_reference="A" * 43)
    retained = validate_acquisition_response(encoded(initial), operation="prepare", context=context, schemas=schemas, server_now=NOW)
    repeat = validate_acquisition_response(encoded(initial), operation="prepare", context=context, schemas=schemas,
                                           server_now=NOW + timedelta(minutes=1), previous=retained)
    assert repeat.entry_reference == retained.entry_reference
    initial["entry_reference"] = "B" * 42 + "A"
    with pytest.raises(ValueError):
        validate_acquisition_response(encoded(initial), operation="prepare", context=context, schemas=schemas,
                                       server_now=NOW, previous=retained)


def gap_projection(basis, previous, schemas, *, state="unavailable"):
    context, response = basis
    value = {name: response[name] for name in ("profile", "operation_id", "binding")}
    value["state"] = state
    if state != "unavailable":
        value["expires_at"] = EXPIRY
    return validate_acquisition_response(encoded(value), operation="read", context=context,
                                         schemas=schemas, server_now=NOW, previous=previous)


@pytest.mark.parametrize("gap_count", [1, 3, 7])
@pytest.mark.parametrize("replacement", ["extended", "different_authorization", "same_confirmation"])
def test_unavailable_cannot_erase_original_authorized_result(gap_count, replacement, basis, schemas):
    context, response = basis
    authorized = copy.deepcopy(response)
    authorized["state"] = "authorized"
    authorized["expires_at"] = "2026-10-25T00:04:00.000Z"
    for name in ("decision", "decision_digest", "decision_reference"):
        authorized.pop(name)
    retained = validate_acquisition_response(encoded(authorized), operation="read", context=context,
                                             schemas=schemas, server_now=NOW)
    for _ in range(gap_count):
        retained = gap_projection(basis, retained, schemas)
    assert retained.state == "unavailable" and retained.confirmation is None and retained.decision is None
    with pytest.raises(ValueError):
        build_issuance_request(retained, authority_kind="preview", schemas=schemas, server_now=NOW)
    with pytest.raises(ValueError):
        build_submit_request(retained, native_consent=consent(retained), schemas=schemas, server_now=NOW)
    candidate = copy.deepcopy(response)
    if replacement != "extended":
        candidate["expires_at"] = authorized["expires_at"]
        candidate["decision"]["expires_at"] = authorized["expires_at"]
        candidate["decision_digest"] = digest(candidate["decision"])
    if replacement != "same_confirmation":
        candidate["confirmation"]["issuance_authorization_id"] = "019a1957-0000-7000-8000-000000000099"
        candidate["confirmation_digest"] = digest(candidate["confirmation"])
    with pytest.raises(ValueError):
        validate_acquisition_response(encoded(candidate), operation="read", context=context,
                                       schemas=schemas, server_now=NOW, previous=retained)
    historical = validate_acquisition_response(encoded(authorized), operation="read", context=context,
        schemas=schemas, server_now=NOW + timedelta(hours=1), previous=retained)
    assert historical.state == "authorized" and historical.confirmation == encoded(authorized["confirmation"])
    with pytest.raises(ValueError):
        build_issuance_request(historical, authority_kind="preview", schemas=schemas, server_now=NOW + timedelta(hours=1))


@pytest.mark.parametrize("change", ["authorization", "decision_reference", "decision_expiry"])
def test_original_confirmation_and_decision_survive_non_authority_gaps(change, basis, schemas):
    context, response = basis
    retained = validated(basis, schemas)
    retained = gap_projection(basis, retained, schemas)
    retained = gap_projection(basis, retained, schemas, state="awaiting-authentication")
    retained = gap_projection(basis, retained, schemas, state="expired")
    retained = gap_projection(basis, retained, schemas)
    candidate = copy.deepcopy(response)
    if change == "authorization":
        candidate["confirmation"]["issuance_authorization_id"] = "019a1957-0000-7000-8000-000000000099"
        candidate["confirmation_digest"] = digest(candidate["confirmation"])
    elif change == "decision_reference":
        candidate["decision_reference"] = "B" * 42 + "A"
    else:
        candidate["expires_at"] = "2026-10-25T00:04:00.000Z"
        candidate["decision"]["expires_at"] = candidate["expires_at"]
        candidate["decision_digest"] = digest(candidate["decision"])
    with pytest.raises(ValueError):
        validate_acquisition_response(encoded(candidate), operation="read", context=context,
                                       schemas=schemas, server_now=NOW, previous=retained)


def test_original_entry_identity_survives_unavailable(basis, schemas):
    context, response = basis
    value = {name: response[name] for name in ("profile", "operation_id", "binding")}
    value.update(state="awaiting-authentication", expires_at=context.operation_expires_at, entry_reference="A" * 43)
    retained = validate_acquisition_response(encoded(value), operation="prepare", context=context, schemas=schemas, server_now=NOW)
    retained = gap_projection(basis, retained, schemas)
    value["entry_reference"] = "B" * 42 + "A"
    with pytest.raises(ValueError):
        validate_acquisition_response(encoded(value), operation="prepare", context=context,
                                       schemas=schemas, server_now=NOW, previous=retained)
