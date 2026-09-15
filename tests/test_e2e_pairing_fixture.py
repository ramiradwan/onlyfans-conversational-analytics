"""Focused proof that the E2E-only pairing signer matches production verifiers."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.persistence.auth import InstallationKeyReference
from app.security.companion_pairing_proof import proof_message, verify_pairing_proof
from app.security.grant_verifier import GrantVerificationContext, verify_grant
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    INSTALLATION_PROOF_ALGORITHM,
)


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "e2e-capture" / "helpers" / "pairing_fixture.py"


def _fixture_module():
    sys.path.insert(0, str(HELPER.parent))
    specification = importlib.util.spec_from_file_location("e2e_pairing_fixture", HELPER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_synthetic_pairing_authority_signs_a_production_valid_brain_proof() -> None:
    fixture = _fixture_module()
    key_id = "e2e-installation-key-proof-test"
    thumbprint, public_key_jwk = fixture.synthetic_installation_key_material(key_id)
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    reference = InstallationKeyReference(
        provider_name="E2E Synthetic Installation Key Provider",
        provider_key_name="e2e-temporary-installation-key",
        algorithm=INSTALLATION_KEY_ALGORITHM,
        installation_key_id=key_id,
        installation_key_jkt=thumbprint,
        public_key_jwk=public_key_jwk,
        created_at=now,
        activated_at=now,
    )
    authority = fixture.SyntheticPairingAuthority(reference)
    digest = bytes(range(32))

    proof = authority.sign_challenge(proof_message("brain", digest))

    jwk = json.loads(public_key_jwk)
    jwk.pop("kid")
    encoded = base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode("ascii")
    assert reference.algorithm == INSTALLATION_KEY_ALGORITHM
    assert proof.installation_key_id == key_id
    assert proof.algorithm == INSTALLATION_PROOF_ALGORITHM
    assert verify_pairing_proof(jwk, "brain", digest, encoded)


def test_synthetic_pairing_grants_pass_the_production_grant_verifier() -> None:
    fixture = _fixture_module()
    organization_id = "e2e-organization"
    installation_id = "e2e-temporary-installation"
    key_id = "e2e-installation-key-grant-test"
    account_id = "dev-creator-account"
    thumbprint, _ = fixture.synthetic_installation_key_material(key_id)
    verifier_time = int(datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc).timestamp())

    cases = (
        (
            "installation_grant",
            "urn:bridge-clean:local-brain:installation",
            f"installation:{installation_id}",
            None,
        ),
        (
            "creator_account_binding",
            "urn:bridge-clean:local-brain:creator-binding",
            f"installation:{installation_id}:creator:{account_id}",
            account_id,
        ),
    )
    for grant_type, audience, subject, creator_account_id in cases:
        token, _, _ = fixture.sign_pairing_grant(
            grant_type,
            organization_id=organization_id,
            installation_id=installation_id,
            installation_key_id=key_id,
            installation_key_jkt=thumbprint,
            creator_account_id=creator_account_id,
            issued_at=verifier_time - 30,
        )
        outcome = verify_grant(
            token,
            trust_set=fixture.TEST_TRUST,
            context=GrantVerificationContext(
                expected_grant_type=grant_type,
                expected_audience=audience,
                expected_organization_id=organization_id,
                expected_installation_id=installation_id,
                expected_installation_key_id=key_id,
                expected_installation_key_jkt=thumbprint,
                expected_subject=subject,
                verifier_time=verifier_time,
            ),
        )
        assert outcome.valid, (grant_type, outcome.result)


def test_qualification_trust_only_promotes_the_explicit_fixture_keys() -> None:
    fixture = _fixture_module()

    qualified = fixture.qualification_trust_set()

    assert fixture.TEST_TRUST["production_usable"] is False
    assert qualified["production_usable"] is True
    assert qualified["keys"]
    assert all(entry["fixture_only"] is True for entry in qualified["keys"])
