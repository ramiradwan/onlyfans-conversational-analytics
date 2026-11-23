from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.core.customer_release import (
    CUSTOMER_RELEASE_PATH,
    CustomerReleaseConfigurationError,
    load_customer_release_config,
    main,
    validate_customer_release_document,
)

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "pyinstaller" / "brain.spec"


def document(receiver: str = "", api: str = "") -> dict[str, object]:
    return {
        "schema": "ofca-customer-release/v2",
        "setup_transfer_receiver_url": receiver,
        "hosted_api_origin": api,
        "authentication_providers": ["default"],
    }


def test_checked_in_release_binds_the_selected_working_api_and_transfer_receiver() -> None:
    config = load_customer_release_config(CUSTOMER_RELEASE_PATH, require_hosted=True)
    assert config.hosted_api_origin == "https://onboarding-dev.dipsy.fi"
    assert config.setup_transfer_receiver_url == config.hosted_api_origin + "/public/onboarding/setup"
    assert config.authentication_providers == ("google", "microsoft-personal", "default")


def test_api_origin_alone_supports_browser_ceremonies_without_a_generic_page() -> None:
    config = validate_customer_release_document(document(api="https://api.example.com/"), require_hosted=True)
    assert config.hosted_configured is True
    assert config.setup_transfer_configured is False
    assert config.setup_transfer_receiver_url == ""
    assert config.hosted_api_origin == "https://api.example.com"


def test_receiver_is_explicit_and_bound_to_the_api_origin() -> None:
    config = validate_customer_release_document(
        document("https://API.example.com:443/public/onboarding/setup", "https://api.example.com/"),
        require_hosted=True,
    )
    assert config.setup_transfer_receiver_url == "https://api.example.com/public/onboarding/setup"
    assert config.setup_transfer_configured is True


def test_empty_development_document_cannot_be_used_for_a_release() -> None:
    assert validate_customer_release_document(document()).hosted_configured is False
    with pytest.raises(CustomerReleaseConfigurationError, match="production release requires"):
        validate_customer_release_document(document(), require_hosted=True)


def test_authentication_choices_are_closed_and_preserve_email_fallback():
    value = document("https://api.example.com/public/onboarding/setup", "https://api.example.com")
    value["authentication_providers"] = ["google", "microsoft-personal", "default"]
    assert validate_customer_release_document(value).authentication_providers == (
        "google", "microsoft-personal", "default")
    for invalid in ([], ["google"], ["default", "default"], ["https://example.com", "default"], "default", [True]):
        with pytest.raises(CustomerReleaseConfigurationError):
            validate_customer_release_document({**value, "authentication_providers": invalid})


@pytest.mark.parametrize(
    "candidate",
    [
        document("https://api.example.com/public/onboarding/setup", ""),
        document("https://api.example.com/", "https://api.example.com"),
        document("https://api.example.com/onboarding/browser-ceremonies/{reference}", "https://api.example.com"),
        document("https://api.example.com/onboarding/browser-ceremonies/operation-reference", "https://api.example.com"),
        document("https://other.example.com/public/onboarding/setup", "https://api.example.com"),
        document("", "https://api.example.com:invalid"),
        document("", "https://*.example.com"),
        document("", " https://api.example.com"),
        {**document(api="https://api.example.com"), "schema": "ofca-customer-release/v1"},
        {"schema": "ofca-customer-release/v1", "hosted_onboarding_url": "https://api.example.com/start",
         "hosted_api_origin": "https://api.example.com", "authentication_providers": ["default"]},
        document("http://setup.example.com/start", "https://api.example.com"),
        document("https://user:secret@setup.example.com/start", "https://api.example.com"),
        document("https://api.example.com/public/onboarding/setup?token=secret", "https://api.example.com"),
        document("https://api.example.com/public/onboarding/setup#resume", "https://api.example.com"),
        document("https://setup.example.invalid/start", "https://api.example.com"),
        document("https://api.example.com/public/onboarding/setup", "http://api.example.com"),
        document("https://api.example.com/public/onboarding/setup", "https://api.example.com/v1"),
        document("https://api.example.com/public/onboarding/setup", "https://api.example.invalid"),
        {
            "schema": "ofca-customer-release/v2",
            "setup_transfer_receiver_url": "https://api.example.com/public/onboarding/setup",
            "hosted_api_origin": "https://api.example.com",
            "authentication_providers": ["default"],
            "extra": True,
        },
    ],
)
def test_release_document_refuses_partial_or_unsafe_customer_routing(candidate: object) -> None:
    with pytest.raises(CustomerReleaseConfigurationError):
        validate_customer_release_document(candidate, require_hosted=True)


