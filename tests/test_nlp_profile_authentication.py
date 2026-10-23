"""Invented signing keys and complete descriptors at the offline boundary."""
from dataclasses import FrozenInstanceError
import copy
import hashlib
import json
from pathlib import Path
import traceback
import warnings

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from pydantic import ValidationError

from app.nlp import profile_authentication as authentication
from app.nlp.profiles import (
    AnalysisProfile, Artifact, BASELINE_RESOURCE_POLICY, MAX_SAFE_INTEGER,
    ResourcePolicy, artifact_closure_digest, canonical_bytes,
)
from app.security import signed_objects
from app.security import signing_parameter_publications as publications

pytestmark = pytest.mark.ci_tier("fast")
FIXTURES = Path(__file__).resolve().parents[1] / "shared/nlp/v2/fixtures"
PRIVATE_KEY = ec.derive_private_key(19, ec.SECP256R1())
KID = "example-nlp-key"


def keys(private_key=PRIVATE_KEY, kid=KID):
    public = private_key.public_key().public_numbers()
    return [{"kid": kid, "jwk": {
        "kty": "EC", "crv": "P-256",
        "x": signed_objects.encode(public.x.to_bytes(32, "big")),
        "y": signed_objects.encode(public.y.to_bytes(32, "big")),
    }}]


def profile(kind="joint"):
    return AnalysisProfile.model_validate_json((FIXTURES / (kind + "-profile.json")).read_bytes())


def sign(raw, *, typ=authentication.PROFILE_JWS_TYPE, private_key=PRIVATE_KEY,
         kid=KID, header=None):
    if header is None:
        header = json.dumps({"alg": "ES256", "kid": kid, "typ": typ},
                            sort_keys=True, separators=(",", ":")).encode()
    message = (signed_objects.encode(header) + "." + signed_objects.encode(raw)).encode("ascii")
    r, s = utils.decode_dss_signature(private_key.sign(message, ec.ECDSA(hashes.SHA256())))
    return message + b"." + signed_objects.encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).encode()


def authenticate(value=None, *, raw=None, **kwargs):
    value = profile() if value is None else value
    raw = canonical_bytes(value) if raw is None else raw
    return authentication.authenticate_profile(
        sign(raw), trusted_nlp_keys=keys(), expected_profile_sha256=value.identity_digest(), **kwargs,
    )


@pytest.mark.parametrize("kind", ["joint", "split"])
def test_complete_profile_authentication_is_immutable_metadata_only(kind):
    original = profile(kind)
    observed = authenticate(original)
    assert observed.profile == original
    assert observed.profile is not original
    assert observed.profile_sha256 == original.identity_digest()
    assert observed.kid == KID
    with pytest.raises(FrozenInstanceError):
        observed.kid = "changed"
    with pytest.raises(ValidationError):
        observed.profile.revision = "changed"
    with pytest.raises(ValidationError):
        observed.profile.components[0].engine.publisher = "changed"
    assert set(observed.__dict__) == {"profile", "profile_sha256", "kid"}


def test_same_exact_replay_authenticates_but_other_revision_requires_its_exact_digest():
    original = profile()
    first = authenticate(original)
    assert authenticate(original) == first
    revised = profile("revision")
    assert authenticate(revised).profile_sha256 != first.profile_sha256
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(canonical_bytes(original)), trusted_nlp_keys=keys(),
                                            expected_profile_sha256=revised.identity_digest())


@pytest.mark.parametrize("fault", ["payload", "signature", "signer", "unknown_kid", "digest", "component_digest"])
def test_substitution_and_wrong_exact_identity_are_rejected(fault):
    value = profile()
    compact = sign(canonical_bytes(value))
    trusted = keys()
    expected = value.identity_digest()
    if fault == "payload":
        parts = compact.split(b".")
        parts[1] = signed_objects.encode(canonical_bytes(profile("revision"))).encode()
        compact = b".".join(parts)
    elif fault == "signature":
        parts = compact.split(b".")
        parts[2] = signed_objects.encode(b"\0" * 64).encode()
        compact = b".".join(parts)
    elif fault == "signer":
        trusted = keys(ec.derive_private_key(23, ec.SECP256R1()))
    elif fault == "unknown_kid":
        trusted = keys(kid="other")
    elif fault == "digest":
        expected = "f" * 64
    else:
        expected = value.components[0].identity_digest()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(compact, trusted_nlp_keys=trusted, expected_profile_sha256=expected)


