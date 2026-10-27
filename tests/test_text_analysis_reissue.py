"""Native reissue wire and original-context conformance."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.security.text_analysis_authority import TextAnalysisKey, TextAnalysisTrust
from app.security.signed_objects import public_key
from contracts.loader import load_trust_set
from app.security.text_analysis_contract import (
    REISSUE_SUPPORTED, SUPPORTED, TextAnalysisSchemas, canonical,
)
from app.security.text_analysis_proof import (
    ProofContext, prepare_request, proof_bytes, validate_challenge, verify_proof,
)
from app.security.text_analysis_reissue import (
    PROFILE, SCHEMA_ROOT, ReissueContext, TextAnalysisReissueError,
    build_prepare_request, build_read_request, build_recover_request, build_finalize_request,
    decode_reissue_document, validate_context, validate_confirmation,
    validate_reissue_projection, validate_reissue_result,
)

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1] / "contracts"
FAMILY = ROOT / "text-analysis-license-reissue-v2"
SCHEMAS = json.loads((FAMILY / "schema-cases.json").read_bytes())
PROOFS = json.loads((FAMILY / "proof-cases.json").read_bytes())
TRANSPORT = json.loads((FAMILY / "transport-cases.json").read_bytes())
RESPONSES = json.loads((ROOT / "text-analysis-license-reissue-response-conformance-v1/response-cases.json").read_bytes())
BASE_PROOFS = {case["case_id"]: case for case in PROOFS if case["valid"]}
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


@pytest.fixture(scope="module")
def trust():
    value = load_trust_set("text-analysis-license-reissue-v2/trust-set.json", environment="development")
    key = value["keys"][0]
    return TextAnalysisTrust({key["jwk"]["kid"]: TextAnalysisKey(
        key["purpose"], public_key({name: key["jwk"][name] for name in ("kty", "crv", "x", "y")}))}, fixture_only=True)


def context():
    base = RESPONSES[0]["confirmation"]
    original = {key: base[key] for key in ("profile", "operation_id", "binding", "supported_profiles", "release_identity")}
    public = BASE_PROOFS["RP-prepare"]["public_key"]
    return ReissueContext(canonical(original), canonical(dict(public, kid=base["binding"]["installation_key_id"])),
                          "2026-10-26T00:30:00.000Z")


@pytest.mark.parametrize("case", SCHEMAS, ids=lambda case: case["case_id"])
def test_reissue_schema_vectors(case, schemas):
    try:
        decode_reissue_document(canonical(case["value"]), schema=case["schema"], schemas=schemas)
        valid = True
    except ValueError:
        valid = False
    assert valid is case["valid"]


@pytest.mark.parametrize("case", TRANSPORT, ids=lambda case: case["case_id"])
def test_reissue_transport_vectors(case, schemas):
    try:
        decode_reissue_document(bytes.fromhex(case["raw_hex"]), schema=case["schema"], schemas=schemas)
        outcome = "accepted"
    except TextAnalysisReissueError as error:
        outcome = error.code
    assert outcome == case["expected"]


@pytest.mark.parametrize("case", PROOFS, ids=lambda case: case["case_id"])
def test_reissue_proof_vectors(case, schemas):
    base = next(value for key, value in BASE_PROOFS.items()
                if case["case_id"] == key or case["case_id"].startswith(key + "-"))
    action = base["purpose"].rsplit("-", 1)[1]
    binding = base["request"]["binding"]
    basis = ProofContext("reissue-v2-" + action, **binding,
        public_key_jwk=canonical(dict(base["public_key"], kid=binding["installation_key_id"])),
        expected_request=canonical(base["request"]))
    try:
        prepared = prepare_request(canonical(case["request"]), context=basis, schemas=schemas)
        challenge = {"profile": "urn:bridge-clean:text-analysis-license-reissue-proof:v2",
            "purpose": case["purpose"], "challenge_id": case["challenge_id"], "challenge": case["challenge"],
            "audience": "urn:bridge-clean:commercial-control-plane:text-analysis-license-reissue:v2",
            "request_digest": prepared.request_digest.hex(), "issued_at": "2026-10-26T00:00:00.000Z",
            "expires_at": "2026-10-26T00:01:00.000Z"}
        validate_challenge(canonical(challenge), prepared=prepared, schemas=schemas, now=NOW)
        valid = verify_proof(case["signature"], prepared=prepared,
            challenge_id=case["challenge_id"], challenge=case["challenge"], context=basis, schemas=schemas)
        if case["valid"]:
            assert prepared.canonical_request.decode() == case["canonical_request"]
            assert prepared.request_digest.hex() == case["request_digest"]
            assert proof_bytes(prepared, challenge_id=case["challenge_id"], challenge=case["challenge"]).hex() == case["proof_bytes_hex"]
    except ValueError:
        valid = False
    assert valid is case["valid"]


@pytest.mark.parametrize("case", RESPONSES, ids=lambda case: case["case_id"])
def test_current_reissue_response_vectors(case, schemas, trust):
    try:
        confirmation = validate_confirmation(canonical(case["confirmation"]), context=context(), schemas=schemas)
        result = validate_reissue_result(canonical(case["result"]), confirmation=confirmation,
            original_finalization_request=canonical(case["finalization_request"]),
            authority_context=case["context"], trust=trust, schemas=schemas)
        assert result.response_bytes == canonical(case["result"])
        valid = True
    except ValueError:
        valid = False
    assert valid is case["valid"]


def test_builders_preserve_exact_inner_meaning_and_profile_isolation(schemas):
    basis = context()
    assert validate_context(basis, schemas=schemas) is basis
    assert build_prepare_request(basis, schemas=schemas) == basis.original_prepare
    assert len(SUPPORTED) == 12 and len(REISSUE_SUPPORTED) == 6
    assert set(REISSUE_SUPPORTED) - set(SUPPORTED) == set(REISSUE_SUPPORTED[:4])
    read = json.loads(build_read_request(basis, schemas=schemas))
    assert read == json.loads(build_recover_request(basis, schemas=schemas)) == {
        "profile": PROFILE, "operation_id": basis.operation_id, "binding": basis.binding}
    confirmation = validate_confirmation(canonical(RESPONSES[0]["confirmation"]), context=basis, schemas=schemas)
    raw = build_finalize_request(confirmation, schemas=schemas, now=NOW)
    assert raw == canonical(RESPONSES[0]["finalization_request"])
    assert json.loads(raw)["confirmation_digest"] == hashlib.sha256(confirmation.confirmation_bytes).hexdigest()
    with pytest.raises(TextAnalysisReissueError, match="Text-analysis reissue") as raised:
        build_finalize_request(confirmation, schemas=schemas, now=NOW + timedelta(minutes=5))
    assert raised.value.code == "authorization_expired"


def test_original_result_recovery_is_exact_after_creation_deadlines(schemas, trust):
    case = RESPONSES[0]
    confirmation = validate_confirmation(canonical(case["confirmation"]), context=context(), schemas=schemas)
    raw = b" " + canonical(case["result"]) + b"\n"
    result = validate_reissue_result(raw, confirmation=confirmation,
        original_finalization_request=canonical(case["finalization_request"]), authority_context=case["context"],
        trust=trust, schemas=schemas, previous_response=raw)
    assert result.response_bytes == raw and result.license_id == case["result"]["license_id"]
    with pytest.raises(TextAnalysisReissueError) as raised:
        validate_reissue_result(raw, confirmation=confirmation,
            original_finalization_request=canonical(case["finalization_request"]), authority_context=case["context"],
            trust=trust, schemas=schemas, previous_response=canonical(case["result"]))
    assert raised.value.code == "result_bytes_changed"


def test_first_preparation_and_protected_confirmation_have_separate_deadlines(schemas):
    basis = replace(context(), operation_expires_at=None)
    preparation = {"profile": PROFILE, "operation_id": basis.operation_id, "binding": basis.binding,
        "state": "awaiting-authorization", "expires_at": "2026-10-26T00:30:00.000Z", "entry_reference": "A" * 43}
    first = validate_reissue_projection(canonical(preparation), operation="prepare", context=basis, schemas=schemas, now=NOW)
    assert first.context.operation_expires_at == preparation["expires_at"]
    confirmation = validate_confirmation(canonical(RESPONSES[0]["confirmation"]), context=first.context, schemas=schemas)
    authorized = {"profile": PROFILE, "operation_id": basis.operation_id, "binding": basis.binding,
        "state": "authorized", "expires_at": confirmation.expires_at,
        "confirmation": json.loads(confirmation.confirmation_bytes), "confirmation_digest": confirmation.confirmation_digest}
    current = validate_reissue_projection(canonical(authorized), operation="read", context=first.context,
        schemas=schemas, now=NOW, previous=first)
    assert current.confirmation == confirmation and current.context.operation_expires_at == preparation["expires_at"]
    assert validate_reissue_projection(canonical(authorized), operation="read", context=first.context,
        schemas=schemas, now=NOW, previous=first, confirmation=confirmation) == current
    with pytest.raises(TextAnalysisReissueError) as raised:
        validate_reissue_projection(canonical({k: v for k, v in preparation.items() if k != "entry_reference"}),
            operation="read", context=first.context, schemas=schemas, now=NOW, previous=current)
    assert raised.value.code == "authorization_regressed"


@pytest.mark.parametrize("field", ["operation_id", "binding", "supported_profiles", "release_identity"])
def test_original_prepare_context_cannot_be_selected_from_confirmation(schemas, field):
    value = copy.deepcopy(RESPONSES[0]["confirmation"])
    if field == "operation_id":
        value[field] = "019a1957-0000-7000-8000-000000000099"
    elif field == "binding":
        value[field]["installation_id"] = "other"
    elif field == "release_identity":
        value[field]["release_version"] = "1.0.1"
    else:
        value[field]["profiles"][0] = "urn:bridge-clean:text-analysis-license-reissue-authorization:v1"
    with pytest.raises(ValueError):
        validate_confirmation(canonical(value), context=context(), schemas=schemas)


@pytest.mark.parametrize("seconds", [0, 30, 60, 61])
def test_reissue_challenge_has_a_positive_at_most_sixty_second_lifetime(schemas, seconds):
    base = BASE_PROOFS["RP-prepare"]
    binding = base["request"]["binding"]
    basis = ProofContext("reissue-v2-prepare", **binding,
        public_key_jwk=canonical(dict(base["public_key"], kid=binding["installation_key_id"])),
        expected_request=canonical(base["request"]))
    prepared = prepare_request(basis.expected_request, context=basis, schemas=schemas)
    challenge = {"profile": "urn:bridge-clean:text-analysis-license-reissue-proof:v2",
        "purpose": base["purpose"], "challenge_id": base["challenge_id"], "challenge": base["challenge"],
        "audience": "urn:bridge-clean:commercial-control-plane:text-analysis-license-reissue:v2",
        "request_digest": prepared.request_digest.hex(), "issued_at": "2026-10-26T00:00:00.000Z",
        "expires_at": (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    if seconds in (30, 60):
        validate_challenge(canonical(challenge), prepared=prepared, schemas=schemas, now=NOW)
    else:
        with pytest.raises(ValueError):
            validate_challenge(canonical(challenge), prepared=prepared, schemas=schemas, now=NOW)


def test_retained_authorization_survives_expired_projection_without_regression(schemas):
    basis = context()
    retained = validate_confirmation(canonical(RESPONSES[0]["confirmation"]), context=basis, schemas=schemas)
    common = {"profile": PROFILE, "operation_id": basis.operation_id, "binding": basis.binding,
              "expires_at": retained.expires_at}
    expired = validate_reissue_projection(canonical(dict(common, state="expired")), operation="read",
        context=basis, schemas=schemas, now=NOW + timedelta(hours=1), confirmation=retained)
    assert expired.confirmation == retained
    with pytest.raises(TextAnalysisReissueError) as raised:
        validate_reissue_projection(canonical(dict(common, state="awaiting-authorization")), operation="read",
            context=basis, schemas=schemas, now=NOW + timedelta(hours=1), confirmation=retained)
    assert raised.value.code == "authorization_regressed"


@pytest.mark.parametrize("field,maximum", [("binding", 512), ("scope", 2048), ("proof", 256),
                                         ("supported_profiles", 3072), ("release_identity", 1024)])
def test_embedded_bounds_precede_schema_work(schemas, field, maximum):
    raw = canonical({field: "x" * maximum})
    with pytest.raises(TextAnalysisReissueError) as raised:
        decode_reissue_document(raw, schema=SCHEMA_ROOT + "read-response.schema.json", schemas=schemas)
    assert raised.value.code == "invalid/413"