def test_customer_release_loader_is_closed_over_exact_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "customer-release.json"
    path.write_text(json.dumps(document("https://api.example.com/public/onboarding/setup", "https://api.example.com")), encoding="utf-8")
    assert load_customer_release_config(path, require_hosted=True).hosted_configured is True

    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CustomerReleaseConfigurationError, match="unavailable"):
        load_customer_release_config(path, require_hosted=True)


def test_support_channels_match_the_agent_vectors():
    vectors = json.loads((ROOT / "extension/tests/fixtures/support-channels.json").read_text(encoding="utf-8"))
    for vector in vectors:
        config = validate_customer_release_document({**document(), **vector["input"]})
        assert list(config.support_channels) == list(vector["expected"].items())
    oversized = {"tiktok": "https://video.example.com/" + "x" * 1024}
    assert not validate_customer_release_document({**document(), "support_channels": oversized}).support_channels


def test_support_channels_are_absent_from_the_routing_summary(tmp_path, monkeypatch, capsys):
    release = tmp_path / "selected.json"
    release.write_text(json.dumps({**document(), "support_channels": {
        "whatsapp": "https://msg.example.com/help"}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["customer-release", "--config", str(release)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == document()


def test_pyinstaller_uses_the_selected_customer_release_bytes(spec_environment, tmp_path, monkeypatch):
    snapshot = tmp_path / "selected" / "customer-release.json"
    snapshot.parent.mkdir()
    selected = {**document(), "support_channels": {"whatsapp": "https://msg.example.com/help"}}
    snapshot.write_bytes((json.dumps(selected, indent=2) + "\n").encode())
    monkeypatch.setenv("BRAIN_BUILD_CUSTOMER_RELEASE_CONFIG", str(snapshot))
    spec_environment.execute("development", document())
    data = spec_environment.calls[0][1]["datas"]
    assert [(source, target) for source, target in data if Path(source).name == snapshot.name] == [
        (str(snapshot), "app/core")
    ]


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows wrapper")
@pytest.mark.parametrize("case", ["default", "override", "authority", "prebuilt", "prebuilt-empty", "changed-brain"])
def test_windows_support_channels_bind_one_snapshot(tmp_path, case):
    project = tmp_path / "source"
    output = tmp_path / "output"
    release = document(api="https://api.example.com")
    override = {**release, "support_channels": {"telegram": "https://chat.example.com/help"}}
    if case == "authority":
        override["hosted_api_origin"] = "https://different.example.com"
    selected = tmp_path / "override.json"
    selected.write_bytes((json.dumps(override, indent=4) + "\n").encode())
    source_bytes = (json.dumps(release, indent=2) + "\n").encode()
    disclosure = b"synthetic document"
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"instruments": {"risk_disclosure": {
        "rendered_sha256": hashlib.sha256(disclosure).hexdigest()}}}), encoding="utf-8")
    files = {
        "app/__init__.py": b"",
        "app/core/customer_release.py": (ROOT / "app/core/customer_release.py").read_bytes(),
        "app/core/extension_identity.py": (ROOT / "app/core/extension_identity.py").read_bytes(),
        "app/core/customer-release.json": source_bytes,
        "app/core/config.py": b'version: str = "1.2.3"\n',
        "app/core/signing-parameter-release.json": b"{}",
        "app/security/signing-publication-trust.json": b"{}",
        "app/security/signing_parameter_publications.py": b"def validate_trust(*args, **kwargs): pass\n",
        "app/services/signing_parameters.py": b"class FixedDistribution: pass\n",
        "app/provisioning/creator-platform-data-risk-disclosure.html": disclosure,
        "extension/build.mjs": b"",
        "extension/manifest.json": b'{"key":"Zml4dHVyZQ=="}',
        "desktop/src-tauri/Cargo.lock": b"fixture",
        "desktop/src-tauri/tauri.conf.json": b'{"version":"1.2.3"}',
        "packaging/build-desktop.ps1": (ROOT / "packaging/build-desktop.ps1").read_bytes(),
        "packaging/pyinstaller/brain.spec": b"",
        "packaging/inno/brain.iss": b"",
        "packaging/new-agent-bundle.ps1": b"",
        "packaging/runtime-files.json": b'{"required_files":[],"package_payloads":{"inno":{"adapter_resources":[]}}}',
        "packaging/write-digests.ps1": (ROOT / "packaging/write-digests.ps1").read_bytes(),
        "shared/desktop-release/prerequisite-policy.json": (ROOT / "shared/desktop-release/prerequisite-policy.json").read_bytes(),
        "tools/packaging_policy.py": b'def verify_runtime_files(*args, **kwargs): return []\ndef installed_runtime_files(*args): return ["Brain.exe"]\n',
    }
    for name, data in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "tests/fixtures/customer-release-forwarding.ps1"),
         "-Wrapper", str(ROOT / "packaging/build-windows.ps1"), "-Python", sys.executable,
         "-Project", str(project), "-Output", str(output), "-Selected", str(selected),
         "-Bindings", str(bindings), "-Case", case],
        capture_output=True, text=True, timeout=45,
    )
    if case in {"authority", "prebuilt", "changed-brain"}:
        assert completed.returncode != 0
        expected_message = {
            "authority": "customer release authority fields differ",
            "prebuilt": "Configured support requires a native asset build",
            "changed-brain": "Brain and Agent customer routing configuration must match",
        }[case]
        assert expected_message in completed.stdout + completed.stderr
        return
    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = source_bytes if case in {"default", "prebuilt-empty"} else selected.read_bytes()
    snapshot = output / "release-input/customer-release.json"
    assert snapshot.read_bytes() == expected
    staged = output / "dist/Brain/_internal/app/core/customer-release.json"
    assert staged.read_bytes() == expected
    lines = (project / "forwarding.txt").read_text(encoding="utf-8").splitlines()
    digest = hashlib.sha256(expected).hexdigest()
    consumers = ["--package", "brain", "--audit-package"]
    if case != "prebuilt-empty":
        consumers.insert(0, "native")
        projection = json.loads((output / "native/support-channels.json").read_text(encoding="utf-8"))
        assert projection == {"source_sha256": digest,
                              "support_channels": override["support_channels"] if case == "override" else {}}
    assert lines == [f"{consumer}|{snapshot}|{digest}" for consumer in consumers]