@pytest.mark.parametrize("typ", ["signing-publisher-keyset+jws", "signing-parameter-catalog+jws", "other.v1+jws"])
def test_other_signature_domains_cannot_supply_nlp_authority(typ):
    value = profile()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(canonical_bytes(value), typ=typ), trusted_nlp_keys=keys(),
                                            expected_profile_sha256=value.identity_digest())


@pytest.mark.parametrize("fault", ["alg", "extra_key", "duplicate_key", "order", "oversized", "kid_type"])
def test_protected_header_is_closed_canonical_and_bounded(fault):
    header = {"alg": "ES256", "kid": KID, "typ": authentication.PROFILE_JWS_TYPE}
    if fault == "alg":
        header["alg"] = "none"
    elif fault == "extra_key":
        header["jwk"] = keys()[0]["jwk"]
    elif fault == "oversized":
        header["kid"] = "x" * 600
    elif fault == "kid_type":
        header["kid"] = 1
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    if fault == "duplicate_key":
        raw = raw.replace(b'"alg":"ES256"', b'"alg":"ES256","alg":"ES256"')
    elif fault == "order":
        raw = json.dumps(header).encode()
    value = profile()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(canonical_bytes(value), header=raw), trusted_nlp_keys=keys(),
                                            expected_profile_sha256=value.identity_digest())


@pytest.mark.parametrize("fault", ["empty", "too_many", "duplicate", "extra", "private", "point", "curve", "coordinate", "generator"])
def test_caller_keys_must_form_one_bounded_unique_public_key_set(fault):
    trusted = keys()
    if fault == "empty":
        trusted = []
    elif fault == "too_many":
        trusted = [keys(kid=f"example-{index}")[0] for index in range(authentication.MAX_NLP_KEYS + 1)]
    elif fault == "duplicate":
        trusted *= 2
    elif fault == "extra":
        trusted[0]["role"] = "nlp"
    elif fault == "private":
        trusted[0]["jwk"]["d"] = "AA"
    elif fault == "point":
        trusted[0]["jwk"].update(x=signed_objects.encode(b"\0" * 32), y=signed_objects.encode(b"\0" * 32))
    elif fault == "curve":
        trusted[0]["jwk"]["crv"] = "P-384"
    elif fault == "coordinate":
        trusted[0]["jwk"]["x"] += "="
    else:
        trusted = iter(trusted)
    value = profile()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(canonical_bytes(value)), trusted_nlp_keys=trusted,
                                            expected_profile_sha256=value.identity_digest())


@pytest.mark.parametrize("fault", ["duplicate", "nested_duplicate", "order", "default_omitted", "float", "bom", "utf8", "surrogate", "nested", "non_object"])
def test_payload_parser_and_canonical_identity_refuse_ambiguous_json(fault):
    value = profile()
    raw = canonical_bytes(value)
    document = value.model_dump(mode="json")
    if fault == "duplicate":
        raw = raw[:-1] + b',"revision":"1"}'
    elif fault == "nested_duplicate":
        raw = raw.replace(b'"size_bytes":16', b'"size_bytes":16,"size_bytes":16', 1)
    elif fault == "order":
        raw = json.dumps(document).encode()
    elif fault == "default_omitted":
        del document["canonicalization"]
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    elif fault == "float":
        raw = raw.replace(b'"size_bytes":16', b'"size_bytes":16.0', 1)
    elif fault == "bom":
        raw = b"\xef\xbb\xbf" + raw
    elif fault == "utf8":
        raw = b'"\xff"'
    elif fault == "surrogate":
        raw = b'"\\ud800"'
    elif fault == "nested":
        raw = b"[" * 20 + b"0" + b"]" * 20
    else:
        raw = b"null"
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(value, raw=raw)


@pytest.mark.parametrize("fault", ["padding", "trailing_bits", "segment", "non_ascii", "string"])
def test_compact_representation_has_one_canonical_base64url_spelling(fault):
    value = profile()
    compact = sign(canonical_bytes(value))
    parts = compact.split(b".")
    if fault == "padding":
        parts[2] += b"="
        compact = b".".join(parts)
    elif fault == "trailing_bits":
        alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        index = alphabet.index(parts[2][-1])
        parts[2] = parts[2][:-1] + bytes([alphabet[index + 1]])
        compact = b".".join(parts)
    elif fault == "segment":
        compact += b".extra"
    elif fault == "non_ascii":
        compact = b"\xff" + compact[1:]
    else:
        compact = compact.decode("ascii")
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(compact, trusted_nlp_keys=keys(), expected_profile_sha256=value.identity_digest())


