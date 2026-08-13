from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.security import capability_permit_verifier as permit_verifier
from app.security.capability_permit_verifier import (
    CapabilityPermitVerificationContext,
    _verify_signature,
    load_pinned_trust_set,
    verify_capability_permit,
)
from contracts.loader import ContractsIntegrityError


pytestmark = pytest.mark.contract_integrity

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-permit-v1"
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)


def _cases() -> list[Path]:
    return sorted(path for path in VECTORS.iterdir() if path.is_dir())


def _context(data: dict[str, Any]) -> CapabilityPermitVerificationContext:
    return CapabilityPermitVerificationContext(
        expected_subject=data["expected_subject"],
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_capability=data["expected_capability"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
    )


@pytest.fixture(scope="module")
def fixture_trust_set() -> dict[str, Any]:
    return load_pinned_trust_set("capability-permit-v1/trust-set.json", environment="development")


@pytest.fixture(scope="module")
def valid_payload() -> dict[str, Any]:
    return json.loads((VECTORS / "valid" / "payload.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def valid_context() -> CapabilityPermitVerificationContext:
    data = json.loads((VECTORS / "valid" / "verification-context.json").read_text(encoding="utf-8"))
    return _context(data)


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.name)
def test_capability_permit_vectors_match_expected_outcomes(
    case: Path, fixture_trust_set: dict[str, Any]
) -> None:
    context = _context(
        json.loads((case / "verification-context.json").read_text(encoding="utf-8"))
    )
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))

    actual = verify_capability_permit(
        (case / "token.jws").read_text(encoding="ascii").strip(),
        context=context,
        trust_set=fixture_trust_set,
    )

    assert asdict(actual) == expected


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def signing_material() -> tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public = private_key.public_key().public_numbers()
    jwk = {
        "crv": "P-256",
        "kid": "test-capability-permit-key",
        "kty": "EC",
        "x": _b64u(public.x.to_bytes(32, "big")),
        "y": _b64u(public.y.to_bytes(32, "big")),
    }
    return private_key, {"keys": [{"purpose": "capability-permit", "jwk": jwk}]}


def _token(
    payload: dict[str, Any],
    private_key: ec.EllipticCurvePrivateKey,
    trust_set: dict[str, Any],
    *,
    header: dict[str, Any] | None = None,
    header_bytes: bytes | None = None,
    signature: bytes | None = None,
) -> str:
    protected = header or {
        "alg": "ES256",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:capability-permit:v1",
    }
    header_segment = _b64u(header_bytes if header_bytes is not None else _json(protected))
    payload_segment = _b64u(_json(payload))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    if signature is None:
        r, s = decode_dss_signature(private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        if s > _P256_ORDER // 2:
            s = _P256_ORDER - s
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header_segment}.{payload_segment}.{_b64u(signature)}"


def _result(
    token: str,
    valid_context: CapabilityPermitVerificationContext,
    trust_set: dict[str, Any],
) -> str:
    return verify_capability_permit(token, context=valid_context, trust_set=trust_set).result


