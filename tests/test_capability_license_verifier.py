from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.security.capability_license_verifier import (
    CapabilityLicenseTrustUnavailable,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
    PackagedCapabilityLicenseTrustProvider,
    verify_capability_license,
)


VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"


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


def test_production_verifier_fails_closed_without_packaged_trust() -> None:
    with pytest.raises(CapabilityLicenseTrustUnavailable, match="production CapabilityLicense trust"):
        CapabilityLicenseVerifier.production()


def test_production_provider_never_falls_back_to_fixture_trust() -> None:
    with pytest.raises(CapabilityLicenseTrustUnavailable):
        PackagedCapabilityLicenseTrustProvider().trust_set()


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
