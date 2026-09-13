from __future__ import annotations

import base64
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import app.security.capability_license_verifier as license_verifier
from app.security.capability_license_verifier import (
    CapabilityLicenseTrustUnavailable,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
    PackagedCapabilityLicenseTrustProvider,
    _trusted_keys,
    verify_capability_license,
)
from contracts.loader import ContractsIntegrityError


ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "contracts" / "capability-license-v1"
PRODUCTION_TRUST = ROOT / "contracts" / "production" / "capability-license-v1" / "trust-set.json"
PRODUCTION_TRUST_SHA256 = "935cd51fe510065d6dc7d29ce481324e7ddf2be1a5431151bc74efc4f45a9173"
PRODUCTION_KID = "bc1.cl.K4cd-bA7f90euaJ7M2eGWw"
PRODUCTION_THUMBPRINT = "K4cd-bA7f90euaJ7M2eGWykHjyAZcAmgGeohXmSYQas"
PRODUCTION_X = "jeTAAwhgyOuW-h7SxOkpoUpbGTNCj1OC2MBLyaKeWv8"
PRODUCTION_Y = "Ev3gKDs0BrvyBfWnPkZNUapGAKgE2UkO0E-XMiJqrCY"
PRODUCTION_NOT_BEFORE = 1_789_334_592
P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)


def _context(data: dict[str, Any]) -> CapabilityLicenseVerificationContext:
    subject = data["expected_subject"]
    seat_id = subject.split(":seat:", 1)[1].split(":capability:", 1)[0]
    return CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_seat_id=seat_id,
        expected_seat_scope=data["expected_seat_scope"],
        expected_capability=data["expected_capability"],
        expected_target_major=data["expected_target_major"],
        expected_artifact_family=data["expected_artifact_family"],
        requested_update_mode=data["requested_update_mode"],
        expected_fallback_major=data.get("expected_fallback_major"),
    )


def _fixture_trust() -> dict[str, Any]:
    return dict(FixtureCapabilityLicenseTrustProvider().trust_set())


def _license_vector_cases() -> list[Path]:
    cases: list[Path] = []
    for case in sorted(path for path in VECTORS.iterdir() if path.is_dir()):
        verifier = json.loads((case / "verifier.json").read_text("utf-8"))
        if verifier["verifier"] == "license":
            cases.append(case)
    return cases


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _public_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64u(numbers.x.to_bytes(32, "big")),
        "y": _b64u(numbers.y.to_bytes(32, "big")),
    }


def _identity(jwk: dict[str, str]) -> tuple[str, str]:
    material = _canonical({name: jwk[name] for name in ("crv", "kty", "x", "y")})
    digest = hashlib.sha256(material).digest()
    return f"bc1.cl.{_b64u(digest[:16])}", _b64u(digest)


def _trust_entry(
    key: ec.EllipticCurvePrivateKey,
    *,
    state: str,
    not_before: int,
    not_after: int | None,
) -> dict[str, object]:
    public = _public_jwk(key)
    kid, thumbprint = _identity(public)
    return {
        "purpose": "capability-license",
        "state": state,
        "kid": kid,
        "jwk": {**public, "kid": kid},
        "thumbprint": thumbprint,
        "not_before": not_before,
        "not_after": not_after,
    }


def _trust(*entries: dict[str, object], overlap: int = 600) -> dict[str, object]:
    return {
        "algorithm": "ES256",
        "backend_type": "synthetic-local-test",
        "curve": "P-256",
        "custody": "protected-non-exportable",
        "environment": "production",
        "keys": list(entries),
        "max_retiring_overlap_seconds": overlap,
        "production_usable": True,
        "profile": "urn:bridge-clean:capability-license-trust:v1",
        "purpose": "capability-license",
        "transition_sequence": 1,
    }


def _accepted_payload(*, iat: int) -> dict[str, Any]:
    payload = json.loads((VECTORS / "accepted" / "payload.json").read_text("utf-8"))
    payload["iat"] = iat
    return payload


def _accepted_context() -> CapabilityLicenseVerificationContext:
    return _context(
        json.loads((VECTORS / "accepted" / "verification-context.json").read_text("utf-8"))
    )