def test_invalid_compact_jws_result(
    valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    _, trust_set = signing_material

    assert _result("two.segments", valid_context, trust_set) == "invalid_compact_jws"


def test_noncanonical_json_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header_bytes = (
        f'{{"typ":"urn:bridge-clean:capability-permit:v1","kid":"{trust_set["keys"][0]["jwk"]["kid"]}","alg":"ES256"}}'
    ).encode("utf-8")

    assert _result(_token(valid_payload, private_key, trust_set, header_bytes=header_bytes), valid_context, trust_set) == "noncanonical_json"


def test_canonical_json_precedes_header_validation(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header = {
        "alg": "none",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:capability-permit:v1",
    }
    header_bytes = json.dumps(header).encode("utf-8")

    assert _result(_token(valid_payload, private_key, trust_set, header_bytes=header_bytes), valid_context, trust_set) == "noncanonical_json"


def test_padded_base64url_segment_is_invalid_compact_jws(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header_segment, payload_segment, signature_segment = _token(valid_payload, private_key, trust_set).split(".")
    padded_token = f"{header_segment}=.{payload_segment}.{signature_segment}"

    assert _result(padded_token, valid_context, trust_set) == "invalid_compact_jws"


def test_non_minimal_base64url_segment_is_invalid_compact_jws(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header_segment, payload_segment, signature_segment = _token(valid_payload, private_key, trust_set).split(".")
    # One extra base64url character makes the segment an impossible base64url
    # quantum length (non-minimal / wrong-length encoding), not merely a
    # noncanonical-but-decodable one.
    wrong_length_token = f"{header_segment}A.{payload_segment}.{signature_segment}"

    assert _result(wrong_length_token, valid_context, trust_set) == "invalid_compact_jws"


def test_non_json_segment_is_invalid_compact_jws(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    _, payload_segment, signature_segment = _token(valid_payload, private_key, trust_set).split(".")
    not_json_segment = _b64u(b"not-json-bytes-but-valid-base64url")
    token = f"{not_json_segment}.{payload_segment}.{signature_segment}"

    assert _result(token, valid_context, trust_set) == "invalid_compact_jws"


@pytest.mark.parametrize(
    "malformed_token",
    ["no-separators-at-all", "a.b.c.d"],
    ids=["zero_dots", "four_segments"],
)
def test_wrong_separator_count_is_invalid_compact_jws(
    malformed_token: str, valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    _, trust_set = signing_material

    assert _result(malformed_token, valid_context, trust_set) == "invalid_compact_jws"


def test_noncanonical_payload_json_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header = {
        "alg": "ES256",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:capability-permit:v1",
    }
    payload_bytes = json.dumps(valid_payload, sort_keys=True, separators=(", ", ": ")).encode("utf-8")
    header_segment = _b64u(_json(header))
    payload_segment = _b64u(payload_bytes)
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    r, s = decode_dss_signature(private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    if s > _P256_ORDER // 2:
        s = _P256_ORDER - s
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = f"{header_segment}.{payload_segment}.{_b64u(signature)}"

    assert _result(token, valid_context, trust_set) == "noncanonical_json"


def test_invalid_header_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header = {
        "alg": "none",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:capability-permit:v1",
    }

    assert _result(_token(valid_payload, private_key, trust_set, header=header), valid_context, trust_set) == "invalid_header"


def test_header_validation_precedes_typ_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header = {
        "alg": "none",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:other",
    }

    assert _result(_token(valid_payload, private_key, trust_set, header=header), valid_context, trust_set) == "invalid_header"


def test_typ_and_audience_precede_signature_verification(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "aud": "urn:bridge-clean:other"}
    header = {
        "alg": "ES256",
        "kid": trust_set["keys"][0]["jwk"]["kid"],
        "typ": "urn:bridge-clean:other",
    }

    assert _result(_token(payload, private_key, trust_set, header=header, signature=b"\0" * 64), valid_context, trust_set) == "typ_mismatch"


def test_audience_precedes_key_lookup(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "aud": "urn:bridge-clean:other"}
    header = {
        "alg": "ES256",
        "kid": "unlisted-key",
        "typ": "urn:bridge-clean:capability-permit:v1",
    }

    assert _result(_token(payload, private_key, trust_set, header=header), valid_context, trust_set) == "audience_mismatch"


def test_unknown_kid_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    header = {
        "alg": "ES256",
        "kid": "unlisted-key",
        "typ": "urn:bridge-clean:capability-permit:v1",
    }

    assert _result(_token(valid_payload, private_key, trust_set, header=header), valid_context, trust_set) == "unknown_kid"


def test_key_purpose_precedes_signature_verification(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    license_trust_set = {"keys": [{**trust_set["keys"][0], "purpose": "license"}]}
    token = _token(valid_payload, private_key, license_trust_set)
    _, _, signature_segment = token.split(".")
    signature = _b64u_decode(signature_segment)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    assert s <= _P256_ORDER // 2
    high_s_signature = r.to_bytes(32, "big") + (_P256_ORDER - s).to_bytes(32, "big")

    assert _result(_token(valid_payload, private_key, license_trust_set, signature=high_s_signature), valid_context, license_trust_set) == "wrong_key_purpose"


def test_schema_invalid_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "extra": True}

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "schema_invalid"


def test_signature_crypto_precedes_schema_validation(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "extra": True}

    assert _result(_token(payload, private_key, trust_set, signature=b"\0" * 64), valid_context, trust_set) == "invalid_signature"


def test_schema_validation_precedes_subject_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {
        **valid_payload,
        "extra": True,
        "sub": "organization:other-organization:installation:other-installation:capability:analysis-run",
    }

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "schema_invalid"


def test_installation_key_mismatch_result(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "installation_key_jkt": "B" * 43}

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "installation_key_mismatch"


def test_high_s_signature_is_rejected(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    token = _token(valid_payload, private_key, trust_set)
    header_segment, payload_segment, signature_segment = token.split(".")
    signature = _b64u_decode(signature_segment)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    assert s <= _P256_ORDER // 2
    high_s_signature = r.to_bytes(32, "big") + (_P256_ORDER - s).to_bytes(32, "big")
    high_s_token = f"{header_segment}.{payload_segment}.{_b64u(high_s_signature)}"

    assert _result(high_s_token, valid_context, trust_set) == "invalid_signature"


def test_malformed_signature_length_is_rejected(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    token = _token(valid_payload, private_key, trust_set)
    header_segment, payload_segment, signature_segment = token.split(".")
    signature = _b64u_decode(signature_segment)
    malformed_token = f"{header_segment}.{payload_segment}.{_b64u(signature[:-1])}"

    assert _result(malformed_token, valid_context, trust_set) == "invalid_signature"


def test_signature_shape_precedes_signature_crypto(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key, trust_set = signing_material
    calls = 0
    verify_signature = permit_verifier._verify_signature

    def counted_verify_signature(
        public_key: ec.EllipticCurvePublicKey, signing_input: bytes, signature: bytes
    ) -> bool:
        nonlocal calls
        calls += 1
        return verify_signature(public_key, signing_input, signature)

    monkeypatch.setattr(permit_verifier, "_verify_signature", counted_verify_signature)

    assert _result(_token(valid_payload, private_key, trust_set, signature=b"\0" * 63), valid_context, trust_set) == "invalid_signature"
    assert calls == 0


def test_signature_helper_rejects_malformed_signature_length(
    signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, _ = signing_material

    assert not _verify_signature(private_key.public_key(), b"signing input", b"\0" * 63)


def test_subject_precedes_organization_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {
        **valid_payload,
        "sub": "organization:other-organization:installation:other-installation:capability:analysis-run",
        "organization_id": "other-organization",
    }

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "subject_mismatch"


def test_organization_precedes_installation_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {
        **valid_payload,
        "organization_id": "other-organization",
        "installation_id": "other-installation",
    }

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "organization_mismatch"


def test_installation_precedes_capability_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "installation_id": "other-installation"}
    context = replace(valid_context, expected_capability="other-capability")

    assert _result(_token(payload, private_key, trust_set), context, trust_set) == "installation_mismatch"


def test_capability_precedes_installation_key_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    context = replace(
        valid_context,
        expected_capability="other-capability",
        expected_installation_key_jkt="B" * 43,
    )

    assert _result(_token(valid_payload, private_key, trust_set), context, trust_set) == "capability_mismatch"


def test_catalog_version_is_not_a_local_binding(
    valid_payload: dict[str, Any], valid_context: CapabilityPermitVerificationContext, signing_material: tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]
) -> None:
    private_key, trust_set = signing_material
    payload = {**valid_payload, "catalog_version": "another-catalog"}

    assert _result(_token(payload, private_key, trust_set), valid_context, trust_set) == "accepted"


def test_fixture_trust_set_is_refused_outside_development_through_permit_loader() -> None:
    with pytest.raises(ContractsIntegrityError, match="trust set is not production usable"):
        load_pinned_trust_set("capability-permit-v1/trust-set.json", environment="production")
