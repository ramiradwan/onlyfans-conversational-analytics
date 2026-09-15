"""Reconstructable pairing keys used only by the browser E2E harness.

The production runtime never imports this module.  The deterministic material is
bound to the repository's TEST-VECTORS-ONLY companion-pairing fixture labels so
CI can exercise the real pairing/session protocol without a TPM or hosted
commercial-control-plane signer.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import InstallationKeyReference
from app.security.installation_key import INSTALLATION_PROOF_ALGORITHM, InstallationProof


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
PROFILE = json.loads(
    (PRODUCT_ROOT / "contracts" / "companion-pairing-profile" / "profile.json").read_text(
        encoding="utf-8"
    )
)
VECTOR = json.loads(
    (PRODUCT_ROOT / "contracts" / "companion-pairing-v1" / "vector.json").read_text(
        encoding="utf-8"
    )
)
TEST_TRUST = json.loads(
    (PRODUCT_ROOT / "contracts" / "companion-pairing-v1" / "trust-set.json").read_text(
        encoding="utf-8"
    )
)
ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
PREFIX = PROFILE["test_fixture_derivation"]["label_prefix"]


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _private_key(label: str) -> ec.EllipticCurvePrivateKey:
    material = hashlib.sha256((PREFIX + label).encode("ascii")).digest()
    scalar = int.from_bytes(material, "big") % (ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _public_jwk(key: ec.EllipticCurvePrivateKey, *, kid: str | None = None) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    result = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    if kid is not None:
        result["kid"] = kid
    return result


def _thumbprint(jwk: dict[str, str]) -> str:
    bare = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(bare, sort_keys=True, separators=(",", ":")).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def _raw_low_s_signature(key: ec.EllipticCurvePrivateKey, message: bytes) -> bytes:
    der = key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")


def synthetic_installation_key_material(installation_key_id: str) -> tuple[str, str]:
    """Return a real P-256 public reference reconstructable from the key id."""

    key = _private_key(f"e2e-installation-key:{installation_key_id}")
    jwk = _public_jwk(key, kid=installation_key_id)
    return _thumbprint(jwk), json.dumps(jwk, sort_keys=True, separators=(",", ":"))


class SyntheticPairingAuthority:
    """Installation-key signing boundary for an E2E synthetic reference."""

    def __init__(self, reference: InstallationKeyReference) -> None:
        self.reference = reference
        expected_jkt, expected_jwk = synthetic_installation_key_material(
            reference.installation_key_id
        )
        if reference.installation_key_jkt != expected_jkt or reference.public_key_jwk != expected_jwk:
            raise RuntimeError("e2e_installation_key_reference_mismatch")
        self.key = _private_key(
            f"e2e-installation-key:{reference.installation_key_id}"
        )

    def ensure_ready(self) -> InstallationKeyReference:
        return self.reference

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        if not isinstance(challenge, bytes):
            raise TypeError("Installation proof challenge must be bytes")
        return InstallationProof(
            self.reference.installation_key_id,
            INSTALLATION_PROOF_ALGORITHM,
            _raw_low_s_signature(self.key, challenge),
        )


def qualification_trust_set() -> dict[str, object]:
    """Return the pinned test trust in a loader-compatible E2E envelope."""

    trust = json.loads(json.dumps(TEST_TRUST))
    trust["production_usable"] = True
    return trust


def _issuer_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = _private_key(VECTOR["fixture_labels"]["issuer_key"])
    entries = TEST_TRUST["keys"]
    if len(entries) != 1:
        raise RuntimeError("e2e_pairing_trust_shape_invalid")
    expected = entries[0]["jwk"]
    actual = _public_jwk(key, kid=expected["kid"])
    if actual != expected:
        raise RuntimeError("e2e_pairing_trust_key_mismatch")
    return key, expected["kid"]


def sign_pairing_grant(
    grant_type: str,
    *,
    organization_id: str,
    installation_id: str,
    installation_key_id: str,
    installation_key_jkt: str,
    creator_account_id: str | None,
    issued_at: int,
    unique_suffix: str = "0",
) -> tuple[str, str, int]:
    """Mint a current installation/binding grant under the pinned test issuer."""

    profiles = {
        "installation_grant": (
            "urn:bridge-clean:grant:installation:v1",
            "urn:bridge-clean:local-brain:installation",
            2_592_000,
        ),
        "creator_account_binding": (
            "urn:bridge-clean:grant:creator-binding:v1",
            "urn:bridge-clean:local-brain:creator-binding",
            604_800,
        ),
    }
    if grant_type not in profiles:
        raise ValueError("unsupported e2e pairing grant")
    typ, audience, lifetime = profiles[grant_type]
    discriminator = "1" if grant_type == "installation_grant" else "2"
    suffix = (unique_suffix[-1:] if unique_suffix else "0")
    jti = f"0199a1b2-c3d4-71{discriminator}{suffix}-8000-00000000000{discriminator}"
    subject = f"installation:{installation_id}"
    claims: dict[str, object] = {
        "aud": audience,
        "exp": issued_at + lifetime,
        "grant_type": grant_type,
        "iat": issued_at,
        "installation_id": installation_id,
        "installation_key_id": installation_key_id,
        "installation_key_jkt": installation_key_jkt,
        "iss": "urn:bridge-clean:commercial-control-plane",
        "jti": jti,
        "nbf": issued_at,
        "organization_id": organization_id,
        "profile": "urn:bridge-clean:grant-profile:v1",
        "sub": subject,
    }
    if grant_type == "creator_account_binding":
        if creator_account_id is None:
            raise ValueError("creator account is required")
        claims.update(
            {
                "approval_id": f"0199a1b2-c3d4-73{suffix}0-8000-000000000003",
                "approval_revision": 1,
                "creator_account_id": creator_account_id,
            }
        )
        claims["sub"] = f"installation:{installation_id}:creator:{creator_account_id}"
    key, kid = _issuer_key()
    header = {"alg": "ES256", "kid": kid, "typ": typ}
    encoded_header = _b64url(
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    encoded_claims = _b64url(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    token = f"{encoded_header}.{encoded_claims}.{_b64url(_raw_low_s_signature(key, signing_input))}"
    return token, jti, issued_at + lifetime
