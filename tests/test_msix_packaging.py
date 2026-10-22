"""Exercise the MSIX adapter through real policy, XML and Windows SDK boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

from test_packaging_policy import _stage_runtime_tree
from tools import msix_packaging as msix
from tools.packaging_policy import installed_runtime_files, verify_runtime_files

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "packaging/msix/profiles/qualification-direct.json"
SDK_ROOT = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10"
MAKEAPPX = SDK_ROOT / "bin/10.0.26100.0/x64/makeappx.exe"
MANIFEST_TOOL = SDK_ROOT / "bin/10.0.26100.0/x64/mt.exe"
IDENTITY_PROBE_TIMEOUT_SECONDS = 120
pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.mark.parametrize(("profile_name", "optional"), [
    ("qualification-direct", "true"), ("production-direct", "true"),
    ("qualification-store", "false"), ("production-store", "false"),
])
def test_dependency_policy_is_owned_by_distribution(profile_name, optional):
    selected = msix.load_profile(ROOT / f"packaging/msix/profiles/{profile_name}.json")
    manifest = ET.fromstring(msix.render_manifest(selected))
    dependency = manifest.find(".//win32dependencies:ExternalDependency", msix.NAMESPACES)
    assert dependency is not None
    assert dependency.attrib["Optional"] == optional
    assert dependency.attrib["MinVersion"] == selected["webview2_minimum_version"]


def profile():
    return msix.load_profile(PROFILE_PATH)


def _read_windows_package_identity(manifest, tmp_path):
    """Keep cold PowerShell compilation bounded and retain its failing phase."""
    script = tmp_path / "identity-probe.ps1"
    script.write_text("""param([string]$Binding, [string]$Manifest)
