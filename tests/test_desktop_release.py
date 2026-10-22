"""Agreement for immutable desktop metadata and qualification root isolation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.desktop_release import (DesktopReleaseError, PackageIdentity, load_desktop_release,
                                      parse_desktop_release, windows_package_identity)
from app.core.runtime_paths import runtime_data_directory
from tools.msix_packaging import canonical_json, desktop_descriptor, load_profile, sha256

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]
ROOT = Path(__file__).resolve().parents[1]


def fixture(name="qualification-direct"):
    path = ROOT / f"shared/desktop-release/{name}.json"
    document = json.loads(path.read_text())
    identity = PackageIdentity(document["identity"]["name"], document["identity"]["publisher"],
                               document["package_version"], document["identity"]["architecture"])
    return path, document, identity


@pytest.mark.parametrize("name", ["qualification-direct", "qualification-store"])
def test_native_backend_fixture_is_generated_by_the_package_owner(name):
    path, document, identity = fixture(name)
    profile = ROOT / f"packaging/msix/profiles/{name}.json"
    assert path.read_bytes() == canonical_json(desktop_descriptor(load_profile(profile), sha256(profile)))
    assert load_desktop_release(path, identity).update_owner == document["update_owner"]


def test_plain_legacy_requires_no_descriptor_and_packaged_identity_requires_it(tmp_path):
    assert load_desktop_release(tmp_path / "absent.json", None) is None
    path, _, identity = fixture()
    with pytest.raises(DesktopReleaseError, match="packaged_descriptor_missing"):
        load_desktop_release(tmp_path / "absent.json", identity)
    with pytest.raises(DesktopReleaseError, match="descriptor_without_package_identity"):
        load_desktop_release(path, None)


@pytest.mark.parametrize("change", [
    lambda value: value.update(extra=True),
    lambda value: value["identity"].update(extra="unexpected"),
    lambda value: value.update(purpose="production"),
    lambda value: value.update(package_version="1.0.2.0"),
    lambda value: value["identity"].update(publisher="CN=Other"),
    lambda value: value["identity"].update(architecture="arm64"),
    lambda value: value.update(update_owner="legacy-inno"),
    lambda value: value.update(update_owner="microsoft-store"),
    lambda value: value.update(app_installer_uri="http://qualification.example.test/unsafe.appinstaller"),
    lambda value: value.pop("app_installer_uri"),
    lambda value: value.update(storage_policy="package-local"),
    lambda value: value.update(executable_family="replace-brain"),
])
def test_invalid_or_mismatched_descriptor_cannot_select_a_root_or_update_owner(tmp_path, change):
    _, value, identity = fixture()
    change(value)
    path = tmp_path / "desktop-release.json"
    path.write_text(json.dumps(value))
    with pytest.raises(DesktopReleaseError):
        load_desktop_release(path, identity)


def test_duplicate_descriptor_member_is_refused(tmp_path):
    _, value, identity = fixture()
    path = tmp_path / "desktop-release.json"
    path.write_text(json.dumps(value).replace('{', '{"purpose":"qualification",', 1))
    with pytest.raises(DesktopReleaseError):
        load_desktop_release(path, identity)


def test_qualification_defaults_and_overrides_cannot_adopt_production_or_other_profile(tmp_path):
    path, _, identity = fixture()
    release = load_desktop_release(path, identity)
    environ = {"LOCALAPPDATA": str(tmp_path)}
    product = tmp_path / "OnlyFans Conversational Analytics"
    expected = product / "qualification" / identity.name
    assert runtime_data_directory(platform="win32", environ=environ, desktop_release=release) == expected
    assert not product.exists(), "path selection must not create retained state"
    for root in (product, product / "auth", product / "qualification/OFCA.Qualification.Store"):
        with pytest.raises(ValueError, match="cannot adopt"):
            runtime_data_directory(root, platform="win32", environ=environ, desktop_release=release)
    assert runtime_data_directory(expected, platform="win32", environ=environ, desktop_release=release) == expected
    isolated = tmp_path / "isolated-explicit-root"
    assert runtime_data_directory(isolated, platform="win32", environ=environ, desktop_release=release) == isolated
    assert not isolated.exists()


def test_local_python_process_reads_real_os_package_absence():
    # This interpreter is a source-checkout process, never registered as MSIX.
    assert windows_package_identity() is None


def test_shared_v2_target_admission_and_future_target_context():
    cases = json.loads((ROOT / "shared/desktop-release/related-servicing-cases.json").read_bytes())
    for case in cases["cases"]:
        body = (ROOT / "shared/desktop-release" / case["descriptor"]).read_bytes()
        release = parse_desktop_release(body, PackageIdentity(**case["actual"]),
            expected_application_version=(case["expected_application_version"]
                if case["context"] == "current" else None))
        if case["expected_error"]:
            with pytest.raises(DesktopReleaseError, match=case["expected_error"]):
                release.admit_target(case["feed_target_kind"])
        else:
            release.admit_target(case["feed_target_kind"])
    future = cases["cases"][-1]
    with pytest.raises(DesktopReleaseError):
        parse_desktop_release((ROOT / "shared/desktop-release" / future["descriptor"]).read_bytes(),
            PackageIdentity(**future["actual"]),
            expected_application_version=future["expected_application_version"])


@pytest.mark.parametrize("change", [
    lambda d: d["servicing"].update(extra=True),
    lambda d: d["servicing"]["allowed_targets"].update(extra=True),
    lambda d: d["servicing"]["allowed_targets"].update(kind="main_package_only"),
    lambda d: d["servicing"]["installed_artifact"]["engine"].update(package_version="1.0.042.0"),
    lambda d: d["servicing"]["installed_artifact"]["engine"].update(package_version="0.0.42.0"),
    lambda d: d["servicing"]["installed_artifact"].update(related_set_id="bad/path"),
    lambda d: d["servicing"]["allowed_targets"]["engine_identity"].update(family="OFCA.Qualification.TextEngine_aaaaaaaaaaaaa"),
    lambda d: d["servicing"]["allowed_targets"]["engine_identity"].update(publisher="CN=Other"),
    lambda d: d["servicing"]["allowed_targets"]["engine_identity"].update(name="OFCA.Qualification.Direct"),
    lambda d: d.update(update_owner="microsoft-store", app_installer_uri=None),
    lambda d: d.update(schema="ofca-desktop-release/v1"),
])
def test_v2_closed_variants_cannot_widen_layout_or_identity(change):
    value = json.loads((ROOT / "shared/desktop-release/qualification-direct-v2-related.json").read_bytes())
    actual = PackageIdentity(value["identity"]["name"], value["identity"]["publisher"],
        value["package_version"], "x64", "OFCA.Qualification.Direct_0123456789abc")
    change(value)
    with pytest.raises(DesktopReleaseError):
        parse_desktop_release(canonical_json(value), actual)


def test_v2_requires_trusted_main_family_but_four_field_v1_call_stays_compatible():
    path, _, actual = fixture()
    assert load_desktop_release(path, actual) is not None
    value = json.loads((ROOT / "shared/desktop-release/qualification-direct-v2-main-only.json").read_bytes())
    with pytest.raises(DesktopReleaseError):
        parse_desktop_release(canonical_json(value), PackageIdentity(
            value["identity"]["name"], value["identity"]["publisher"], value["package_version"], "x64"))


def test_v2_schema_and_profile_producer_preserve_v1_fixture_bytes():
    from jsonschema import Draft202012Validator
    for name in ("bridge", "main-only", "related"):
        descriptor = json.loads((ROOT / f"shared/desktop-release/qualification-direct-v2-{name}.json").read_bytes())
        schema = json.loads((ROOT / "packaging/msix/desktop-release-v2.schema.json").read_bytes())
        Draft202012Validator(schema).validate(descriptor)
        profile = load_profile(ROOT / "packaging/msix/profiles/qualification-direct.json")
        profile.update(schema="ofca-msix-package-profile/v2", servicing=descriptor["servicing"],
                       application_version=descriptor["application_version"],
                       release_sequence=int(descriptor["package_version"].split(".")[2]))
        assert desktop_descriptor(profile, descriptor["profile_sha256"]) == descriptor
    test_native_backend_fixture_is_generated_by_the_package_owner("qualification-direct")
    test_native_backend_fixture_is_generated_by_the_package_owner("qualification-store")
