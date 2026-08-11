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

from app.security.grant_verifier import (
    GrantVerificationContext,
    load_pinned_trust_set,
    verify_grant,
)


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "grant-profile-v1"


def _cases() -> list[Path]:
    return sorted(
        case
        for grant_type in CONTRACTS.iterdir()
        if grant_type.is_dir() and grant_type.name != "keys"
        for case in grant_type.iterdir()
        if case.is_dir()
    )


def _context(data: dict[str, Any]) -> GrantVerificationContext:
    return GrantVerificationContext(
        expected_grant_type=data["expected_grant_type"],
        expected_audience=data["expected_audience"],
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_subject=data["expected_subject"],
        verifier_time=data["fixed_verifier_time"],
        tombstones=data["tombstones"],
        requested_operation=data["requested_operation"],
        requested_creator_account_ids=data["requested_creator_account_ids"],
    )


@pytest.mark.contract_integrity
@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case.relative_to(CONTRACTS)))
def test_grant_profile_vectors_match_expected_outcomes(case: Path) -> None:
    context = _context(json.loads((case / "verification-context.json").read_text(encoding="utf-8")))
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    trust_set = load_pinned_trust_set("grant-profile-v1/keys/trust-set.json", environment="development")

    actual = verify_grant(
        (case / "token.jws").read_text(encoding="ascii").strip(),
        context=context,
        trust_set=trust_set,
    )

    assert asdict(actual) == expected


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def signed_membership() -> tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey]:
    case = CONTRACTS / "membership_snapshot" / "valid-current"
    context = _context(json.loads((case / "verification-context.json").read_text(encoding="utf-8")))
    payload = json.loads((case / "payload.json").read_text(encoding="utf-8"))
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_numbers()
    jwk = {"crv": "P-256", "kty": "EC", "x": _b64u(public.x.to_bytes(32, "big")), "y": _b64u(public.y.to_bytes(32, "big"))}
    thumbprint_bytes = __import__("hashlib").sha256(_json_bytes(jwk)).digest()
    thumbprint = _b64u(thumbprint_bytes)
    jwk["kid"] = f"bc1.ms.{_b64u(thumbprint_bytes[:16])}"
    trust_set = {"keys": [{"purpose": "membership", "jwk": jwk, "thumbprint": thumbprint}]}
    return context, payload, trust_set, key


def _token(
    payload: dict[str, Any],
    trust_set: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    *,
    header_bytes: bytes | None = None,
    payload_bytes: bytes | None = None,
) -> str:
    header = {"alg": "ES256", "kid": trust_set["keys"][0]["jwk"]["kid"], "typ": "urn:bridge-clean:grant:membership:v1"}
    header_segment = _b64u(header_bytes if header_bytes is not None else _json_bytes(header))
    payload_segment = _b64u(payload_bytes if payload_bytes is not None else _json_bytes(payload))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    if s > int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16) // 2:
        s = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16) - s
    return f"{header_segment}.{payload_segment}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class _ShortLengthToken(str):
    def __len__(self) -> int:
        return 0


def _oversize_payload_token() -> str:
    return _ShortLengthToken(f"{_b64u(b'{}')}.{_b64u(b'x' * 12289)}.e30")


def _result(token: str, signed_membership: tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey], *, context: GrantVerificationContext | None = None, trust_set: dict[str, Any] | None = None) -> str:
    default_context, _, default_trust_set, _ = signed_membership
    return verify_grant(token, context=context or default_context, trust_set=trust_set or default_trust_set).result


@pytest.mark.parametrize(
    ("name", "make", "expected"),
    [
        ("invalid_compact_jws", lambda payload, trust, key: "two.segments", "invalid_compact_jws"),
        ("header_too_large", lambda payload, trust, key: "a" * 513 + ".x.x", "header_too_large"),
        ("noncanonical_base64url", lambda payload, trust, key: "=.e30.e30", "noncanonical_base64url"),
        ("payload_too_large", lambda payload, trust, key: _oversize_payload_token(), "payload_too_large"),
        ("duplicate_json_member", lambda payload, trust, key: _token(payload, trust, key, header_bytes=b'{\"alg\":\"ES256\",\"alg\":\"ES256\",\"kid\":\"x\",\"typ\":\"x\"}'), "duplicate_json_member"),
        ("invalid_json", lambda payload, trust, key: _token(payload, trust, key, header_bytes=b"{"), "invalid_json"),
        ("unsupported_json_value", lambda payload, trust, key: _token(payload, trust, key, payload_bytes=b'{\"x\":null}'), "unsupported_json_value"),
        ("noncanonical_header", lambda payload, trust, key: _token(payload, trust, key, header_bytes=(f'{{\"typ\":\"urn:bridge-clean:grant:membership:v1\",\"kid\":\"{trust["keys"][0]["jwk"]["kid"]}\",\"alg\":\"ES256\"}}').encode()), "noncanonical_header"),
        ("invalid_header", lambda payload, trust, key: _token(payload, trust, key, header_bytes=_json_bytes({"alg": "none", "kid": trust["keys"][0]["jwk"]["kid"], "typ": "urn:bridge-clean:grant:membership:v1"})), "invalid_header"),
    ],
)
def test_unvectored_pre_signature_result_codes(
    name: str, make: Any, expected: str, signed_membership: tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey]
) -> None:
    _, payload, trust_set, key = signed_membership
    assert _result(make(payload, trust_set, key), signed_membership) == expected, name


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        ("schema_invalid", lambda payload: payload.update({"extra": True}), "schema_invalid"),
        ("issuer_or_profile_mismatch", lambda payload: payload.update({"iss": "urn:other"}), "issuer_or_profile_mismatch"),
        ("invalid_jti", lambda payload: payload.update({"jti": "bad"}), "invalid_jti"),
        ("invalid_numeric_date", lambda payload: payload.update({"iat": True}), "invalid_numeric_date"),
        ("invalid_time_contract", lambda payload: payload.update({"exp": payload["exp"] + 1}), "invalid_time_contract"),
        ("organization_mismatch", lambda payload: payload.update({"organization_id": "other"}), "organization_mismatch"),
        ("installation_key_mismatch", lambda payload: payload.update({"installation_key_jkt": "other"}), "installation_key_mismatch"),
        ("unknown_role", lambda payload: payload.update({"roles": ["unknown"], "scopes": []}), "unknown_role"),
    ],
)
def test_unvectored_signed_result_codes(
    name: str, mutate: Any, expected: str, signed_membership: tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey]
) -> None:
    _, original, trust_set, key = signed_membership
    payload = dict(original)
    mutate(payload)
    assert _result(_token(payload, trust_set, key), signed_membership) == expected, name


def test_unvectored_invalid_trust_set_result_code(signed_membership: tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey]) -> None:
    _, payload, trust_set, key = signed_membership
    assert _result(_token(payload, trust_set, key), signed_membership, trust_set={"keys": []}) == "invalid_trust_set"


def test_unvectored_unsupported_grant_type_result_code(signed_membership: tuple[GrantVerificationContext, dict[str, Any], dict[str, Any], ec.EllipticCurvePrivateKey]) -> None:
    context, payload, trust_set, key = signed_membership
    assert _result(_token(payload, trust_set, key), signed_membership, context=replace(context, expected_grant_type="unknown")) == "unsupported_grant_type"
