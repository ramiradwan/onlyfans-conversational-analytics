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
pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


def profile():
    return msix.load_profile(PROFILE_PATH)


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


@pytest.mark.skipif(os.name != "nt", reason="Windows Appx identity calculation")
def test_reserved_store_identity_matches_windows_family_without_registration(tmp_path):
    reserved = msix.read_json(ROOT / "packaging/msix/store-reservation.json")
    store = msix.load_profile(ROOT / "packaging/msix/profiles/production-store.json", production=True)
    assert store["distribution"] == "microsoft-store" and store["app_installer_uri"] is None
    assert store["identity"]["publisher_display_name"] == reserved["publisher_display_name"]
    assert msix.version_tuple(store["minimum_os_version"]) >= (10, 0, 26200, 0)
    manifest = tmp_path / "AppxManifest.xml"
    manifest.write_bytes(msix.render_manifest(store))
    script = tmp_path / "identity.ps1"
    script.write_text("""param([string]$Binding, [string]$Manifest)
$ErrorActionPreference = 'Stop'
Add-Type -Path $Binding
[Ofca.Msix.IdentityReader]::Read([IO.File]::ReadAllBytes($Manifest)) | ConvertTo-Json -Compress
""", encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script),
                             str(ROOT / "packaging/msix/ReadPackageIdentity.cs"), str(manifest)],
                            capture_output=True, text=True, timeout=30, check=True)
    identity = json.loads(result.stdout)
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


def _prepared_and_sealed(stage, tmp_path):
    working = tmp_path / "msix-output"
    msix.prepare_payload(stage, working, PROFILE_PATH)
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/write-digests.ps1"),
        "-Directory", str(working / "payload/app"),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    msix.seal_payload(working, PROFILE_PATH, Path(sys.executable))
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
@pytest.mark.parametrize("case", ["empty", "exe", "duplicate-profile"])
def test_release_verifier_rejects_missing_wrong_or_ambiguous_inputs(tmp_path, case):
    arguments = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "packaging/verify-msix.ps1")]
    if case != "empty":
        package = tmp_path / ("unrelated.exe" if case == "exe" else "fake.msix")
        package.write_bytes(b"not-an-msix")
        inventory = tmp_path / "inventory.json"
        inventory.write_text("{}")
        selected_profile = tmp_path / "profile.json"
        selected_profile.write_text(PROFILE_PATH.read_text().replace('{', '{"purpose":"qualification",', 1))
        arguments += ["-Package", str(package), "-Inventory", str(inventory), "-PackageProfile", str(selected_profile), "-AllowQualification"]
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    assert result.returncode == 2, result.stdout + result.stderr
    expected = {"empty": "Name an MSIX", "exe": "requires an MSIX", "duplicate-profile": "Duplicate JSON member"}
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
    script = tmp_path / 'read-identity.ps1'
    script.write_text('param([string]$Binding,[string]$Manifest)\n'
                      '$ErrorActionPreference="Stop"; Add-Type -Path $Binding; '
                      '[Ofca.Msix.IdentityReader]::Read([IO.File]::ReadAllBytes($Manifest)) | ConvertTo-Json -Compress', encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script),
                             str(ROOT / 'packaging/msix/ReadPackageIdentity.cs'), str(manifest)],
                            check=True, capture_output=True, text=True, timeout=30)
    identity = json.loads(result.stdout)
    assert identity['Publisher'] == profile['identity']['publisher']
    assert identity['FamilyName'] == msix.read_json(INPUTS)['expected_package_family']