@pytest.fixture
def spec_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Execute the spec and its real routing validator without freezing an application."""
    project = tmp_path / "project"
    for relative in ("packaging", "app/core", "app/static/dist", "contracts", "extension/dist"):
        (project / relative).mkdir(parents=True, exist_ok=True)
    (project / "packaging/windows_branding.py").write_text(
        "def version_resource(path):\n    return None\n", encoding="utf-8")
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

    for name in ("PyInstaller", "PyInstaller.config", "PyInstaller.building", "PyInstaller.utils",
                 "PyInstaller.building.api", "PyInstaller.building.build_main", "PyInstaller.utils.hooks"):
        module = ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["PyInstaller.config"].CONF = {"workpath": str(tmp_path / "spec-work")}
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
        "https://api.example.com/public/onboarding/setup", "https://api.example.com")
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
        spec_environment.execute(mode, document("https://api.example.com/public/onboarding/setup", "https://api.example.com"))
    assert spec_environment.calls == []


@pytest.mark.parametrize("configuration", [
    document("https://api.example.com/public/onboarding/setup", ""),
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
    path.write_text(json.dumps(document("https://api.example.com/public/onboarding/setup", "https://api.example.com")), encoding="utf-8")
    monkeypatch.setenv(release.DEVELOPMENT_CUSTOMER_RELEASE_ENV, str(path))
    monkeypatch.setattr(release.sys, "frozen", False, raising=False)
    assert release.load_customer_release_config().setup_transfer_receiver_url == "https://api.example.com/public/onboarding/setup"
    monkeypatch.setattr(release.sys, "frozen", True)
    assert release.load_customer_release_config() == release.load_customer_release_config(CUSTOMER_RELEASE_PATH)
    assert release.load_customer_release_config().hosted_api_origin != "https://api.example.com"
