"""Optional-package metadata refuses identity and executable substitution."""
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.nlp.contracts import EngineIdentity
from tools import optional_msix as optional

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


def identity(**changes):
    value = dict(package_name="OFCA.Qualification.Text.Engine",
                 package_family="OFCA.Qualification.Text.Engine_0123456789abc",
                 core_package_name="OFCA.Qualification.Text.Main", core_package_version="1.0.1.0",
                 publisher="CN=OFCA MSIX Qualification Text", package_version="1.0.2.0",
                 related_set_id="text-set-1", engine_version="0.1.0", platform="windows-x64")
    value.update(changes)
    return EngineIdentity(**value)


@pytest.mark.parametrize("changes", [
    {"package_name": "a:b"}, {"core_package_name": "OFCA.Qualification.Text.Engine"},
    {"publisher": "CN=bad\nvalue"}, {"package_family": "Other_0123456789abc"},
    {"package_version": "1.0.01.0"}, {"core_package_version": "0.0.1.0"},
])
def test_identity_substitution_refused(changes):
    with pytest.raises(ValueError):
        optional.validate_identity(identity(**changes))


def test_optional_manifest_has_main_dependency_without_activation_entry():
    root = ET.fromstring(optional.optional_manifest(identity(), minimum_os="10.0.26100.0", tested_os="10.0.26340.0"))
    assert root.find(f".//{{{optional.UAP3}}}MainPackageDependency").attrib == {"Name": identity().core_package_name}
    assert root.find(f"{{{optional.FOUNDATION}}}Applications") is None
    assert root.find(f"{{{optional.FOUNDATION}}}Identity").get("Version") == "1.0.2.0"


def test_related_set_is_external_and_does_not_eagerly_install():
    mapping = optional.bundle_mapping("main.msix", "engine.msixbundle").decode()
    assert mapping == '[Files]\n"main.msix" "main.msix"\n[ExternalPackages]\n"engine.msixbundle" "engine.msixbundle"\n'
    root = ET.fromstring(optional.appinstaller(identity(), descriptor_uri="https://packages.example.test/set.appinstaller",
                        core_uri="https://packages.example.test/main.msixbundle", optional_uri="https://packages.example.test/engine.msixbundle"))
    assert root.find(f"{{{optional.APPINSTALLER}}}Dependencies") is None
    related = root.find(f"{{{optional.APPINSTALLER}}}RelatedPackages/{{{optional.APPINSTALLER}}}Bundle")
    assert related.get("Version") == "1.0.2.0"


@pytest.mark.parametrize("uri", ["http://packages.example.test/a.msixbundle", "https://u:p@packages.example.test/a.msixbundle",
                                 "https://packages.example.test/a.msixbundle?q=1", "https://packages.example.test/%2e%2e/a.msixbundle",
                                 "https://packages.example.test/../a.msixbundle", "https://other.example.test/a.msixbundle"])
def test_arbitrary_artifact_sources_refused(uri):
    with pytest.raises(ValueError):
        optional.appinstaller(identity(), descriptor_uri="https://packages.example.test/set.appinstaller",
                              core_uri="https://packages.example.test/main.msixbundle", optional_uri=uri)


@pytest.mark.parametrize("name", ["../engine.msixbundle", 'engine".msixbundle', "engine.msixbundle\n", "engine.exe"])
def test_mapping_injection_refused(name):
    with pytest.raises(ValueError):
        optional.bundle_mapping("main.msix", name)


