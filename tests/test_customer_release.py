from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.customer_release import (
    CUSTOMER_RELEASE_PATH,
    CustomerReleaseConfigurationError,
    load_customer_release_config,
    validate_customer_release_document,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "pyinstaller" / "brain.spec"


def document(onboarding: str = "", api: str = "") -> dict[str, str]:
    return {
        "schema": "ofca-customer-release/v1",
        "hosted_onboarding_url": onboarding,
        "hosted_api_origin": api,
    }


def test_checked_in_customer_release_document_is_safe_for_development_only() -> None:
    config = load_customer_release_config(CUSTOMER_RELEASE_PATH)
    assert config.hosted_configured is False
    with pytest.raises(CustomerReleaseConfigurationError, match="production release requires"):
        load_customer_release_config(CUSTOMER_RELEASE_PATH, require_hosted=True)


def test_release_document_accepts_one_customer_entry_and_one_api_origin() -> None:
    config = validate_customer_release_document(
        document(
            "https://setup.example.com/start",
            "https://api.example.com/",
        ),
        require_hosted=True,
    )
    assert config.hosted_onboarding_url == "https://setup.example.com/start"
    assert config.hosted_api_origin == "https://api.example.com"
    assert config.hosted_configured is True


@pytest.mark.parametrize(
    "candidate",
    [
        document("https://setup.example.com/start", ""),
        document("", "https://api.example.com"),
        document("http://setup.example.com/start", "https://api.example.com"),
        document("https://user:secret@setup.example.com/start", "https://api.example.com"),
        document("https://setup.example.com/start?token=secret", "https://api.example.com"),
        document("https://setup.example.com/start#resume", "https://api.example.com"),
        document("https://setup.example.invalid/start", "https://api.example.com"),
        document("https://setup.example.com/start", "http://api.example.com"),
        document("https://setup.example.com/start", "https://api.example.com/v1"),
        document("https://setup.example.com/start", "https://api.example.invalid"),
        {
            "schema": "ofca-customer-release/v1",
            "hosted_onboarding_url": "https://setup.example.com/start",
            "hosted_api_origin": "https://api.example.com",
            "extra": True,
        },
    ],
)
def test_release_document_refuses_partial_or_unsafe_customer_routing(candidate: object) -> None:
    with pytest.raises(CustomerReleaseConfigurationError):
        validate_customer_release_document(candidate, require_hosted=True)


def test_customer_release_loader_is_closed_over_exact_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "customer-release.json"
    path.write_text(json.dumps(document("https://setup.example.com/start", "https://api.example.com")), encoding="utf-8")
    assert load_customer_release_config(path, require_hosted=True).hosted_configured is True

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CustomerReleaseConfigurationError, match="unavailable"):
        load_customer_release_config(path, require_hosted=True)


def test_pyinstaller_binds_and_requires_customer_routing_for_release_artifacts() -> None:
    source = SPEC.read_text(encoding="utf-8")
    assert '_add_file(_DATAS, "_internal/app/core/customer-release.json")' in source
    assert "require_hosted=_release_mode" in source
    assert '_agent_metadata.get("signing_rule") is not None' in source
    assert '_agent_metadata.get("legal_bindings") is not None' in source
    assert '_agent_metadata.get("privacy_policy_configured") is True' in source