def _sign(key: ec.EllipticCurvePrivateKey, *, iat: int) -> str:
    public = _public_jwk(key)
    kid, _ = _identity(public)
    header = {
        "alg": "ES256",
        "kid": kid,
        "typ": "urn:bridge-clean:capability-license:v1",
    }
    header_segment = _b64u(_canonical(header))
    payload_segment = _b64u(_canonical(_accepted_payload(iat=iat)))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    if s > P256_ORDER // 2:
        s = P256_ORDER - s
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header_segment}.{payload_segment}.{_b64u(signature)}"


def _verify(token: str, trust: dict[str, object]) -> str:
    return verify_capability_license(token, context=_accepted_context(), trust_set=trust).result


@pytest.mark.parametrize("case", _license_vector_cases(), ids=lambda path: path.name)
def test_published_capability_license_vectors(case: Path) -> None:
    token = (case / "token.jws").read_text("ascii").strip()
    context = _context(json.loads((case / "verification-context.json").read_text("utf-8")))
    expected = json.loads((case / "expected.json").read_text("utf-8"))

    result = verify_capability_license(token, context=context, trust_set=_fixture_trust())

    assert result.valid is expected["valid"]
    assert result.result == expected["result"]
    if result.valid:
        assert result.license is not None
        assert result.license.object_digest
        assert result.license.compact_jws == token
        assert result.license.capability == "analysis-run"
    else:
        assert result.license is None


def test_fixture_provider_is_explicitly_development_only() -> None:
    with pytest.raises(CapabilityLicenseTrustUnavailable, match="development-only"):
        FixtureCapabilityLicenseTrustProvider(environment="production")


def test_production_verifier_uses_packaged_production_trust() -> None:
    verifier = CapabilityLicenseVerifier.production()
    assert isinstance(verifier, CapabilityLicenseVerifier)
    trust = PackagedCapabilityLicenseTrustProvider().trust_set()
    assert trust["profile"] == "urn:bridge-clean:capability-license-trust:v1"
    assert trust["production_usable"] is True


def test_production_verifier_fails_closed_when_packaged_trust_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> dict[str, object]:
        raise ContractsIntegrityError("missing vendored file")

    monkeypatch.setattr(license_verifier, "load_trust_set", unavailable)
    with pytest.raises(CapabilityLicenseTrustUnavailable, match="production CapabilityLicense trust"):
        CapabilityLicenseVerifier.production()


def test_production_provider_never_falls_back_to_fixture_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture_trust()
    monkeypatch.setattr(license_verifier, "load_trust_set", lambda *args, **kwargs: fixture)
    with pytest.raises(CapabilityLicenseTrustUnavailable):
        PackagedCapabilityLicenseTrustProvider().trust_set()


def test_packaged_sequence_1_production_authority_is_exact_and_independently_derived() -> None:
    raw = PRODUCTION_TRUST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PRODUCTION_TRUST_SHA256
    artifact = json.loads(raw)
    assert set(artifact) == {
        "algorithm", "backend_type", "curve", "custody", "environment", "keys",
        "max_retiring_overlap_seconds", "production_usable", "profile", "purpose",
        "transition_sequence",
    }
    assert artifact["profile"] == "urn:bridge-clean:capability-license-trust:v1"
    assert artifact["environment"] == "production"
    assert artifact["production_usable"] is True
    assert artifact["purpose"] == "capability-license"
    assert artifact["algorithm"] == "ES256"
    assert artifact["curve"] == "P-256"
    assert artifact["custody"] == "protected-non-exportable"
    assert artifact["backend_type"] == "azure-key-vault-premium-ec-hsm"
    assert artifact["transition_sequence"] == 1
    assert 1 <= len(artifact["keys"]) <= 8
    assert sum(key["state"] == "active" for key in artifact["keys"]) == 1

    key = artifact["keys"][0]
    assert set(key) == {"purpose", "state", "kid", "jwk", "thumbprint", "not_before", "not_after"}
    assert key["purpose"] == "capability-license"
    assert key["state"] == "active"
    assert key["kid"] == PRODUCTION_KID
    assert key["thumbprint"] == PRODUCTION_THUMBPRINT
    assert key["not_before"] == PRODUCTION_NOT_BEFORE
    assert key["not_after"] is None
    assert set(key["jwk"]) == {"crv", "kid", "kty", "x", "y"}
    assert key["jwk"] == {
        "crv": "P-256", "kid": PRODUCTION_KID, "kty": "EC",
        "x": PRODUCTION_X, "y": PRODUCTION_Y,
    }
    assert not ({"d", "p", "q", "dp", "dq", "qi", "oth"} & set(key["jwk"]))

    derived_kid, derived_thumbprint = _identity({
        name: key["jwk"][name] for name in ("crv", "kty", "x", "y")
    })
    assert derived_kid == key["kid"] == key["jwk"]["kid"] == PRODUCTION_KID
    assert derived_thumbprint == key["thumbprint"] == PRODUCTION_THUMBPRINT
    _trusted_keys(artifact)


