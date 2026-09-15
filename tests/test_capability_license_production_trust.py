from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

import app.security.capability_license_verifier as license_verifier
from app.security.capability_license_verifier import (
    CapabilityLicenseTrustUnavailable,
    CapabilityLicenseVerifier,
)


ROOT = Path(__file__).resolve().parents[1]
TRUST_PATH = ROOT / "contracts" / "production" / "capability-license-v1" / "trust-set.json"
EXPECTED_SHA256 = "935cd51fe510065d6dc7d29ce481324e7ddf2be1a5431151bc74efc4f45a9173"
EXPECTED_KID = "bc1.cl.K4cd-bA7f90euaJ7M2eGWw"
EXPECTED_THUMBPRINT = "K4cd-bA7f90euaJ7M2eGWykHjyAZcAmgGeohXmSYQas"


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_packaged_sequence_1_key_identity_is_derived_from_a_valid_p256_point() -> None:
    raw = TRUST_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256
    key = json.loads(raw)["keys"][0]
    jwk = key["jwk"]

    x = int.from_bytes(_decode(jwk["x"]), "big")
    y = int.from_bytes(_decode(jwk["y"]), "big")
    public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    assert public_key.public_numbers().x == x
    assert public_key.public_numbers().y == y

    material = json.dumps(
        {name: jwk[name] for name in ("crv", "kty", "x", "y")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    assert _encode(digest) == key["thumbprint"] == EXPECTED_THUMBPRINT
    assert f"bc1.cl.{_encode(digest[:16])}" == key["kid"] == jwk["kid"] == EXPECTED_KID


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.__setitem__("production_usable", False),
        lambda doc: doc.__setitem__("profile", "urn:bridge-clean:capability-license:v1"),
        lambda doc: doc.__setitem__("purpose", "license"),
        lambda doc: doc.__setitem__("algorithm", "ES384"),
        lambda doc: doc["keys"][0].__setitem__("purpose", "membership"),
        lambda doc: doc["keys"][0]["jwk"].__setitem__("d", "A" * 43),
    ],
)
def test_production_verifier_construction_fails_closed_on_unusable_packaged_trust(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    trust = json.loads(TRUST_PATH.read_text("utf-8"))
    mutate(trust)
    monkeypatch.setattr(license_verifier, "load_trust_set", lambda *args, **kwargs: trust)
    with pytest.raises(CapabilityLicenseTrustUnavailable):
        CapabilityLicenseVerifier.production()
