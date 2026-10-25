"""Native cryptographic conformance without hosted challenge state."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from app.security.installation_key import InstallationProof
from app.security.signed_objects import decode, encode
from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
from app.security.text_analysis_proof import (
    ProofContext, TextAnalysisProofError, prepare_request, proof_bytes,
    sign_request, validate_challenge, verify_proof,
)

pytestmark = pytest.mark.ci_tier("fast")
ROOT = Path(__file__).resolve().parents[1] / "contracts"
CASES = json.loads((ROOT / "text-analysis-authority-v1/proof-cases.json").read_bytes())
BASES = {case["case_id"]: case for case in CASES if case["valid"]}
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
OPERATIONS = {"IL-issue": "license-issue", "IP-issue": "preview-issue", "DP-deliver": "authority-deliver",
              "DP-recover": "authority-recover", "XP-prepare": "preview-prepare",
              "XP-read": "preview-read", "XP-submit": "preview-submit"}
FAMILIES = {"IL": "license-issuance", "IP": "preview-issuance", "DP": "authority-delivery", "XP": "preview-acquisition"}


@pytest.fixture(scope="module")
def schemas():
    return TextAnalysisSchemas()


def context_for(base):
    binding = base["request"]["binding"]
    return ProofContext(operation=OPERATIONS[base["case_id"]], **binding,
                        public_key_jwk=canonical(dict(base["public_key"], kid=binding["installation_key_id"])),
                        expected_request=canonical(base["request"]))


def challenge_for(base, prepared, *, purpose=None):
    family = FAMILIES[base["kind"]]
    profile = json.loads((ROOT / f"profiles/text-analysis-{family}-proof/v1/profile.json").read_bytes())
    return {"profile": profile["profile"], "purpose": purpose or base["purpose"],
            "challenge_id": base["challenge_id"], "challenge": base["challenge"],
            "audience": profile["audience"], "request_digest": prepared.request_digest.hex(),
            "issued_at": "2026-10-25T00:00:00.000Z", "expires_at": "2026-10-25T00:01:00.000Z"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_published_native_proof_vectors(case, schemas):
    name = next(name for name in BASES if case["case_id"] == name or case["case_id"].startswith(name + "-"))
    base = BASES[name]
    context = context_for(base)
    try:
        prepared = prepare_request(canonical(case["request"]), context=context, schemas=schemas)
        challenge = challenge_for(base, prepared, purpose=case["purpose"])
        validate_challenge(canonical(challenge), prepared=prepared, schemas=schemas, now=NOW)
        result = verify_proof(case["signature"], prepared=prepared, challenge_id=case["challenge_id"],
                              challenge=case["challenge"], context=context, schemas=schemas)
        if case["valid"]:
            assert prepared.canonical_request.decode() == case["canonical_request"]
            assert prepared.request_digest.hex() == case["request_digest"]
            assert proof_bytes(prepared, challenge_id=case["challenge_id"], challenge=case["challenge"]).hex() == case["proof_bytes_hex"]
    except ValueError:
        result = False
    assert result is case["valid"]


class ExistingKey:
    def __init__(self, private, context, mode=None):
        self.private, self.context, self.mode = private, context, mode
        self.reopened, self.messages = 0, []

    def reopen_existing(self):
        self.reopened += 1
        identifier = self.context.installation_key_id
        if self.mode == "changed_after_sign" and self.messages:
            identifier = "ik1." + "A" * 22
        return SimpleNamespace(installation_key_id=identifier,
                               installation_key_jkt=self.context.installation_key_jkt,
                               public_key_jwk=self.context.public_key_jwk.decode())

    def ensure_ready(self):
        pytest.fail("key creation path must not be used")

    def sign_challenge(self, message):
        self.messages.append(message)
        der = self.private.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        s = min(s, ORDER - s)
        if self.mode == "high_s":
            s = ORDER - s
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        if self.mode == "short":
            raw = raw[:-1]
        if self.mode == "tamper":
            raw = bytes([raw[0] ^ 1]) + raw[1:]
        return InstallationProof(self.context.installation_key_id, "ES256", raw)


@pytest.fixture(scope="module")
def local_contexts():
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": encode(point.x.to_bytes(32, "big")), "y": encode(point.y.to_bytes(32, "big"))}
    thumb = hashlib.sha256(canonical(jwk)).digest()
    key_id, jkt = "ik1." + encode(thumb[:16]), encode(thumb)
    contexts = {}
    for name, base in BASES.items():
        value = copy.deepcopy(base["request"])
        value["binding"].update(installation_key_id=key_id, installation_key_jkt=jkt)
        context = context_for(base)
        contexts[name] = replace(context, installation_key_id=key_id, installation_key_jkt=jkt,
                                 public_key_jwk=canonical(dict(jwk, kid=key_id)), expected_request=canonical(value))
    return private, contexts


@pytest.mark.parametrize("name", list(BASES))
def test_noncreating_signing_and_self_verification(name, schemas, local_contexts):
    private, contexts = local_contexts
    context = contexts[name]
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    challenge = challenge_for(BASES[name], prepared)
    key = ExistingKey(private, context)
    result = sign_request(context.expected_request, canonical(challenge), context=context,
                          key_authority=key, schemas=schemas, clock=lambda: NOW, monotonic=lambda: 0)
    envelope = json.loads(result)
    assert key.reopened == 2 and len(key.messages) == 1
    assert key.messages[0] == proof_bytes(prepared, challenge_id=challenge["challenge_id"], challenge=challenge["challenge"])
    assert envelope["request"] == json.loads(context.expected_request)
    assert verify_proof(envelope["proof"]["signature"], prepared=prepared, challenge_id=challenge["challenge_id"],
                        challenge=challenge["challenge"], context=context, schemas=schemas)


@pytest.mark.parametrize("mode", ["high_s", "short", "tamper", "changed_after_sign"])
def test_signing_rejects_bad_or_changed_key_material(mode, schemas, local_contexts):
    private, contexts = local_contexts
    context = contexts["IL-issue"]
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    with pytest.raises(ValueError):
        sign_request(context.expected_request, canonical(challenge_for(BASES["IL-issue"], prepared)),
                     context=context, key_authority=ExistingKey(private, context, mode), schemas=schemas,
                     clock=lambda: NOW, monotonic=lambda: 0)


@pytest.mark.parametrize("field, value", [
    ("organization_id", "other"), ("installation_id", "other"),
    ("installation_key_id", "ik1." + "A" * 22), ("installation_key_jkt", "A" * 43),
    ("operation", "license-reissue"), ("public_key_jwk", b"{}"),
])
def test_independent_context_is_required(field, value, schemas):
    context = context_for(BASES["IL-issue"])
    with pytest.raises(ValueError):
        prepare_request(context.expected_request, context=replace(context, **{field: value}), schemas=schemas)


@pytest.mark.parametrize("field", ["operation_id", "issuance_authorization_id", "confirmation_digest", "release_identity"])
def test_retained_request_meaning_cannot_change(field, schemas):
    context = context_for(BASES["IL-issue"])
    value = json.loads(context.expected_request)
    if field == "release_identity":
        value[field]["release_version"] = "1.0.1"
    elif field.endswith("digest"):
        value[field] = "a" * 64
    else:
        value[field] = "019a1957-0000-7000-8000-000000000099"
    with pytest.raises(ValueError):
        prepare_request(canonical(value), context=context, schemas=schemas)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b"\xff", b"{}" + b" " * 8192, b"[]", b'{"x":9007199254740993}'],
                         ids=["duplicate", "utf8", "oversize", "array", "unsafe-number"])
def test_request_input_is_bounded_and_closed(raw, schemas):
    with pytest.raises(ValueError):
        prepare_request(raw, context=context_for(BASES["IL-issue"]), schemas=schemas)


@pytest.mark.parametrize("field, value", [
    ("profile", "urn:bridge-clean:installation-key-proof:v1"), ("purpose", "other"),
    ("audience", "other"), ("request_digest", "a" * 64), ("challenge", "A" * 43 + "="),
    ("challenge_id", "not-a-uuid"), ("issued_at", "2026-02-30T00:00:00.000Z"),
    ("expires_at", "2026-10-25T00:01:01.000Z"), ("expires_at", "2026-10-25T00:00:00.000Z"),
])
def test_challenge_binding_and_exact_lifetime(field, value, schemas):
    base = BASES["IL-issue"]
    context = context_for(base)
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    challenge = challenge_for(base, prepared)
    challenge[field] = value
    with pytest.raises(ValueError):
        validate_challenge(canonical(challenge), prepared=prepared, schemas=schemas, now=NOW)


@pytest.mark.parametrize("offset", [-1, 60, 61])
def test_challenge_time_boundaries(offset, schemas):
    base = BASES["IL-issue"]
    context = context_for(base)
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    with pytest.raises(ValueError):
        validate_challenge(canonical(challenge_for(base, prepared)), prepared=prepared, schemas=schemas,
                           now=NOW + timedelta(seconds=offset))


def test_monotonic_budget_covers_blocking_signing(schemas, local_contexts):
    private, contexts = local_contexts
    context = contexts["IL-issue"]
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    key = ExistingKey(private, context)
    def monotonic():
        return 60 if key.messages else 0
    with pytest.raises(TextAnalysisProofError, match="proof_deadline"):
        sign_request(context.expected_request, canonical(challenge_for(BASES["IL-issue"], prepared)), context=context,
                     key_authority=key, schemas=schemas, clock=lambda: NOW, monotonic=monotonic)


@pytest.mark.parametrize("raw", [b"{}" + b" " * 4096, b"[]", b"\xff", b'{"purpose":"x","purpose":"y"}'],
                         ids=["oversize", "array", "utf8", "duplicate"])
def test_challenge_raw_input_limits(raw, schemas):
    context = context_for(BASES["IL-issue"])
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    with pytest.raises(ValueError):
        validate_challenge(raw, prepared=prepared, schemas=schemas, now=NOW)


@pytest.mark.parametrize("kind", ["unsorted", "missing_required", "extra_field"])
def test_retained_support_context_must_itself_be_valid(kind, schemas):
    context = context_for(BASES["IL-issue"])
    value = json.loads(context.expected_request)
    support = value["supported_profiles"]
    if kind == "unsorted":
        support["profiles"].reverse()
    elif kind == "missing_required":
        support["profiles"].remove("urn:bridge-clean:text-analysis-license:v1")
    else:
        support["extra"] = 1
    raw = canonical(value)
    with pytest.raises(ValueError):
        prepare_request(raw, context=replace(context, expected_request=raw), schemas=schemas)


@pytest.mark.parametrize("mode", ["before_sign", "after_sign", "naive_clock", "negative_elapsed", "nonfinite_clock"])
def test_key_and_time_checks_surround_signing(mode, schemas, local_contexts):
    private, contexts = local_contexts
    context = contexts["IL-issue"]
    prepared = prepare_request(context.expected_request, context=context, schemas=schemas)
    key = ExistingKey(private, context)
    def clock():
        if mode == "naive_clock":
            return NOW.replace(tzinfo=None)
        if (mode == "before_sign" and key.reopened) or (mode == "after_sign" and key.messages):
            return NOW + timedelta(seconds=60)
        return NOW
    calls = 0
    def monotonic():
        nonlocal calls
        calls += 1
        if mode == "nonfinite_clock":
            return float("nan")
        if mode == "negative_elapsed" and calls > 1:
            return -1
        return 0
    with pytest.raises(ValueError):
        sign_request(context.expected_request, canonical(challenge_for(BASES["IL-issue"], prepared)), context=context,
                     key_authority=key, schemas=schemas, clock=clock, monotonic=monotonic)
    if mode in ("before_sign", "naive_clock", "negative_elapsed", "nonfinite_clock"):
        assert not key.messages


def test_shared_definition_validator_is_closed(schemas):
    path = "schemas/text-analysis-authority-delivery/v1/definitions.schema.json"
    value = BASES["DP-deliver"]["request"]
    assert schemas.valid_definition(path, "request", value)
    assert not schemas.valid_definition(path, "request", dict(value, extra=1))
    with pytest.raises(ValueError):
        schemas.valid_definition(path, "../../other", value)