$ErrorActionPreference = 'Stop'
$timer = [Diagnostics.Stopwatch]::StartNew()
function Write-ProbePhase([string]$Phase) {
    [Console]::Error.WriteLine(('ofca-msix-identity-probe:{0}:{1}ms' -f $Phase, $timer.ElapsedMilliseconds))
}
Write-ProbePhase 'script-entry'
try {
    Add-Type -Path $Binding
    Write-ProbePhase 'add-type-complete'
    $bytes = [IO.File]::ReadAllBytes($Manifest)
    Write-ProbePhase 'windows-read-start'
    $identity = [Ofca.Msix.IdentityReader]::Read($bytes)
    Write-ProbePhase 'windows-read-complete'
    $identity | ConvertTo-Json -Compress
} finally {
    Write-ProbePhase 'exit'
}
""", encoding="utf-8")
    try:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script),
             str(ROOT / "packaging/msix/ReadPackageIdentity.cs"), str(manifest)],
            capture_output=True, text=True, timeout=IDENTITY_PROBE_TIMEOUT_SECONDS, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        diagnostic = error.stderr or ""
        if isinstance(diagnostic, bytes):
            diagnostic = diagnostic.decode("utf-8", errors="replace")
        phases = [line for line in diagnostic.splitlines()
                  if line.startswith("ofca-msix-identity-probe:")]
        error.add_note("Windows identity probe phases (elapsed from script entry):\n"
                       + ("\n".join(phases)[:2048] or "No script phase was captured."))
        if diagnostic:
            error.add_note("Windows identity probe stderr (last 2048 characters):\n"
                           + diagnostic[-2048:])
        raise
    return json.loads(result.stdout)


@pytest.fixture
def stage(tmp_path):
    root = _stage_runtime_tree(tmp_path)
    (root / "release-manifest.json").write_bytes(msix.canonical_json({
        "schema": "ofca-release-manifest/v1", "version": "0.7.5", "architecture": "x64",
        "source_commit": "1" * 40,
    }))
    return root


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(unknown=True),
    lambda value: value.update(release_sequence=True),
    lambda value: value.update(release_sequence=0),
    lambda value: value.update(minimum_os_version="10.0.26099.0"),
    lambda value: value.update(maximum_os_version_tested="10.0.26099.0"),
    lambda value: value.update(webview2_minimum_version="0.1.0.0"),
    lambda value: value.update(webview2_minimum_version="133.0.3124.0"),
    lambda value: value["identity"].update(architecture="arm64"),
    lambda value: value["identity"].update(name="Production.Name"),
    lambda value: value.update(app_installer_uri="http://qualification.example.test/feed.appinstaller"),
    lambda value: value.update(app_installer_uri="https://user:password@qualification.example.test/feed.appinstaller"),
    lambda value: value.update(app_installer_uri="https://localhost/feed.appinstaller"),
    lambda value: value.update(app_installer_uri="https://qualification.example.test/feed.appinstaller?redirect=1"),
])
def test_profile_refuses_undeclared_or_unsupported_authority(mutation):
    value = profile()
    mutation(value)
    with pytest.raises(ValueError):
        msix.validate_profile(value)


def test_production_refuses_qualification_or_unverified_template():
    with pytest.raises(ValueError, match="qualification profiles"):
        msix.validate_profile(profile(), production=True)
    with pytest.raises(ValueError):
        msix.load_profile(ROOT / "packaging/msix/profiles/production-direct.template.json", production=True)
    store = msix.load_profile(ROOT / "packaging/msix/profiles/qualification-store.json")
    store["app_installer_uri"] = profile()["app_installer_uri"]
    with pytest.raises(ValueError, match="Store profiles"):
        msix.validate_profile(store)


def test_production_preparation_admits_only_ignored_workflow_artifacts(stage, tmp_path, monkeypatch):
    # A tiny standalone Git fixture exercises the producer's actual status
    # command with the checked-in ignore rules, without copying a checkout.
    repository = tmp_path / "producer-source"
    repository.mkdir()
    empty_template = tmp_path / "empty-git-template"
    empty_template.mkdir()
    empty_excludes = tmp_path / "empty-git-excludes"
    empty_excludes.write_bytes(b"")

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(repository), *arguments], text=True)

    git("init", "--quiet", "--template=" + str(empty_template))
    for name, value in {
        "user.name": "MSIX synthetic source fixture", "user.email": "msix-fixture@example.invalid",
        "commit.gpgsign": "false", "core.hooksPath": str(empty_template),
        "core.excludesFile": str(empty_excludes),
    }.items():
        git("config", name, value)
    (repository / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    delivery = repository / "tools/desktop-delivery"
    delivery.mkdir(parents=True)
    (delivery / ".gitignore").write_bytes((ROOT / "tools/desktop-delivery/.gitignore").read_bytes())
    prerequisite = repository / "shared/desktop-release/prerequisite-policy.json"
    prerequisite.parent.mkdir(parents=True)
    prerequisite.write_bytes((ROOT / "shared/desktop-release/prerequisite-policy.json").read_bytes())
    tracked = repository / "source.txt"
    tracked.write_text("committed source\n", encoding="utf-8")
    git("add", "--", ".gitignore", "tools/desktop-delivery/.gitignore",
        "shared/desktop-release/prerequisite-policy.json", "source.txt")
    git("commit", "--quiet", "-m", "Synthetic producer source")
    assert git("status", "--porcelain") == ""

    generated = (
        repository / ".build-venv/pyvenv.cfg",
        delivery / "node_modules/synthetic/package.json",
    )
    for path in generated:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"synthetic workflow artifact; never executed")
    monkeypatch.setattr(msix, "ROOT", repository)
    production = ROOT / "packaging/msix/profiles/production-direct.json"
    working = tmp_path / "admitted-output"
    msix.prepare_payload(stage, working, production)
    assert (working / "preparation.json").is_file()
    assert git("status", "--porcelain") == ""
    assert set(git("check-ignore", "--", *(path.relative_to(repository).as_posix() for path in generated)).splitlines()) == {
        path.relative_to(repository).as_posix() for path in generated
    }

    # The build-environment exclusion is root-only: a lookalike elsewhere is
    # unrelated untracked source and must still prevent production admission.
    unrelated = repository / "elsewhere/.build-venv/unexpected.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("not a declared workflow artifact\n", encoding="utf-8")
    refused = tmp_path / "untracked-output"
    with pytest.raises(ValueError, match="committed clean producer source") as error:
        msix.prepare_payload(stage, refused, production)
    assert not refused.exists()
    assert json.loads(str(error.value).split("; producer_status=", 1)[1]) == {
        "entry_count": 1, "sample": ["?? elsewhere/"], "truncated": False,
    }
    assert "not a declared workflow artifact" not in str(error.value)
    unrelated.unlink()
    assert git("status", "--porcelain") == ""

    tracked.write_text("changed source\n", encoding="utf-8")
    refused = tmp_path / "tracked-output"
    with pytest.raises(ValueError, match="committed clean producer source") as error:
        msix.prepare_payload(stage, refused, production)
    assert not refused.exists()
    assert json.loads(str(error.value).split("; producer_status=", 1)[1])["sample"] == [" M source.txt"]
    assert "changed source" not in str(error.value)

    # A noisy checkout still refuses before output creation, with bounded,
    # JSON-escaped path metadata rather than file contents or a raw log dump.
    for number in range(12):
        (repository / f"untracked-{number:02d}.txt").write_text("private fixture content\n", encoding="utf-8")
    refused = tmp_path / "noisy-output"
    with pytest.raises(ValueError, match="committed clean producer source") as error:
        msix.prepare_payload(stage, refused, production)
    assert not refused.exists()
    diagnostic = json.loads(str(error.value).split("; producer_status=", 1)[1])
    assert diagnostic["entry_count"] == 13
    assert len(diagnostic["sample"]) == 8
    assert diagnostic["truncated"] is True
    assert "private fixture content" not in str(error.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows Appx identity calculation")
def test_reserved_store_identity_matches_windows_family_without_registration(tmp_path):
    reserved = msix.read_json(ROOT / "packaging/msix/store-reservation.json")
    store = msix.load_profile(ROOT / "packaging/msix/profiles/production-store.json", production=True)
    assert store["distribution"] == "microsoft-store" and store["app_installer_uri"] is None
    assert store["identity"]["publisher_display_name"] == reserved["publisher_display_name"]
    assert msix.version_tuple(store["minimum_os_version"]) >= (10, 0, 26200, 0)
    manifest = tmp_path / "AppxManifest.xml"
    manifest.write_bytes(msix.render_manifest(store))
    identity = _read_windows_package_identity(manifest, tmp_path)
    assert identity["Name"] == reserved["package_name"] == store["identity"]["name"]
    assert identity["Publisher"] == reserved["publisher"] == store["identity"]["publisher"]
    assert identity["FamilyName"] == reserved["package_family_name"]
    assert reserved["store_id"] == "9PD4KGLX42JD" and reserved["product_status"] == "In draft"
    assert profile()["identity"]["name"] != identity["Name"]
    assert msix.load_profile(ROOT / "packaging/msix/profiles/qualification-store.json")["identity"]["name"] != identity["Name"]


def test_deployment_counter_rolls_forward_without_changing_application_major():
    assert msix.package_version(1) == "1.0.1.0"
    assert msix.package_version(65535) == "1.0.65535.0"
    assert msix.package_version(65536) == "1.1.0.0"
    assert msix.package_version(4294967295) == "1.65535.65535.0"
    assert msix.version_tuple(msix.package_version(65536)) > msix.version_tuple(msix.package_version(65535))
    assert profile()["application_version"] == "0.7.5"


def _release_handoff(tmp_path, target="msix-direct"):
    """Synthetic bytes exercise handoff admission, never signature qualification."""
    directory = tmp_path / "release"
    directory.mkdir()
    route = "direct" if target == "msix-direct" else "store"
    selected = msix.load_profile(ROOT / f"packaging/msix/profiles/production-{route}.json", production=True)
    version = msix.package_version(selected["release_sequence"])
    package_file = f"ConversationAnalytics_{version}_x64.msix"
    (directory / package_file).write_bytes(b"deliberately not a signed MSIX")
    agent_file = "ConversationAnalytics-Agent-2.0.3-chrome.zip"
    (directory / agent_file).write_bytes(b"synthetic linked Agent bytes")
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
    (directory / "desktop.profile.json").write_bytes(msix.canonical_json(selected))
    profile_hash = msix.sha256(directory / "desktop.profile.json")
    (directory / "desktop.inventory.json").write_bytes(msix.canonical_json({
        "schema": "ofca-msix-inventory/v1", "profile_sha256": profile_hash,
        "runtime_release": {"source_commit": source}, "identity": selected["identity"],
        "application_version": selected["application_version"], "package_version": version,
    }))
    unsigned_hash = msix.sha256(directory / package_file)
    (directory / "desktop.build.json").write_bytes(msix.canonical_json({
        "producer_source": {"base_commit": source, "dirty": False},
        "verification": {"package_sha256": unsigned_hash},
    }))
    direct = msix.read_json(ROOT / "packaging/msix/direct-delivery.json")
    template_hash = delivery_hash = None
    family = msix.read_json(ROOT / "packaging/msix/store-reservation.json")["package_family_name"]
    if route == "direct":
        uri = direct["hosting"]["public_origin"] + direct["hosting"]["base_path"] + "packages/" + "0" * 64 + "/" + package_file
        (directory / "desktop.appinstaller-template.xml").write_bytes(msix.render_appinstaller(selected, uri))
        template_hash = msix.sha256(directory / "desktop.appinstaller-template.xml")
        delivery_hash = msix.sha256(ROOT / "packaging/msix/direct-delivery.json")
        family = direct["expected_package_family"]
    record = {
        "schema": "ofca-msix-release-inputs/v1", "target": target, "source_commit": source,
        "source_tree": tree, "package_file": package_file, "package_version": version,
        "application_version": selected["application_version"], "profile_sha256": profile_hash,
        "inventory_sha256": msix.sha256(directory / "desktop.inventory.json"),
        "build_sha256": msix.sha256(directory / "desktop.build.json"), "unsigned_package_sha256": unsigned_hash,
        "agent_file": agent_file, "agent_sha256": msix.sha256(directory / agent_file),
        "expected_family": family, "direct_delivery_sha256": delivery_hash,
        "appinstaller_template_sha256": template_hash,
    }
    (directory / "desktop.release-inputs.json").write_bytes(msix.canonical_json(record))
    environment = dict(os.environ, **{
        "MSIX_DIRECT_PUBLISHER": direct["signing"]["subject"],
        "MSIX_DIRECT_EXPECTED_FAMILY": direct["expected_package_family"],
        "MSIX_DIRECT_APPINSTALLER_URI": direct["hosting"]["app_installer_uri"],
        "DESKTOP_R2_BUCKET": direct["hosting"]["bucket"], "DESKTOP_R2_ENDPOINT": direct["hosting"]["s3_endpoint"],
        "DESKTOP_UPDATES_BASE_URL": direct["hosting"]["public_origin"] + direct["hosting"]["base_path"],
    })
    return directory, record, environment


def _check_release_handoff(directory, environment, target="msix-direct", script="assert-desktop-release-inputs.ps1"):
    return subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "packaging" / script),
        "-Target", target, "-Directory", str(directory), "-ProductRevision",
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()],
        env=environment, capture_output=True, text=True, check=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows SDK release handoff and signature verification")
@pytest.mark.parametrize("target", ["msix-direct", "msix-store"])
def test_release_handoff_does_not_substitute_for_package_verification(tmp_path, target):
    directory, _, environment = _release_handoff(tmp_path, target)
    admitted = _check_release_handoff(directory, environment, target)
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    if target == "msix-direct":
        snapshot = msix.read_json(directory / "desktop.resolved-direct-inputs.json")
        assert snapshot["schema"] == "ofca-direct-release-environment/v1"
        assert snapshot["values"] == {name: environment[name] for name in snapshot["values"]}
    verified = _check_release_handoff(directory, environment, target, "verify-desktop-release.ps1")
    assert verified.returncode != 0
    assert not (directory / "desktop.artifact-release.json").exists()
    assert not (directory / "ConversationAnalytics.appinstaller").exists()


def test_release_verification_refuses_changed_resolved_input_evidence(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows PowerShell release verification")
    directory, _, environment = _release_handoff(tmp_path)
    admitted = _check_release_handoff(directory, environment)
    assert admitted.returncode == 0, admitted.stdout + admitted.stderr
    path = directory / "desktop.resolved-direct-inputs.json"
    snapshot = msix.read_json(path)
    snapshot["values"]["MSIX_DIRECT_PUBLISHER"] = "CN=Another Publisher"
    path.write_bytes(msix.canonical_json(snapshot))
    refused = _check_release_handoff(directory, environment, script="verify-desktop-release.ps1")
    assert refused.returncode != 0 and "Resolved release environment changed" in refused.stderr
    assert not (directory / "desktop.verification.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Independent Windows MSIX publication verification")
@pytest.mark.parametrize("mutation", ["forged-receipt", "descriptor-policy", "final-hash", "store-scope"])
def test_direct_publication_requires_the_actual_signature_not_claimed_receipts(tmp_path, mutation):
    directory, candidate, environment = _release_handoff(tmp_path)
    assert _check_release_handoff(directory, environment).returncode == 0
    package_hash = msix.sha256(directory / candidate["package_file"])
    selected = msix.load_profile(ROOT / "packaging/msix/profiles/production-direct.json", production=True)
    delivery = msix.read_json(ROOT / "packaging/msix/direct-delivery.json")
    package_uri = delivery["hosting"]["public_origin"] + delivery["hosting"]["base_path"] + "packages/" + package_hash + "/" + candidate["package_file"]
    descriptor = msix.render_appinstaller(selected, package_uri)
    if mutation == "descriptor-policy":
        descriptor = descriptor.replace(b'ShowPrompt="false"', b'ShowPrompt="true"')
    (directory / "ConversationAnalytics.appinstaller").write_bytes(descriptor)
    (directory / "desktop.verification.json").write_bytes(msix.canonical_json({"signature": "trusted-timestamped"}))
    final = {key: candidate[key] for key in ("source_commit", "source_tree", "target", "application_version", "package_version",
        "package_file", "profile_sha256", "inventory_sha256", "build_sha256", "expected_family", "agent_file", "agent_sha256")}
    final.update(schema="ofca-desktop-artifact-release/v1", package_sha256=package_hash,
        signature_scope="trusted-timestamped", verification_sha256=msix.sha256(directory / "desktop.verification.json"),
        resolved_direct_inputs_sha256=msix.sha256(directory / "desktop.resolved-direct-inputs.json"), descriptor_sha256=msix.sha256(directory / "ConversationAnalytics.appinstaller"))
    if mutation == "final-hash":
        final["package_sha256"] = "0" * 64
    if mutation == "store-scope":
        final["signature_scope"] = "unsigned-store-submission"
    (directory / "desktop.artifact-release.json").write_bytes(msix.canonical_json(final))
    verification, admission = tmp_path / "fresh-verification.json", tmp_path / "publication-admission.json"
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "packaging/assert-direct-publication.ps1"),
        "-Directory", str(directory), "-ProductRevision", candidate["source_commit"],
        "-FreshVerificationPath", str(verification), "-AdmissionPath", str(admission)],
        env=environment, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not admission.exists() and not verification.exists()
    assert not Path(str(verification) + ".descriptor.xml").exists()
    if mutation == "forged-receipt":
        assert "verify-msix: rejected" in result.stdout  # Real verifier was reached.


@pytest.mark.skipif(os.name != "nt", reason="Windows SDK runtime PE verification")
@pytest.mark.parametrize("failure", ["source", "pe"])
def test_release_stage_refuses_before_packing_an_unqualified_runtime(tmp_path, failure):
    build = tmp_path / "build"
    runtime = build / "dist/Brain"
    runtime.mkdir(parents=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (runtime / "release-manifest.json").write_bytes(msix.canonical_json({
        "source_commit": "3" * 40 if failure == "source" else revision,
    }))
    for name in ("Brain.exe", "Bridge.exe"):
        (runtime / name).write_bytes(b"not a runtime PE")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "packaging/stage-msix-release.ps1"),
        "-Target", "msix-direct", "-BuildRoot", str(build), "-BuildPython", sys.executable,
        "-ProductRevision", revision], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert not (build / "msix").exists() and not (build / "msix-release").exists()
    if failure == "source":
        assert "Runtime source differs" in result.stderr
    else:
        assert "embedded application manifest" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell release handoff")
@pytest.mark.parametrize("filename", ["desktop.profile.json", "desktop.inventory.json", "desktop.build.json",
    "ConversationAnalytics-Agent-2.0.3-chrome.zip", "desktop.appinstaller-template.xml", "package"])
def test_release_handoff_refuses_changed_sealed_bytes(tmp_path, filename):
    directory, record, environment = _release_handoff(tmp_path)
    selected = record["package_file"] if filename == "package" else filename
    with (directory / selected).open("ab") as output:
        output.write(b"changed")
    result = _check_release_handoff(directory, environment)
    assert result.returncode != 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell release environment verification")
@pytest.mark.parametrize("variable", ["MSIX_DIRECT_PUBLISHER", "MSIX_DIRECT_EXPECTED_FAMILY",
    "MSIX_DIRECT_APPINSTALLER_URI", "DESKTOP_R2_BUCKET", "DESKTOP_R2_ENDPOINT", "DESKTOP_UPDATES_BASE_URL"])
def test_release_handoff_refuses_environment_drift(tmp_path, variable):
    directory, _, environment = _release_handoff(tmp_path)
    environment[variable] += "/changed"
    result = _check_release_handoff(directory, environment)
    assert result.returncode != 0 and "Release environment input disagrees" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell release authority verification")
@pytest.mark.parametrize("change", ["family", "source", "tree", "version", "dirty", "profile", "profile_case", "extra", "duplicate",
    "downgrade", "template_identity", "self_redirect", "template_dtd"])
def test_release_handoff_refuses_substituted_release_authority(tmp_path, change):
    directory, record, environment = _release_handoff(tmp_path)
    if change == "family":
        record["expected_family"] = "Another.Family"
    elif change == "source":
        record["source_commit"] = "3" * 40
    elif change == "tree":
        record["source_tree"] = "2" * 40
    elif change == "version":
        record["application_version"] = "0.7.4"
    elif change == "dirty":
        path = directory / "desktop.build.json"
        document = msix.read_json(path)
        document["producer_source"]["dirty"] = True
        path.write_bytes(msix.canonical_json(document))
        record["build_sha256"] = msix.sha256(path)
    elif change == "profile":
        path = directory / "desktop.profile.json"
        document = msix.read_json(path)
        document["identity"]["publisher"] += ", O=Changed"
        path.write_bytes(msix.canonical_json(document))
        record["profile_sha256"] = msix.sha256(path)
    elif change == "profile_case":
        path = directory / "desktop.profile.json"
        document = msix.read_json(path)
        document["Identity"] = document.pop("identity")
        path.write_bytes(msix.canonical_json(document))
        record["profile_sha256"] = msix.sha256(path)
    elif change == "extra":
        (directory / "unrelated.msix").write_bytes(b"not the declared package")
    elif change in {"downgrade", "template_identity", "self_redirect", "template_dtd"}:
        path = directory / "desktop.appinstaller-template.xml"
        document = path.read_bytes()
        if change == "downgrade":
            document = document.replace(b'<AutomaticBackgroundTask', b'<ForceUpdateFromAnyVersion>true</ForceUpdateFromAnyVersion><AutomaticBackgroundTask')
        elif change == "template_identity":
            document = document.replace(b'Name="Dipsy.OnlyFansConversationalAnalytics"', b'Name="Another.Name"')
        elif change == "self_redirect":
            document = document.replace(b'Uri="https://updates.dipsy.fi/desktop/v1/windows', b'Uri="https://redirect.example/windows')
        else:
            document = document.replace(b'<AppInstaller ', b'<!DOCTYPE AppInstaller [<!ENTITY injected "x">]><AppInstaller ')
        path.write_bytes(document)
        record["appinstaller_template_sha256"] = msix.sha256(path)
    else:
        path = directory / "desktop.release-inputs.json"
        path.write_bytes(msix.canonical_json(record).replace(b'{', b'{"target":"msix-store",', 1))
        result = _check_release_handoff(directory, environment)
        assert result.returncode != 0 and "Duplicate release JSON" in result.stderr
        return
    (directory / "desktop.release-inputs.json").write_bytes(msix.canonical_json(record))
    result = _check_release_handoff(directory, environment)
    assert result.returncode != 0, result.stdout + result.stderr


def test_manifest_escapes_identity_and_preserves_protocol_and_storage_policy():
    value = profile()
    value["identity"]["publisher"] += ", O=A & B"
    manifest = msix.render_manifest(value)
    root = ET.fromstring(manifest)
    ns = {key or "f": uri for key, uri in msix.NAMESPACES.items()}
    assert root.find("f:Identity", ns).attrib["Publisher"] == value["identity"]["publisher"]
    assert root.find(".//uap10:Protocol", ns).attrib == {"Name": "ofca", "Parameters": '--open-workspace "%1"'}
    assert root.find(".//uap17:UpdateWhileInUse", ns).text == "defer"
    assert root.find(".//virtualization:ExcludedDirectory", ns).text == r"$(KnownFolder:LocalAppData)\OnlyFans Conversational Analytics"
    assert {item.attrib["Name"] for item in root.findall("f:Capabilities/rescap:Capability", ns)} == {"runFullTrust", "unvirtualizedResources"}
    assert "RegistryWriteVirtualization" not in manifest.decode()
    assert msix.render_manifest(value) == manifest


def test_installed_path_budget_does_not_depend_on_global_long_path_configuration():
    assert msix.validate_installed_path_budget(["Brain.exe", "_internal/app/core/config.pyc"], profile()) < 260
    with pytest.raises(ValueError, match="path budget"):
        msix.validate_installed_path_budget(["_internal/" + "segment/" * 30 + "runtime.dll"], profile())


def test_msix_part_names_decode_once_without_form_url_plus_or_path_aliases():
    assert msix.installed_archive_path('app/tzdata/GMT%2B1') == 'app/tzdata/GMT+1'
    assert msix.installed_archive_path('app/encoded%2Bname%20%25value.txt') == 'app/encoded+name %value.txt'
    assert msix.installed_archive_path('app/literal%252B.txt') == 'app/literal%2B.txt'
    for unsafe in ('app/%2FBrain.exe', 'app/%5cBrain.exe', 'app/%2e%2e/Brain.exe', 'app/%00name', 'app/invalid%FF', 'app/invalid%', '%41ppxSignature.p7x'):
        with pytest.raises(ValueError):
            msix.installed_archive_path(unsafe)


def test_msix_excludes_build_inputs_only_after_full_qualification(stage):
    (stage / "ensure-webview2.ps1").unlink()
    selected = installed_runtime_files(stage, "msix")
    assert "Brain.exe" in selected and "Bridge.exe" in selected
    assert "ensure-webview2.ps1" not in selected
    assert not any(path.startswith("Agent/") for path in selected)
    assert any(finding.path == "ensure-webview2.ps1" for finding in verify_runtime_files(stage, adapters=("inno",)))
    (stage / "Agent/build-meta.json").unlink()
    with pytest.raises(ValueError, match="staged runtime rejected"):
        installed_runtime_files(stage, "msix")


def test_msix_does_not_hide_unsafe_input_when_it_selects_files(stage):
    (stage / "private.pfx").write_bytes(b"not-release-material")
    with pytest.raises(ValueError, match="staged runtime rejected"):
        installed_runtime_files(stage, "msix")


def _prepared_and_sealed(stage, tmp_path, profile_path=PROFILE_PATH):
    working = tmp_path / "msix-output"
    msix.prepare_payload(stage, working, profile_path)
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/write-digests.ps1"),
        "-Directory", str(working / "payload/app"),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    msix.seal_payload(working, profile_path, Path(sys.executable))
    return working


@pytest.mark.skipif(os.name != "nt", reason="Windows digest writer")
def test_sealing_rejects_runtime_changes_after_qualification(stage, tmp_path):
    working = _prepared_and_sealed(stage, tmp_path)
    (working / "payload/app/Brain.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after stage qualification"):
        msix.seal_payload(working, PROFILE_PATH, Path(sys.executable))


@pytest.mark.skipif(os.name != "nt", reason="Windows digest writer")
def test_archive_verifier_rejects_tampered_and_extra_payload(stage, tmp_path):
    working = _prepared_and_sealed(stage, tmp_path)
    package = tmp_path / "qualification.msix"
    def write_package(tamper=False, extra=False):
        with zipfile.ZipFile(package, "w") as archive:
            for path in (working / "payload").rglob("*"):
                if path.is_file():
                    relative = path.relative_to(working / "payload").as_posix()
                    archive.writestr(relative, b"tampered" if tamper and relative == "app/Brain.exe" else path.read_bytes())
            archive.writestr("AppxBlockMap.xml", b"footprint-for-negative-test")
            archive.writestr("[Content_Types].xml", b"footprint-for-negative-test")
            if extra:
                archive.writestr("app/private.pfx", b"unexpected")
    write_package()
    receipt = msix.verify_archive(package, working / "inventory.json", PROFILE_PATH)
    assert receipt["package_version"] == "1.0.1.0"
    write_package(tamper=True)
    with pytest.raises(ValueError, match="size|digest"):
        msix.verify_archive(package, working / "inventory.json", PROFILE_PATH)
    write_package(extra=True)
    with pytest.raises(ValueError, match="extra or missing"):
        msix.verify_archive(package, working / "inventory.json", PROFILE_PATH)


@pytest.mark.skipif(os.name != "nt", reason="Windows manifest policy verifier")
@pytest.mark.parametrize("profile_name", ["qualification-direct", "qualification-store"])
def test_dependency_policy_cannot_be_reversed_by_resealing_inventory(stage, tmp_path, profile_name):
    profile_path = ROOT / f"packaging/msix/profiles/{profile_name}.json"
    working = _prepared_and_sealed(stage, tmp_path, profile_path)
    manifest_path = working / "payload/AppxManifest.xml"
    manifest = ET.fromstring(manifest_path.read_bytes())
    dependency = manifest.find(".//win32dependencies:ExternalDependency", msix.NAMESPACES)
    dependency.set("Optional", "false" if dependency.attrib["Optional"] == "true" else "true")
    manifest_path.write_bytes(ET.tostring(manifest, encoding="utf-8", xml_declaration=True))
    inventory_path = working / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    record = next(entry for entry in inventory["files"] if entry["path"] == "AppxManifest.xml")
    record.update(msix.file_record(manifest_path, "AppxManifest.xml"))
    inventory_path.write_text(json.dumps(inventory))
    package = tmp_path / "reversed-policy.msix"
    with zipfile.ZipFile(package, "w") as archive:
        for path in (working / "payload").rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(working / "payload").as_posix())
        archive.writestr("AppxBlockMap.xml", b"footprint-for-negative-test")
        archive.writestr("[Content_Types].xml", b"footprint-for-negative-test")
    with pytest.raises(ValueError, match="expected identity and policy"):
        msix.verify_archive(package, inventory_path, profile_path)
    result = subprocess.run([
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/verify-msix.ps1"),
        "-Package", str(package), "-Inventory", str(inventory_path), "-PackageProfile", str(profile_path),
        "-AllowQualification", "-VerificationMode", "StoreSubmission" if profile_name.endswith("store") else "DirectSigned",
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "Manifest identity or policy differs" in result.stdout, result.stdout + result.stderr


def test_appinstaller_has_one_fixed_update_owner_and_no_downgrade_policy():
    value = profile()
    package_uri = "https://qualification.example.test/desktop/direct/1.0.1.0.msix"
    xml = msix.render_appinstaller(value, package_uri)
    root = ET.fromstring(xml)
    namespace = {"a": "http://schemas.microsoft.com/appx/appinstaller/2018"}
    assert root.attrib["Uri"] == value["app_installer_uri"]
    assert root.find("a:MainPackage", namespace).attrib["Uri"] == package_uri
    assert root.find("a:UpdateSettings/a:OnLaunch", namespace).attrib == {
        "HoursBetweenUpdateChecks": "6", "ShowPrompt": "false", "UpdateBlocksActivation": "false",
    }
    assert root.find("a:UpdateSettings/a:AutomaticBackgroundTask", namespace) is not None
    assert b"ForceUpdateFromAnyVersion" not in xml
    with pytest.raises(ValueError, match="approved descriptor origin"):
        msix.render_appinstaller(value, "https://other.example.test/package.msix")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows SDK")
@pytest.mark.ci_tier("integration")
@pytest.mark.serial
@pytest.mark.parametrize("profile_name", ["qualification-direct", "qualification-store"])
def test_real_sdk_produces_and_unpacks_the_same_installed_closure(stage, tmp_path, profile_name):
    assert MAKEAPPX.is_file(), "Install the pinned Windows SDK 10.0.26100.0; MSIX validation must not silently skip"
    # Real PE files permit SDK semantic inspection. This test does not claim a
    # frozen-Brain startup or installed journey from stand-in executables.
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    for name in ("Bridge.exe", "Brain.exe"):
        shutil.copyfile(executable, stage / name)
    (stage / '_internal/encoded+name %value.txt').write_bytes(b'reviewed-msix-name-encoding-marker')
    profile_path = ROOT / f"packaging/msix/profiles/{profile_name}.json"
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/build-msix.ps1"),
        "-BuildPython", sys.executable, "-StagingRoot", str(stage), "-PackageProfile", str(profile_path),
        "-OutputRoot", str(tmp_path / "package"),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    packages = list((tmp_path / "package").glob("*.msix"))
    assert len(packages) == 1
    inventory = next((tmp_path / "package").glob("*.inventory.json"))
    msix.verify_archive(packages[0], inventory, profile_path)
    msix.verify_unpacked(tmp_path / "package/working/unpacked", inventory)
    with zipfile.ZipFile(packages[0]) as archive:
        assert "app/Brain.exe" in archive.namelist()
        assert "app/_internal/app/core/signing-parameter-release.json" in archive.namelist()
        assert not any("/Agent/" in name or name.endswith("ensure-webview2.ps1") for name in archive.namelist())
        assert "app/_internal/encoded+name %value.txt" in {
            msix.installed_archive_path(name) for name in archive.namelist()
        }
    verifier = [
        "pwsh", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/verify-msix.ps1"),
        "-Package", str(packages[0]), "-Inventory", str(inventory), "-PackageProfile", str(profile_path),
    ]
    production = subprocess.run(verifier, capture_output=True, text=True, check=False)
    assert production.returncode == 2 and "refuses qualification" in production.stdout, production.stdout + production.stderr
    mode = "StoreSubmission" if profile_name.endswith("store") else "DirectSigned"
    verified = subprocess.run(verifier + ["-AllowQualification", "-VerificationMode", mode],
                              capture_output=True, text=True, check=False)
    if mode == "StoreSubmission":
        assert verified.returncode == 0, verified.stdout + verified.stderr
        receipt = json.loads(verified.stdout)
        assert receipt["identity"]["FamilyName"].startswith("OFCA.Qualification.Store_")
        assert receipt["identity"]["FullName"].startswith("OFCA.Qualification.Store_1.0.1.0_x64__")
        assert receipt["signature"] == "unsigned-store-submission"
        family = subprocess.run(verifier + ["-AllowQualification", "-VerificationMode", mode,
                                          "-ExpectedFamilyName", "OFCA.Qualification.Store_wrong"],
                                capture_output=True, text=True, check=False)
        assert family.returncode == 2 and "family mismatch" in family.stdout, family.stdout + family.stderr
    else:
        assert verified.returncode == 2 and "requires an MSIX signature" in verified.stdout, verified.stdout + verified.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows release verifier")
@pytest.mark.parametrize("case", ["empty", "exe", "duplicate-profile", "bundle-duplicate-profile"])
def test_release_verifier_rejects_missing_wrong_or_ambiguous_inputs(tmp_path, case):
    arguments = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/verify-msix.ps1")]
    if case != "empty":
        package = tmp_path / ("unrelated.exe" if case == "exe" else
                              "fake.msixbundle" if case == "bundle-duplicate-profile" else "fake.msix")
        package.write_bytes(b"not-an-msix")
        inventory = tmp_path / "inventory.json"
        inventory.write_text("{}")
        selected_profile = tmp_path / "profile.json"
        selected_profile.write_text(PROFILE_PATH.read_text().replace('{', '{"purpose":"qualification",', 1))
        arguments += ["-Package", str(package), "-Inventory", str(inventory), "-PackageProfile", str(selected_profile), "-AllowQualification"]
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    assert result.returncode == 2, result.stdout + result.stderr
    expected = {"empty": "Name an MSIX", "exe": "requires an MSIX", "duplicate-profile": "Duplicate JSON member",
                "bundle-duplicate-profile": "Duplicate JSON member"}
    assert expected[case] in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="embedded Windows PE resources")
@pytest.mark.ci_tier("integration")
@pytest.mark.serial
def test_pe_manifest_gate_reads_embedded_resources_and_rejects_missing_opt_in(tmp_path):
    assert MANIFEST_TOOL.is_file(), "Pinned SDK manifest inspection must not skip"
    executable = tmp_path / "application.exe"
    source = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    shutil.copyfile(source, executable)
    inspect = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
               str(ROOT / "packaging/verify-pe-manifests.ps1"), "-Path", str(executable)]
    result = subprocess.run(inspect, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["executables"][0]["long_path_aware"] is True
    # Change only the copied fixture resource, never the system executable or
    # an already packed MSIX. No executable is launched by this test.
    manifest = tmp_path / "unsafe.manifest"
    manifest.write_text('''<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false" />
      </requestedPrivileges></security></trustInfo>
      <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">false</longPathAware>
      </windowsSettings></application></assembly>''', encoding="utf-8")
    changed = subprocess.run([str(MANIFEST_TOOL), "-nologo", "-manifest", str(manifest),
                              f"-outputresource:{executable};#1"], capture_output=True, text=True, check=False)
    assert changed.returncode == 0, changed.stdout + changed.stderr
    rejected = subprocess.run(inspect, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0 and "must embed longPathAware" in rejected.stderr, rejected.stdout + rejected.stderr

    # The GUI executable needs an explicit process default before any HWND is
    # created. Exercise the resource gate, including a valid but wrong DPI mode.
    bridge = tmp_path / "Bridge.exe"
    shutil.copyfile(source, bridge)
    bridge_inspect = inspect[:-1] + [str(bridge)]
    valid_manifest = (ROOT / "desktop/src-tauri/bridge.manifest").read_text(encoding="utf-8")
    for dpi_mode in ("missing", "system", "PerMonitorV2"):
        contents = valid_manifest
        if dpi_mode == "missing":
            contents = "\n".join(line for line in contents.splitlines() if "<dpiAware" not in line)
        elif dpi_mode == "system":
            contents = contents.replace(">PerMonitorV2<", ">system<")
        manifest.write_text(contents, encoding="utf-8")
        changed = subprocess.run([str(MANIFEST_TOOL), "-nologo", "-manifest", str(manifest),
                                  f"-outputresource:{bridge};#1"], capture_output=True, text=True, check=False)
        assert changed.returncode == 0, changed.stdout + changed.stderr
        inspected = subprocess.run(bridge_inspect, capture_output=True, text=True, check=False)
        if dpi_mode == "PerMonitorV2":
            assert inspected.returncode == 0, inspected.stdout + inspected.stderr
            record = json.loads(inspected.stdout)["executables"][0]
            assert record["dpi_awareness"] == "PerMonitorV2"
            assert record["legacy_dpi_aware"] == "true/pm"
        else:
            assert inspected.returncode != 0 and "must embed PerMonitorV2" in inspected.stderr, inspected.stdout + inspected.stderr


# Verified direct-distribution inputs share this registered Windows packaging suite.
INPUTS = ROOT / 'packaging/msix/direct-delivery.json'
PROFILE = ROOT / 'packaging/msix/profiles/production-direct.json'


def test_production_direct_inputs_are_complete_and_match_the_producer():
    data = msix.read_json(INPUTS)
    profile = msix.load_profile(PROFILE, production=True)
    assert data['profile'] == PROFILE.relative_to(ROOT).as_posix()
    assert profile['purpose'] == 'production'
    assert profile['distribution'] == 'direct-appinstaller'
    assert profile['identity']['publisher'] == data['signing']['subject'] == 'CN=Dipsy, O=Dipsy, L=London, C=GB'
    assert profile['app_installer_uri'] == data['hosting']['app_installer_uri']
    assert profile['minimum_os_version'] == '10.0.26200.0'
    # Input provisioning does not authorize a product release.
    assert data['stable_feed_published'] is False


def test_store_and_qualification_authority_remain_separate():
    direct = msix.load_profile(PROFILE, production=True)
    store = msix.load_profile(ROOT / 'packaging/msix/profiles/production-store.json', production=True)
    assert store['app_installer_uri'] is None
    assert direct['identity']['publisher'] != store['identity']['publisher']
    assert direct['identity']['name'] != store['identity']['name']
    with pytest.raises(ValueError):
        msix.load_profile(ROOT / 'packaging/msix/profiles/production-direct.template.json', production=True)
    for name in ('qualification-direct.json', 'qualification-store.json'):
        with pytest.raises(ValueError):
            msix.load_profile(ROOT / 'packaging/msix/profiles' / name, production=True)


def test_appinstaller_output_has_exact_source_and_the_same_package_origin():
    profile = msix.load_profile(PROFILE, production=True)
    version = msix.package_version(profile['release_sequence'])
    uri = 'https://updates.dipsy.fi/desktop/v1/packages/' + 'a' * 64 + f'/ConversationAnalytics_{version}_x64.msix'
    tree = ET.fromstring(msix.render_appinstaller(profile, uri))
    ns = {'a': 'http://schemas.microsoft.com/appx/appinstaller/2018'}
    assert tree.attrib['Uri'] == profile['app_installer_uri']
    package = tree.find('a:MainPackage', ns)
    assert package.attrib['Publisher'] == profile['identity']['publisher']
    assert package.attrib['Uri'] == uri
    assert tree.find('a:UpdateSettings/a:ForceUpdateFromAnyVersion', ns) is None


def test_serving_owner_has_only_dedicated_desktop_buckets():
    config = json.loads((ROOT / 'tools/desktop-delivery/wrangler.jsonc').read_text(encoding='utf-8'))
    inputs = msix.read_json(INPUTS)
    assert config['name'] == inputs['hosting']['worker']
    assert config['workers_dev'] is False and config['preview_urls'] is False
    assert config['routes'][0]['pattern'] == 'updates.dipsy.fi/desktop/v1/*'
    assert {b['bucket_name'] for b in config['r2_buckets']} == {inputs['hosting']['bucket'], inputs['hosting']['qualification_bucket']}
    assert all(b['jurisdiction'] == 'eu' for b in config['r2_buckets'])
    assert 'dipsy-signing-parameters' not in {b['bucket_name'] for b in config['r2_buckets']}


@pytest.mark.skipif(os.name != 'nt', reason='Windows Appx identity derivation')
def test_verified_direct_family_is_derived_by_windows(tmp_path):
    profile = msix.load_profile(PROFILE, production=True)
    manifest = tmp_path / 'AppxManifest.xml'
    manifest.write_bytes(msix.render_manifest(profile))
    identity = _read_windows_package_identity(manifest, tmp_path)
    assert identity['Publisher'] == profile['identity']['publisher']
    assert identity['FamilyName'] == msix.read_json(INPUTS)['expected_package_family']
    malformed = tmp_path / "MalformedManifest.xml"
    malformed.write_bytes(b"<Package>")
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        _read_windows_package_identity(malformed, tmp_path)
    assert not rejected.value.stdout.strip(), "A malformed manifest must not produce an accepted identity"
    diagnostic = rejected.value.__notes__[0]
    for phase in ("script-entry", "add-type-complete", "windows-read-start", "exit"):
        assert f"ofca-msix-identity-probe:{phase}:" in diagnostic
    assert "ofca-msix-identity-probe:windows-read-complete:" not in diagnostic
