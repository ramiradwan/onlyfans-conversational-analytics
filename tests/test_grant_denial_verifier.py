"""Independently signed denial cases; no compact tokens in assertion output."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.security.grant_verifier import (
    GrantDenialVerificationContext,
    verify_grant_denial,
)


_PROFILE = "urn:bridge-clean:grant-denial:v1"
_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_GRANTS = {
    "installation_grant": ("installation", "installation-binding", "ib", "revoked"),
    "creator_account_binding": ("creator-binding", "installation-binding", "ib", "approval_revoked"),
    "membership_snapshot": ("membership", "membership", "ms", "membership_removed"),
    "license_entitlement": ("license", "license", "le", "entitlement_inactive"),
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class _Signer:
    context: GrantDenialVerificationContext
    payload: dict[str, Any]
    trust: dict[str, Any]
    key: ec.EllipticCurvePrivateKey = field(repr=False)

    def token(
        self, *, payload: dict[str, Any] | None = None,
        header: dict[str, Any] | None = None, payload_bytes: bytes | None = None,
        high_s: bool = False, corrupt: bool = False,
    ) -> str:
        protected = header if header is not None else {
            "alg": "ES256", "kid": self.trust["keys"][0]["jwk"]["kid"], "typ": _PROFILE,
        }
        raw_payload = payload_bytes if payload_bytes is not None else _json(
            self.payload if payload is None else payload
        )
        encoded = f"{_b64(_json(protected))}.{_b64(raw_payload)}"
        r, s = decode_dss_signature(self.key.sign(encoded.encode(), ec.ECDSA(hashes.SHA256())))
        s = min(s, _ORDER - s)
        if high_s:
            s = _ORDER - s
        if corrupt:
            r ^= 1
        return f"{encoded}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _signer(grant_type: str = "membership_snapshot") -> _Signer:
    audience, purpose, code, reason = _GRANTS[grant_type]
    key = ec.generate_private_key(ec.SECP256R1())
    point = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": _b64(point.x.to_bytes(32, "big")),
           "y": _b64(point.y.to_bytes(32, "big"))}
    thumbprint = hashlib.sha256(_json(jwk)).digest()
    jwk["kid"] = f"bc1.{code}.{_b64(thumbprint[:16])}"
    trust = {"keys": [{"purpose": purpose, "jwk": jwk, "thumbprint": _b64(thumbprint)}]}
    context = GrantDenialVerificationContext(
        expected_grant_type=grant_type,
        expected_audience=f"urn:bridge-clean:local-brain:{audience}",
        expected_subject="synthetic-bound-subject",
        expected_revoked_jti="0198a1b2-c3d4-7400-8000-000000000001",
        verifier_time=1_000,
    )
    payload = {
        "profile": _PROFILE, "iss": "urn:bridge-clean:commercial-control-plane",
        "aud": context.expected_audience, "sub": context.expected_subject,
        "jti": "0198a1b2-c3d4-7400-8000-000000000002", "iat": 990, "nbf": 990,
        "exp": 1590, "grant_type": grant_type, "revoked_jti": context.expected_revoked_jti,
        "effective_at": 985, "reason_code": reason,
    }
    return _Signer(context, payload, trust, key)


@pytest.mark.parametrize("grant_type", list(_GRANTS))
def test_independent_purpose_signature_produces_only_verified_denial_metadata(grant_type: str) -> None:
    signer = _signer(grant_type)
    token = signer.token()
    result = verify_grant_denial(token, trust_set=signer.trust, context=signer.context)
    assert result.valid and result.result == "accepted"
    evidence = result.denial
    assert evidence is not None
    assert evidence.revoked_jti == signer.context.expected_revoked_jti
    assert evidence.grant_type == grant_type
    assert evidence.effective_at == 985
    assert evidence.evidence_sha256 == hashlib.sha256(token.encode()).hexdigest()
    payload_free = token not in repr(result) and token not in repr(evidence)
    assert payload_free


@pytest.mark.parametrize("field_name,value", [
    ("iss", "urn:untrusted:issuer"), ("profile", "urn:bridge-clean:grant-profile:v1"),
    ("aud", "urn:other:audience"), ("sub", "another-subject"),
    ("grant_type", "installation_grant"),
    ("revoked_jti", "0198a1b2-c3d4-7400-8000-000000000003"),
    ("jti", "0198a1b2-c3d4-4400-8000-000000000002"),
    ("jti", {"unexpected": "object"}), ("jti", "0198a1b2-c3d4-7400-8000-000000000001"),
    ("reason_code", "unbounded-sensitive-diagnostic"), ("reason_code", "approval_revoked"),
    ("reason_code", []), ("iat", True), ("nbf", 991), ("exp", 1591),
    ("effective_at", -1), ("effective_at", 1_001), ("effective_at", 9_007_199_254_740_992),
])
def test_wrong_binding_or_unclosed_claims_cannot_create_evidence(field_name: str, value: Any) -> None:
    signer = _signer()
    result = verify_grant_denial(
        signer.token(payload={**signer.payload, field_name: value}),
        trust_set=signer.trust, context=signer.context,
    )
    assert not result.valid and result.denial is None


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "whitespace", "float"])
def test_denial_json_is_closed_unique_and_canonical(mutation: str) -> None:
    signer = _signer()
    payload = dict(signer.payload)
    if mutation == "missing":
        del payload["effective_at"]
    elif mutation == "extra":
        payload["organization_id"] = "not-in-denial-contract"
    elif mutation == "float":
        payload["iat"] = 990.0
    raw = _json(payload)
    if mutation == "duplicate":
        raw = raw.replace(b'{"aud":', b'{"aud":"shadow","aud":', 1)
    elif mutation == "whitespace":
        raw = b" " + raw
    result = verify_grant_denial(signer.token(payload_bytes=raw), trust_set=signer.trust, context=signer.context)
    assert not result.valid and result.denial is None


@pytest.mark.parametrize("mutation", ["high-s", "signature", "typ", "alg", "remote-key", "kid-object", "purpose"])
def test_signature_and_protected_header_are_domain_separated(mutation: str) -> None:
    signer = _signer()
    header: dict[str, Any] = {"alg": "ES256", "kid": signer.trust["keys"][0]["jwk"]["kid"], "typ": _PROFILE}
    if mutation == "purpose":
        alternate = _signer("installation_grant")
        signer.key, signer.trust = alternate.key, alternate.trust
        header["kid"] = alternate.trust["keys"][0]["jwk"]["kid"]
    elif mutation == "typ":
        header["typ"] = "urn:bridge-clean:grant:membership:v1"
    elif mutation == "alg":
        header["alg"] = "none"
    elif mutation == "remote-key":
        header["jku"] = "https://untrusted.invalid/key.json"
    elif mutation == "kid-object":
        header["kid"] = []
    result = verify_grant_denial(
        signer.token(header=header, high_s=mutation == "high-s", corrupt=mutation == "signature"),
        trust_set=signer.trust, context=signer.context,
    )
    assert not result.valid and result.denial is None


@pytest.mark.parametrize("verifier_time,valid", [(929, False), (930, False), (984, False), (985, True), (1589, True), (1590, False)])
def test_effective_time_and_exact_expiry_have_no_offline_grace(verifier_time: int, valid: bool) -> None:
    signer = _signer()
    result = verify_grant_denial(
        signer.token(), trust_set=signer.trust,
        context=replace(signer.context, verifier_time=verifier_time),
    )
    assert result.valid is valid


@pytest.mark.parametrize("shape", ["segments", "non-ascii", "oversize", "header-limit", "padding"])
def test_malformed_compact_object_is_bounded(shape: str) -> None:
    signer = _signer()
    token = {
        "segments": "not-a-jws", "non-ascii": "å.a.a", "oversize": "a" * 16_385,
        "header-limit": "a" * 513 + ".e30.e30", "padding": "e30=.e30.e30",
    }[shape]
    result = verify_grant_denial(token, trust_set=signer.trust, context=signer.context)
    assert not result.valid and result.denial is None