def test_active_key_and_signed_iat_inside_window_is_accepted() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    trust = _trust(_trust_entry(key, state="active", not_before=1_000, not_after=None))
    assert _verify(_sign(key, iat=1_001), trust) == "accepted"


def test_retiring_and_historical_keys_preserve_old_licenses_by_signed_iat() -> None:
    old = ec.generate_private_key(ec.SECP256R1())
    active = ec.generate_private_key(ec.SECP256R1())
    retiring = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        _trust_entry(old, state="retiring", not_before=800, not_after=1_100),
    )
    historical = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        _trust_entry(old, state="historical", not_before=800, not_after=1_100),
    )
    old_token = _sign(old, iat=900)
    assert _verify(old_token, retiring) == "accepted"
    assert _verify(old_token, historical) == "accepted"


def test_issuance_window_is_inclusive_and_not_wall_clock_based(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = ec.generate_private_key(ec.SECP256R1())
    active = ec.generate_private_key(ec.SECP256R1())
    trust = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        _trust_entry(old, state="historical", not_before=800, not_after=1_100),
    )
    at_cutoff = _sign(old, iat=1_100)
    monkeypatch.setattr(time, "time", lambda: 10.0)
    assert _verify(at_cutoff, trust) == "accepted"
    monkeypatch.setattr(time, "time", lambda: 4_000_000_000.0)
    assert _verify(at_cutoff, trust) == "accepted"
    assert _verify(_sign(old, iat=1_101), trust) == "issuance_after_key_not_after"
    assert _verify(_sign(old, iat=799), trust) == "issuance_before_key_not_before"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda doc: doc["keys"][0].__setitem__("state", "compromised"), "state"),
        (lambda doc: doc["keys"][0].__setitem__("not_after", 1_100), "active .*not_after"),
        (lambda doc: doc["keys"][1].__setitem__("not_after", None), "historical .*not_after"),
        (lambda doc: doc["keys"][1].__setitem__("not_after", 700), "historical .*not_after"),
        (lambda doc: doc["keys"][1].update(state="retiring", not_after=None), "retiring .*not_after"),
        (lambda doc: doc["keys"][1].update(state="retiring", not_after=700), "retiring .*not_after"),
        (lambda doc: doc["keys"][0].__setitem__("purpose", "membership"), "wrong-purpose"),
        (lambda doc: doc["keys"][0].__setitem__("thumbprint", "A" * 43), "identity mismatch"),
        (lambda doc: doc["keys"][0].__setitem__("kid", "bc1.cl.AAAAAAAAAAAAAAAAAAAAAA"), "identity mismatch"),
        (lambda doc: doc["keys"][0]["jwk"].__setitem__("d", "A" * 43), "JWK"),
        (lambda doc: doc["keys"][0]["jwk"].__setitem__("x", "A" * 43), "trust set"),
    ],
)
def test_production_trust_rejects_malformed_state_identity_and_jwk(
    mutate: Any, match: str
) -> None:
    active = ec.generate_private_key(ec.SECP256R1())
    old = ec.generate_private_key(ec.SECP256R1())
    doc = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        _trust_entry(old, state="historical", not_before=800, not_after=900),
    )
    mutate(doc)
    with pytest.raises(ValueError, match=match):
        _trusted_keys(doc)