@pytest.mark.parametrize("mutation", ["version", "namespace", "root", "main_architecture", "missing_packages", "schema_version", "missing_main_payload", "external_version", "embedded_optional"])
def test_bundle_metadata_refuses_substitution(tmp_path, mutation):
    path = tmp_path / "main.msixbundle"
    namespace = "http://schemas.microsoft.com/appx/2016/bundle"
    root = ET.Element(f"{{{namespace}}}Bundle", SchemaVersion="5.0")
    ET.SubElement(root, f"{{{namespace}}}Identity", Name=identity().core_package_name, Publisher=identity().publisher, Version="1.0.1.0")
    packages = ET.SubElement(root, f"{{{namespace}}}Packages")
    ET.SubElement(packages, f"{{{namespace}}}Package", Type="application", Architecture="x64", Version="1.0.1.0", FileName="main.msix")
    link = ET.SubElement(root, f"{{{namespace}}}OptionalBundle", Name=identity().package_name, Publisher=identity().publisher,
                         Version="1.0.2.0", FileName="engine.msixbundle")
    external = ET.SubElement(link, f"{{{namespace}}}Package", Type="application", Architecture="x64", Version="1.0.2.0", FileName="engine.msix")
    def write(*, omit_main=False, embed_optional=False):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AppxMetadata/AppxBundleManifest.xml", ET.tostring(root))
            if not omit_main:
                archive.writestr("main.msix", b"mechanical package fixture")
            if embed_optional:
                archive.writestr("engine.msixbundle", b"mechanical package fixture")
    write()
    optional.verify_related_bundle(path, identity())
    if mutation == "version":
        link.set("Version", "1.0.3.0")
    elif mutation == "namespace":
        root.tag = "{urn:other}Bundle"
    elif mutation == "root":
        root.tag = f"{{{namespace}}}NotABundle"
    elif mutation == "main_architecture":
        packages[0].set("Architecture", "arm64")
    elif mutation == "missing_packages":
        root.remove(packages)
    elif mutation == "schema_version":
        root.set("SchemaVersion", "99.0")
    elif mutation == "external_version":
        external.set("Version", "1.0.3.0")
    write(omit_main=mutation == "missing_main_payload", embed_optional=mutation == "embedded_optional")
    with pytest.raises(ValueError):
        optional.verify_related_bundle(path, identity())


def test_worker_path_must_exist_in_payload(tmp_path):
    with pytest.raises(ValueError):
        optional.verified_worker_path(tmp_path)
    worker = tmp_path / optional.WORKER_PATH
    worker.parent.mkdir()
    worker.write_bytes(b"mechanical executable fixture")
    assert optional.verified_worker_path(tmp_path) == worker


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse points")
@pytest.mark.parametrize("link_root", [False, True])
def test_worker_path_refuses_junction_even_inside_payload(tmp_path, link_root):
    root = tmp_path / "payload"
    root.mkdir()
    target = root / "actual"
    target.mkdir()
    (target / "TextAnalysisWorker.exe").write_bytes(b"mechanical fixture")
    link = tmp_path / "linked-root" if link_root else root / "worker"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root if link_root else target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    with pytest.raises(ValueError, match="link"):
        optional.verified_worker_path(link if link_root else root)


@pytest.mark.skipif(os.name != "nt", reason="Windows SDK construction")
def test_sdk_constructs_exact_external_related_set(tmp_path):
    from tools import msix_packaging as msix
    sdk = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10/bin/10.0.26100.0/x64/makeappx.exe"
    assert sdk.is_file(), "Windows SDK required"
    engine = identity()
    main, worker = tmp_path / "main", tmp_path / "optional"
    for folder in (main, worker):
        folder.mkdir()
        shutil.copytree(msix.CONFIG_ROOT / "Assets", folder / "Assets")
    profile = msix.load_profile(msix.CONFIG_ROOT / "profiles/qualification-direct.json")
    profile["identity"].update(name=engine.core_package_name, publisher=engine.publisher)
    (main / "AppxManifest.xml").write_bytes(msix.render_manifest(profile))
    (main / "app").mkdir()
    (main / "app/Bridge.exe").write_bytes(b"mechanical package fixture")
    (worker / "AppxManifest.xml").write_bytes(optional.optional_manifest(engine, minimum_os="10.0.26100.0", tested_os="10.0.26340.0"))
    (worker / "worker").mkdir()
    (worker / optional.WORKER_PATH).write_bytes(b"mechanical package fixture")
    def sdk_run(*args):
        result = subprocess.run([str(sdk), *args], cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    sdk_run("pack", "/d", str(main), "/p", "main.msix", "/no")
    sdk_run("pack", "/d", str(worker), "/p", "engine.msix", "/no")
    (tmp_path / "engine-map.txt").write_text('[Files]\n"engine.msix" "engine.msix"\n', encoding="utf-8")
    sdk_run("bundle", "/f", "engine-map.txt", "/p", "engine.msixbundle", "/bv", engine.package_version, "/no")
    (tmp_path / "main-map.txt").write_bytes(optional.bundle_mapping("main.msix", "engine.msixbundle"))
    sdk_run("bundle", "/f", "main-map.txt", "/p", "main.msixbundle", "/bv", engine.core_package_version, "/no")
    optional.verify_related_bundle(tmp_path / "main.msixbundle", engine)
