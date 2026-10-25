"""Offline text-analysis authority conformance."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from app.security.signed_objects import encode
import app.security.text_analysis_authority as authority_module
from app.security.text_analysis_authority import (
    ORDER, TextAnalysisConfigurationError, TextAnalysisKey, TextAnalysisTrust,
    fixture_trust, verify_authority,
)
from app.security.text_analysis_contract import (
    OUTPUT_POLICY, PREFIX, SUPPORTED, REQUIRED_LICENSE, REQUIRED_PREVIEW,
    TextAnalysisContractError, TextAnalysisSchemas, canonical, parse_json,
    output_policy, release_identity, supported_profiles,
)

ROOT = Path(__file__).resolve().parents[1] / "contracts" / "text-analysis-authority-v1"
SIGNED = json.loads((ROOT / "signed-cases.json").read_bytes())
SCHEMA = json.loads((ROOT / "schema-cases.json").read_bytes())


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


@pytest.fixture(scope="module")
def trust():
    return fixture_trust(environment="test")


@pytest.mark.parametrize("case", SIGNED, ids=lambda case: case["case_id"])
def test_signed_vectors(case, schemas, trust):
    if case["expected"] == "configuration_failure":
        with pytest.raises(TextAnalysisConfigurationError):
            verify_authority(case["token"], kind=case["kind"], context=case["context"],
                             trust=trust, schemas=schemas)
    else:
        decision = verify_authority(case["token"], kind=case["kind"], context=case["context"],
                                    trust=trust, schemas=schemas)
        assert decision.reason == case["expected"]
        assert decision.accepted == (case["expected"] == "accepted")
        if decision.accepted:
            assert decision.authority.fixture_only
            assert decision.authority.compact_jws == case["token"]


@pytest.mark.parametrize("case", SCHEMA, ids=lambda case: case["case_id"])
def test_schema_vectors(case, schemas):
    assert schemas.valid(case["schema"], case["value"]) is case["valid"]


def test_fixture_trust_refuses_production():
    with pytest.raises(TextAnalysisConfigurationError):
        fixture_trust(environment="production")


def test_no_implicit_context_or_type_dispatch(schemas, trust):
    case = SIGNED[0]
    with pytest.raises(TextAnalysisConfigurationError):
        verify_authority(case["token"], kind="capability-license", context=case["context"],
                         trust=trust, schemas=schemas)
    with pytest.raises(TextAnalysisConfigurationError):
        verify_authority(case["token"], kind="license", context={}, trust=trust, schemas=schemas)


def test_scope_is_immutable_after_verification(schemas, trust):
    case = SIGNED[0]
    result = verify_authority(case["token"], kind=case["kind"], context=case["context"],
                              trust=trust, schemas=schemas)
    with pytest.raises(TypeError):
        result.authority.payload["capability"] = "other"
    assert type(result.authority.payload["fallback_major_versions"]) is tuple


def test_bounded_policy_and_closed_supported_profiles(schemas):
    assert output_policy(canonical(OUTPUT_POLICY), schemas=schemas) == OUTPUT_POLICY
    for value in (dict(OUTPUT_POLICY, version=True), dict(OUTPUT_POLICY, extra=0)):
        with pytest.raises(TextAnalysisContractError):
            output_policy(canonical(value), schemas=schemas)
    assert len(SUPPORTED) == 12 and len(REQUIRED_LICENSE) == 7 and len(REQUIRED_PREVIEW) == 10
    raw = canonical({"profile": PREFIX + "supported-profiles:v1", "profiles": list(SUPPORTED)})
    assert supported_profiles(raw, schemas=schemas, authority="preview", expected_profiles=SUPPORTED) == SUPPORTED
    with pytest.raises(TextAnalysisContractError):
        supported_profiles(raw, schemas=schemas, authority="preview", expected_profiles=tuple(sorted(REQUIRED_PREVIEW)))
    for profiles in (list(reversed(SUPPORTED)), list(SUPPORTED) + [SUPPORTED[0]],
                     ["urn:bridge-clean:capability-license:v1"], list(sorted(REQUIRED_LICENSE))):
        with pytest.raises(TextAnalysisContractError):
            supported_profiles(canonical({"profile": PREFIX + "supported-profiles:v1", "profiles": profiles}),
                               schemas=schemas, authority="preview", expected_profiles=SUPPORTED)


def test_release_exact_comparison_and_duplicate_rejection(schemas):
    case = next(case for case in SCHEMA if case["valid"] and
                case["schema"].endswith("release-identity.schema.json"))
    value = case["value"]
    assert release_identity(canonical(value), schemas=schemas, expected=value) == value
    different = copy.deepcopy(value)
    different["artifact_sha256"] = "a" * 64
    with pytest.raises(TextAnalysisContractError):
        release_identity(canonical(value), schemas=schemas, expected=different)
    with pytest.raises(TextAnalysisContractError):
        output_policy(b'{"profile":"x","profile":"y","version":1}', schemas=schemas)
    with pytest.raises(TextAnalysisContractError):
        output_policy(b" " * 257, schemas=schemas)


def test_compact_size_and_multifault_order(schemas, trust):
    case = SIGNED[0]
    for token in ("", "x" * 16385, "a.b", "a.b.c.d", "\u00e9.b.c"):
        result = verify_authority(token, kind="license", context=case["context"], trust=trust, schemas=schemas)
        assert result.reason == "invalid_compact_jws"


def test_first_failure_order_before_signature(schemas, trust):
    case = SIGNED[0]
    h, p, sig = case["token"].split(".")
    def decision(header=h, payload=p, signature=sig):
        return verify_authority(".".join((header, payload, signature)), kind="license",
                                context=case["context"], trust=trust, schemas=schemas).reason
    assert decision(header=encode(b"{")) == "invalid_compact_jws"
    assert decision(header=encode(b"\xff")) == "invalid_compact_jws"
    assert decision(header=encode(b'{"alg":"ES256","alg":"bad"}')) == "noncanonical_json"
    assert decision(payload=encode(b" " + canonical(case["payload"]))) == "noncanonical_json"
    assert decision(header=encode(canonical(dict(case["protected"], alg="none", typ="wrong")))) == "invalid_header"
    wrong_audience = encode(canonical(dict(case["payload"], aud="other")))
    assert decision(header=encode(canonical(dict(case["protected"], typ="wrong"))),
                    payload=wrong_audience) == "typ_mismatch"
    unknown = encode(canonical(dict(case["protected"], kid="bc1.tl." + "A" * 22)))
    assert decision(header=unknown, payload=wrong_audience) == "audience_mismatch"
    assert decision(header=unknown, signature=encode(b"short")) == "unknown_kid"
    preview_kid = next(kid for kid in trust.keys if kid.startswith("bc1.tp."))
    assert decision(header=encode(canonical(dict(case["protected"], kid=preview_kid))),
                    signature=encode(b"short")) == "wrong_key_purpose"
    assert decision(signature=encode(b"short")) == "invalid_signature"
    assert decision(payload=encode(canonical(dict(case["payload"], unknown=1)))) == "invalid_signature"
    assert decision(payload=encode(b"[]")) == "audience_mismatch"


@pytest.mark.parametrize("mutation", ["extra", "production", "purpose", "kid", "private_key", "duplicate"])
def test_fixture_trust_is_closed_and_purpose_bound(monkeypatch, mutation):
    value = json.loads((ROOT / "trust-set.json").read_bytes())
    if mutation == "extra":
        value["extra"] = 1
    elif mutation == "production":
        value["production_usable"] = True
    elif mutation == "purpose":
        value["keys"][0]["purpose"] = "capability-license"
    elif mutation == "kid":
        value["keys"][0]["jwk"]["kid"] = "bc1.tl." + "A" * 22
    elif mutation == "private_key":
        value["keys"][0]["jwk"]["d"] = "A" * 43
    else:
        value["keys"][1] = copy.deepcopy(value["keys"][0])
    monkeypatch.setattr(authority_module, "load_trust_set", lambda *args, **kwargs: value)
    with pytest.raises(TextAnalysisConfigurationError):
        fixture_trust(environment="test")


def test_closed_context_and_independent_seat(schemas, trust):
    case = SIGNED[0]
    for context in (dict(case["context"], extra=1),
                    {key: value for key, value in case["context"].items() if key != "expected_subject"}):
        with pytest.raises(TextAnalysisConfigurationError):
            verify_authority(case["token"], kind="license", context=context, trust=trust, schemas=schemas)
    context = dict(case["context"], expected_subject=case["context"]["expected_subject"].replace("fixture-seat", "other-seat"))
    result = verify_authority(case["token"], kind="license", context=context, trust=trust, schemas=schemas)
    assert result.reason == "subject_mismatch"


@pytest.mark.parametrize("value, expected", [
    (0.0, b"0"), (-0.0, b"0"), (0.5, b"0.5"),
    (333333333.3333333, b"333333333.3333333"),
    (1e30, b"1e+30"), (1e21, b"1e+21"), (1e20, b"100000000000000000000"),
    (1e-6, b"0.000001"), (1e-7, b"1e-7"),
    (5e-324, b"5e-324"), (1.7976931348623157e308, b"1.7976931348623157e+308"),
    (9007199254740992.0, b"9007199254740992"),
])
def test_rfc8785_number_representation(value, expected):
    assert canonical(value) == expected
    assert canonical(parse_json(expected, maximum=128, require_canonical=True)) == expected


def test_rfc8785_utf16_key_order_and_escaping():
    value = {"\ufb33": 7, "\U0001f600": 6, "\u20ac": 5, "\u00f6": 4, "\u0080": 3, "1": 2, "\r": 1}
    expected = '{"\\r":1,"1":2,"\u0080":3,"\u00f6":4,"\u20ac":5,"\U0001f600":6,"\ufb33":7}'.encode()
    assert canonical(value) == expected
    assert canonical({"s": "\u000f\n\\\"/"}) == b'{"s":"\\u000f\\n\\\\\\\"/"}'


@pytest.mark.parametrize("raw", [b"1.0", b"-0", b"1e30", b"1E+30", b"9007199254740993"])
def test_noncanonical_number_spellings(raw):
    with pytest.raises(ValueError):
        parse_json(raw, maximum=128, require_canonical=True)


@pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"1e400", b'"\\ud800"',
                                  b'{"x":0,"x":0}', b"[" * 18 + b"0" + b"]" * 18])
def test_isolated_json_parser_refuses_nonfinite_unicode_duplicates_and_depth(raw):
    with pytest.raises(ValueError):
        parse_json(raw, maximum=256, require_canonical=True)


@pytest.fixture(scope="module")
def local_signer():
    # An ephemeral local test key exercises signature-to-schema precedence.
    import hashlib
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_numbers()
    jwk = {"crv": "P-256", "kty": "EC", "x": encode(point.x.to_bytes(32, "big")),
           "y": encode(point.y.to_bytes(32, "big"))}
    kid = "bc1.tl." + encode(hashlib.sha256(canonical(jwk)).digest()[:16])
    trust = TextAnalysisTrust({kid: TextAnalysisKey("text-analysis-license", private.public_key())}, fixture_only=True)
    def sign(header, payload):
        message = encode(canonical(header)) + "." + encode(canonical(payload))
        der = private.sign(message.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")
        return message + "." + encode(signature)
    return kid, trust, sign


@pytest.mark.parametrize("number", [0.5, 1e30, 9007199254740992.0])
def test_canonical_numeric_extras_follow_header_and_payload_precedence(number, schemas, local_signer):
    kid, trust, sign = local_signer
    case = SIGNED[0]
    header = dict(case["protected"], kid=kid)
    extra_header = sign(dict(header, extra=number), case["payload"])
    result = verify_authority(extra_header, kind="license", context=case["context"], trust=trust, schemas=schemas)
    assert result.reason == "invalid_header"
    extra_payload = sign(header, dict(case["payload"], extra=number))
    result = verify_authority(extra_payload, kind="license", context=case["context"], trust=trust, schemas=schemas)
    assert result.reason == "schema_invalid"
    numeric_issued_at = sign(header, dict(case["payload"], iat=number))
    result = verify_authority(numeric_issued_at, kind="license", context=case["context"], trust=trust, schemas=schemas)
    assert result.reason == "schema_invalid"