@pytest.mark.parametrize("fault", ["second_weights", "binding", "closure", "unreferenced", "runtime"])
def test_signature_cannot_authenticate_an_incomplete_or_inconsistent_profile(fault):
    value = profile("split")
    document = value.model_dump(mode="json")
    if fault == "second_weights":
        document["artifacts"] = [asset for asset in document["artifacts"] if asset["artifact_id"] != "weights.mentions"]
    elif fault == "binding":
        document["bindings"].pop()
    elif fault == "closure":
        document["components"][0]["artifact_closure_sha256"] = "f" * 64
    elif fault == "unreferenced":
        asset = copy.deepcopy(document["artifacts"][-1])
        asset["artifact_id"] = "unreferenced"
        document["artifacts"].append(asset)
    else:
        document["artifacts"] = [asset for asset in document["artifacts"] if asset["role"] != "runtime"]
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(raw), trusted_nlp_keys=keys(),
                                            expected_profile_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("field", ["download_bytes", "installed_bytes", "temporary_bytes", "transition_peak_memory_bytes", "inference_threads"])
def test_signature_cannot_raise_callers_resource_limits(field):
    value = profile()
    limit = value.resources.model_dump()[field] - 1
    policy = BASELINE_RESOURCE_POLICY.model_copy(update={field: limit})
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(value, resource_policy=policy)


@pytest.mark.parametrize("invalid", [-1, True, 2.0, MAX_SAFE_INTEGER + 1, None, "invalid"])
def test_unchecked_resource_policy_is_revalidated(invalid):
    policy = BASELINE_RESOURCE_POLICY.model_copy(update={"download_bytes": invalid})
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(authentication.ProfileAuthenticationRejected):
            authenticate(resource_policy=policy)
    assert not recorded


@pytest.mark.parametrize("policy", [ResourcePolicy.model_construct(), {}, None])
def test_missing_or_untyped_policy_is_not_a_valid_local_limit(policy):
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(resource_policy=policy)


def test_caller_policy_alternative_and_rollback_are_checked():
    document = profile().model_dump(mode="json")
    document["resources"].update(download_bytes=BASELINE_RESOURCE_POLICY.download_bytes + 1)
    value = AnalysisProfile.model_validate_json(json.dumps(document))
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(value)
    alternative = BASELINE_RESOURCE_POLICY.model_copy(update={"download_bytes": value.resources.download_bytes})
    assert authenticate(value, resource_policy=alternative).profile == value
    document["resources"].update(installed_bytes=1024, rollback_bytes=1)
    value = AnalysisProfile.model_validate_json(json.dumps(document))
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(value, resource_policy=alternative.model_copy(update={"installed_bytes": 1024}))


def maximal_profile():
    document = profile("split").model_dump(mode="json")

    def identifier(prefix):
        return prefix + "x" * (160 - len(prefix))

    def fill_identifiers(value):
        for key, item in value.items():
            if key not in ("abi", "platform", "worker_protocol", "schema_version", "canonicalization") and isinstance(item, str):
                if not key.endswith("sha256") and key not in ("language", "execution_mode"):
                    value[key] = identifier(item[:32])

    assets = []
    for index in range(256):
        role = "engine" if index == 0 else "runtime" if index == 1 else "weights"
        assets.append(dict(artifact_id=identifier(f"artifact.{index}."), sha256="a" * 64,
                           size_bytes=MAX_SAFE_INTEGER // 256, role=role,
                           format_id=identifier("format.")))
    document["artifacts"] = assets
    for index, component in enumerate(document["components"]):
        fill_identifiers(component)
        fill_identifiers(component["engine"])
        component["engine"]["publisher"] = "😀" * 512
        fill_identifiers(component["adapter"])
        component["adapter"].update(artifact_format=identifier("format."), tokenizer_format="none")
        for key in ("runtime_requirements", "preprocessing", "windowing", "decoding", "output_mapping", "decision"):
            fill_identifiers(component[key])
        task = component["tasks"][0]
        task["enabled_ids"] = [identifier(f"label.{number}.") for number in range(64)]
        fill_identifiers(task["taxonomy"])
        task["score_policy"]["semantics_id"] = identifier("score.")
        component["executed_labels"] = [
            dict(task=name, label_id=identifier(f"label.{number}."), description="😀" * 2048)
            for name in ("message_topics", "concept_mentions") for number in range(64)
        ]
        selected = assets[2:130] if index == 0 else assets[128:256]
        component["artifact_ids"] = [asset["artifact_id"] for asset in selected]
        closure = tuple(Artifact.model_validate_json(json.dumps(asset)) for asset in assets[:2] + selected)
        component["artifact_closure_sha256"] = artifact_closure_digest(closure)
        component["tokenizer_sha256"] = artifact_closure_digest(())
        component.update(peak_memory_bytes=MAX_SAFE_INTEGER, inference_threads=MAX_SAFE_INTEGER)
        document["bindings"][index]["component_id"] = component["component_id"]
    fill_identifiers(document["aggregation"])
    fill_identifiers(document["presentation"])
    document["profile_id"] = identifier("profile.")
    document["revision"] = identifier("revision.")
    document["resources"] = dict(download_bytes=MAX_SAFE_INTEGER, installed_bytes=MAX_SAFE_INTEGER,
                                 rollback_bytes=0, temporary_bytes=MAX_SAFE_INTEGER,
                                 transition_peak_memory_bytes=MAX_SAFE_INTEGER, inference_threads=MAX_SAFE_INTEGER)
    return AnalysisProfile.model_validate_json(json.dumps(document))