def test_production_trust_accepts_eight_keys_and_rejects_nine() -> None:
    active = ec.generate_private_key(ec.SECP256R1())
    historical = [ec.generate_private_key(ec.SECP256R1()) for _ in range(8)]
    eight = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        *(
            _trust_entry(key, state="historical", not_before=100, not_after=900)
            for key in historical[:7]
        ),
    )
    assert len(_trusted_keys(eight)) == 8
    nine = _trust(
        _trust_entry(active, state="active", not_before=1_000, not_after=None),
        *(
            _trust_entry(key, state="historical", not_before=100, not_after=900)
            for key in historical
        ),
    )
    with pytest.raises(ValueError, match="key count"):
        _trusted_keys(nine)


def test_production_trust_rejects_duplicate_kid_and_duplicate_public_key() -> None:
    active = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    first = _trust_entry(active, state="active", not_before=1_000, not_after=None)
    second = _trust_entry(other, state="historical", not_before=100, not_after=900)

    duplicate_kid = _trust(copy.deepcopy(first), copy.deepcopy(second))
    duplicate_kid["keys"][1]["kid"] = first["kid"]
    with pytest.raises(ValueError, match="duplicate .*kid"):
        _trusted_keys(duplicate_kid)

    duplicate_public = _trust(copy.deepcopy(first), copy.deepcopy(first))
    duplicate_public["keys"][1]["state"] = "historical"
    duplicate_public["keys"][1]["not_before"] = 100
    duplicate_public["keys"][1]["not_after"] = 900
    with pytest.raises(ValueError, match="duplicate .*kid|duplicate .*public key"):
        _trusted_keys(duplicate_public)


def test_fixture_and_cross_purpose_keys_are_not_production_authority() -> None:
    production = json.loads(PRODUCTION_TRUST.read_text("utf-8"))
    accepted = VECTORS / "accepted"
    fixture_token = (accepted / "token.jws").read_text("ascii").strip()
    fixture_context = _context(json.loads((accepted / "verification-context.json").read_text("utf-8")))
    result = verify_capability_license(fixture_token, context=fixture_context, trust_set=production)
    assert result.valid is False
    assert result.result == "unknown_kid"

    permit_token = (VECTORS / "permit-presented-to-license-verifier" / "token.jws").read_text("ascii").strip()
    permit_context = _context(json.loads((VECTORS / "permit-presented-to-license-verifier" / "verification-context.json").read_text("utf-8")))
    permit = verify_capability_license(permit_token, context=permit_context, trust_set=production)
    assert permit.valid is False
    assert permit.result in {"typ_mismatch", "unknown_kid"}

    for purpose in ("installation-binding", "membership", "license", "capability-permit"):
        key = ec.generate_private_key(ec.SECP256R1())
        entry = _trust_entry(key, state="active", not_before=1_000, not_after=None)
        entry["purpose"] = purpose
        with pytest.raises(ValueError, match="wrong-purpose"):
            _trusted_keys(_trust(entry))

    operational = json.loads(
        (ROOT / "contracts" / "production" / "grant-profile-v1" / "trust-set.json").read_text("utf-8")
    )
    permit_trust = json.loads((ROOT / "contracts" / "capability-permit-v1" / "trust-set.json").read_text("utf-8"))
    for source in (*operational["keys"], *permit_trust["keys"]):
        jwk = {name: source["jwk"][name] for name in ("crv", "kty", "x", "y")}
        kid, thumbprint = _identity(jwk)
        entry = {
            "purpose": source["purpose"],
            "state": "active",
            "kid": kid,
            "jwk": {**jwk, "kid": kid},
            "thumbprint": thumbprint,
            "not_before": 1_000,
            "not_after": None,
        }
        with pytest.raises(ValueError, match="wrong-purpose"):
            _trusted_keys(_trust(entry))

    unknown = ec.generate_private_key(ec.SECP256R1())
    unknown_result = verify_capability_license(
        _sign(unknown, iat=PRODUCTION_NOT_BEFORE),
        context=_accepted_context(),
        trust_set=production,
    )
    assert unknown_result.valid is False
    assert unknown_result.result == "unknown_kid"


def test_verified_license_repr_does_not_expose_compact_jws() -> None:
    case = VECTORS / "accepted"
    token = (case / "token.jws").read_text("ascii").strip()
    result = verify_capability_license(
        token,
        context=_context(json.loads((case / "verification-context.json").read_text("utf-8"))),
        trust_set=_fixture_trust(),
    )
    assert result.license is not None
    assert token not in repr(result.license)
