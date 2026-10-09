from __future__ import annotations

import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.core.customer_release import (
    CUSTOMER_RELEASE_PATH,
    CustomerReleaseConfigurationError,
    load_customer_release_config,
    validate_customer_release_document,
)

pytestmark = [pytest.mark.ci_tier('fast')]


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


def test_initial_handoff_browser_entry_uses_browser_origin_not_proof_api_origin():
    from app.core.customer_release import resolve_hosted_onboarding_start
    config = validate_customer_release_document(document(
        "https://setup.example.com/public/onboarding/setup", "https://api.example.com"))
    assert resolve_hosted_onboarding_start(config) == "https://setup.example.com/public/onboarding/setup/start"
    with pytest.raises(CustomerReleaseConfigurationError):
        resolve_hosted_onboarding_start(validate_customer_release_document(document(
            "https://setup.example.com/start", "https://api.example.com")))


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


@pytest.fixture
def spec_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Execute the spec and its real routing validator without freezing an application."""
    project = tmp_path / "project"
    for relative in ("packaging", "app/core", "app/static/dist", "contracts", "extension/dist"):
        (project / relative).mkdir(parents=True, exist_ok=True)
    (project / "packaging/runtime-files.json").write_text(json.dumps({
        "required_files": [], "frontend": {"dist_path": "_internal/app/static/dist"},
        "sql_catalogs": [], "contracts": {"path": "_internal/contracts"},
    }), encoding="utf-8")
    monkeypatch.setenv("BRAIN_PROJECT_ROOT", str(project))
    monkeypatch.setenv("BRAIN_SOURCE_ROOT", str(project))
    monkeypatch.syspath_prepend(str(project))
    calls = []

    def analysis(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pure=[], zipped_data=[], scripts=[], binaries=[], datas=kwargs["datas"])

    for name in ("PyInstaller", "PyInstaller.building", "PyInstaller.utils",
                 "PyInstaller.building.api", "PyInstaller.building.build_main", "PyInstaller.utils.hooks"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    api = sys.modules["PyInstaller.building.api"]
    api.COLLECT = api.EXE = api.PYZ = lambda *args, **kwargs: None
    sys.modules["PyInstaller.building.build_main"].Analysis = analysis
    hooks = sys.modules["PyInstaller.utils.hooks"]
    hooks.collect_data_files = hooks.collect_dynamic_libs = hooks.copy_metadata = lambda *args, **kwargs: []

    def execute(mode, configuration, *, metadata=True, spec=SPEC):
        if mode is None:
            monkeypatch.delenv("BRAIN_BUILD_MODE", raising=False)
        else:
            monkeypatch.setenv("BRAIN_BUILD_MODE", mode)
        (project / "app/core/customer-release.json").write_text(json.dumps(configuration), encoding="utf-8")
        metadata_path = project / "extension/dist/build-meta.json"
        if metadata:
            rule_bytes = (ROOT / "extension/tests/fixtures/packaged-signing-rule.json").read_bytes()
            legal_bytes = (ROOT / "extension/tests/fixtures/legal-instrument-bindings.synthetic.json").read_bytes()
            rule, legal = json.loads(rule_bytes), json.loads(legal_bytes)
            metadata_path.write_text(json.dumps({
                "signing_rule": {"schema": rule["schema"], "source_revision": rule["source_revision"],
                    "sha256": hashlib.sha256(rule_bytes).hexdigest()},
                "legal_bindings": {"schema": legal["schema"], "source_revision": legal["legal_repository_revision"],
                    "legal_bindings_digest": hashlib.sha256(legal_bytes).hexdigest()},
                "privacy_policy_configured": True,
            }), encoding="utf-8")
        else:
            metadata_path.unlink(missing_ok=True)
        return runpy.run_path(str(spec))

    return SimpleNamespace(execute=execute, calls=calls, project=project)


@pytest.mark.parametrize("mode", ["development", "release"])
def test_pyinstaller_binds_and_requires_customer_routing_for_release_artifacts(spec_environment, mode):
    configuration = document() if mode == "development" else document(
        "https://setup.example.com/start", "https://api.example.com")
    spec_environment.execute(mode, configuration)
    assert len(spec_environment.calls) == 1
    data = spec_environment.calls[0][1]["datas"]
    assert (str(spec_environment.project / "app/core/customer-release.json"), "app/core") in data


@pytest.mark.parametrize("metadata", [False, True])
def test_release_mode_requires_hosted_routing_independently_of_agent_metadata(spec_environment, metadata):
    with pytest.raises(CustomerReleaseConfigurationError, match="production release requires"):
        spec_environment.execute("release", document(), metadata=metadata)
    assert spec_environment.calls == []


@pytest.mark.parametrize("mode", [None, "", "production", "Release", "false", "0", " development "])
def test_spec_refuses_missing_or_invalid_build_mode(spec_environment, mode):
    with pytest.raises(ValueError, match="explicit build mode"):
        spec_environment.execute(mode, document("https://setup.example.com/start", "https://api.example.com"))
    assert spec_environment.calls == []


@pytest.mark.parametrize("configuration", [
    document("https://setup.example.com/start", ""),
    document("http://setup.example.com/start", "http://api.example.com"),
    {**document(), "unexpected": True},
])
def test_development_mode_keeps_customer_routing_validation(spec_environment, configuration):
    with pytest.raises(CustomerReleaseConfigurationError):
        spec_environment.execute("development", configuration)
    assert spec_environment.calls == []


def test_source_release_override_is_complete_and_ignored_by_frozen_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.customer_release as release

    path = tmp_path / "customer-release.json"
    path.write_text(json.dumps(document("https://setup.example.com/start", "https://api.example.com")), encoding="utf-8")
    monkeypatch.setenv(release.DEVELOPMENT_CUSTOMER_RELEASE_ENV, str(path))
    monkeypatch.setattr(release.sys, "frozen", False, raising=False)
    assert release.load_customer_release_config().hosted_onboarding_url == "https://setup.example.com/start"
    monkeypatch.setattr(release.sys, "frozen", True)
    assert release.load_customer_release_config().hosted_configured is False