def test_structurally_maximal_unicode_labels_and_artifacts_fit_frozen_limit():
    value = maximal_profile()
    raw = canonical_bytes(value)
    assert 6 * 1024 * 1024 < len(raw) < authentication.MAX_PROFILE_BYTES == 8 * 1024 * 1024
    assert len(value.artifacts) == 256
    assert all(len(component.artifact_ids) == len(component.executed_labels) == 128 for component in value.components)
    assert all(len(component.tasks[0].enabled_ids) == 64 for component in value.components)
    policy = ResourcePolicy(download_bytes=MAX_SAFE_INTEGER, installed_bytes=MAX_SAFE_INTEGER,
                            temporary_bytes=MAX_SAFE_INTEGER, transition_peak_memory_bytes=MAX_SAFE_INTEGER,
                            inference_threads=MAX_SAFE_INTEGER)
    assert authenticate(value, resource_policy=policy).profile_sha256 == hashlib.sha256(raw).hexdigest()


def test_non_ascii_nlp_identity_remains_ascii_and_signing_domain_remains_utf8():
    document = profile().model_dump(mode="json")
    document["components"][0]["executed_labels"][0]["description"] = "é😀"
    value = AnalysisProfile.model_validate_json(json.dumps(document))
    raw = canonical_bytes(value)
    assert b"\\u00e9\\ud83d\\ude00" in raw
    assert authenticate(value).profile_sha256 == hashlib.sha256(raw).hexdigest()
    utf8 = json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authenticate(value, raw=utf8)
    domain_payload = {"label": "é😀", "integer": 1.0}
    domain_raw = publications.canonical(domain_payload)
    verified = publications.signature(sign(domain_raw, typ="example+jws").decode(), "example+jws", keys(), 4096)
    assert verified.payload == {"label": "é😀", "integer": 1}
    with pytest.raises(publications.PublicationRejected):
        publications.signature(sign(json.dumps(domain_payload, ensure_ascii=True).encode(), typ="example+jws").decode(),
                               "example+jws", keys(), 4096)


def test_overbound_envelope_is_refused_before_verifier(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("overbound envelope reached verifier")

    monkeypatch.setattr(signed_objects, "verify_compact", forbidden)
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(b"x" * (authentication.MAX_ENVELOPE_BYTES + 1),
                                            trusted_nlp_keys=keys(), expected_profile_sha256="a" * 64)


def test_overbound_decoded_payload_is_refused_before_payload_validator(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("overbound payload reached validator")

    monkeypatch.setattr(authentication, "_profile", forbidden)
    compact = sign(b"x" * (authentication.MAX_PROFILE_BYTES + 1))
    assert len(compact) < authentication.MAX_ENVELOPE_BYTES
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(compact, trusted_nlp_keys=keys(), expected_profile_sha256="a" * 64)


def test_rejection_errors_do_not_retain_payloads_in_rendered_tracebacks():
    marker = "EXAMPLE_REJECTED_PAYLOAD_9284"
    with pytest.raises(authentication.ProfileAuthenticationRejected) as caught:
        authenticate(raw=json.dumps({"invalid": marker}).encode())
    assert marker not in "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "NLP profile authentication failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_authority_inputs_have_no_ambient_defaults():
    compact = sign(canonical_bytes(profile()))
    with pytest.raises(TypeError):
        authentication.authenticate_profile(compact)
