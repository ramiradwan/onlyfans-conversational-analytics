"""Optional-package metadata refuses identity and executable substitution."""
import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.nlp.contracts import EngineIdentity
from tools import optional_msix as optional

pytestmark = pytest.mark.ci_tier("fast")


def _engine_quote_inputs(tmp_path):
    """Synthetic verifier records exercise byte binding, not signature trust."""
    from tools import msix_packaging as msix
    from test_related_release import bundles
    fixture = msix.CONFIG_ROOT / "fixtures"
    selected = msix.read_json(fixture / "engine-acquisition-quote-profile.v2.json")
    profile_path = tmp_path / "profile.json"
    profile_path.write_bytes(msix.canonical_json(selected))
    _, engine = bundles(tmp_path, selected)
    canonical = tmp_path / optional.artifact_filename("engine", selected["servicing"]["installed_artifact"]["engine"]["package_version"])
    engine.rename(canonical)
    bundle = dict(file=canonical.name, sha256=msix.sha256(canonical), size_bytes=canonical.stat().st_size)
    inventory = optional.engine_payload_inventory(canonical, selected)
    inventory_path = tmp_path / "engine.inventory.json"
    inventory_path.write_bytes(msix.canonical_json(inventory))
    declared_engine = selected["servicing"]["installed_artifact"]["engine"]
    identity = declared_engine["identity"]
    declaration = dict(schema="optional-engine-disk-budget/v1", engine=declared_engine,
        bundle_sha256=bundle["sha256"], engine_payload_inventory_sha256=msix.sha256(inventory_path),
        disk=dict(installed_space_budget_bytes=sum(record["size"] for record in inventory["files"]),
                  temporary_space_budget_bytes=0, rollback_space_budget_bytes=0))
    declaration_path = tmp_path / "disk.json"
    declaration_path.write_bytes(msix.canonical_json(declaration))
    old_identity = dict(Name=selected["identity"]["name"], Publisher=selected["identity"]["publisher"],
        Version="1.0.1.0", Architecture="x64", FullName="older-core", FamilyName="older-core-family")
    engine_identity = dict(Name=identity["name"], Publisher=identity["publisher"], Version=declared_engine["package_version"],
        Architecture="x64", FamilyName=identity["family"],
        FullName=f'{identity["name"]}_{declared_engine["package_version"]}_x64__{identity["family"].rsplit("_", 1)[1]}')
    verification = dict(schema="ofca-msix-release-verification/v2", purpose=selected["purpose"],
        distribution="direct-appinstaller", mode="DirectSigned", signature="trusted-timestamped",
        identity=old_identity, engine_identity=engine_identity, file_count=1, package_sha256="c" * 64,
        profile_sha256="d" * 64, inventory_sha256="e" * 64,
        artifact_set=dict(kind="related_bundle", related_set_id="older.related.set", engine=dict(**declared_engine,
            artifact=dict(file=bundle["file"], sha256=bundle["sha256"], size=bundle["size_bytes"])),
            core=dict(file="ConversationAnalytics_1.0.1.0_x64.msixbundle", sha256="c" * 64, size=1234)))
    verification_path = tmp_path / "verified.json"
    verification_path.write_bytes(msix.canonical_json(verification))
    inputs = dict(schema="optional-engine-acquisition-quote-inputs/v1", engine_bundle=bundle,
        engine_payload_inventory=dict(file=inventory_path.name, sha256=msix.sha256(inventory_path)),
        disk_budget_declaration=dict(file=declaration_path.name, sha256=msix.sha256(declaration_path)),
        direct_signed_verification=dict(file=verification_path.name, sha256=msix.sha256(verification_path)))
    inputs_path = tmp_path / "quote-inputs.json"
    inputs_path.write_bytes(msix.canonical_json(inputs))
    descriptor = msix.canonical_json(msix.desktop_descriptor(selected, msix.sha256(profile_path)))
    return selected, profile_path, canonical, inputs_path, descriptor


def test_shared_engine_quote_binds_exact_descriptor_and_immutable_expected_artifact():
    from tools import msix_packaging as msix
    fixture = msix.CONFIG_ROOT / "fixtures"
    selected = msix.read_json(fixture / "engine-acquisition-quote-profile.v2.json")
    descriptor = (fixture / "engine-acquisition-quote-descriptor.v2.json").read_bytes()
    raw = (fixture / "engine-acquisition-quote.v1.json").read_bytes()
    quote = optional.validate_acquisition_quote(raw, descriptor, selected)
    assert quote["desktop_descriptor_sha256"] == hashlib.sha256(descriptor).hexdigest()
    assert quote["bundle"]["size_bytes"] == 12345
    assert quote["disk"]["rollback_space_budget_bytes"] == 0


@pytest.mark.parametrize("change", ["unknown", "nested_unknown", "boolean", "float", "negative", "overflow",
    "aggregate", "missing_budget", "zero_installed", "zero_bundle", "descriptor", "relation", "identity", "uri", "file"])
def test_engine_quote_refuses_unbound_or_unsafe_facts(change):
    from tools import msix_packaging as msix
    fixture = msix.CONFIG_ROOT / "fixtures"
    selected = msix.read_json(fixture / "engine-acquisition-quote-profile.v2.json")
    descriptor = (fixture / "engine-acquisition-quote-descriptor.v2.json").read_bytes()
    quote = msix.read_json(fixture / "engine-acquisition-quote.v1.json")
    if change == "unknown": quote["approved"] = True
    elif change == "nested_unknown": quote["bundle"]["observed"] = True
    elif change == "boolean": quote["disk"]["temporary_space_budget_bytes"] = True
    elif change == "float": quote["bundle"]["size_bytes"] = 12345.0
    elif change == "negative": quote["disk"]["rollback_space_budget_bytes"] = -1
    elif change == "overflow": quote["bundle"]["size_bytes"] = optional.MAX_SAFE_INTEGER + 1
    elif change == "aggregate": quote["disk"]["installed_space_budget_bytes"] = optional.MAX_SAFE_INTEGER
    elif change == "missing_budget": del quote["disk"]["rollback_space_budget_bytes"]
    elif change == "zero_installed": quote["disk"]["installed_space_budget_bytes"] = 0
    elif change == "zero_bundle": quote["bundle"]["size_bytes"] = 0
    elif change == "descriptor": descriptor += b" "
    elif change == "relation": quote["related_set_id"] = "another.set"
    elif change == "identity": quote["engine"]["package_version"] = "1.0.43.0"
    elif change == "uri": quote["bundle"]["uri"] = quote["bundle"]["uri"].replace("/packages/", "/other/")
    elif change == "file": quote["bundle"]["file"] = "engine.msixbundle"
    with pytest.raises(ValueError):
        optional.validate_acquisition_quote(msix.canonical_json(quote), descriptor, selected)


@pytest.mark.parametrize("mutation", ["duplicate", "nested_duplicate", "nonfinite", "oversize"])
def test_engine_quote_strict_json(mutation):
    from tools import msix_packaging as msix
    fixture = msix.CONFIG_ROOT / "fixtures"
    raw = (fixture / "engine-acquisition-quote.v1.json").read_bytes()
    if mutation == "duplicate": raw = raw.replace(b'{', b'{"schema":"wrong",', 1)
    elif mutation == "nested_duplicate": raw = raw.replace(b'"size_bytes":12345', b'"size_bytes":0,"size_bytes":12345')
    elif mutation == "nonfinite": raw = raw.replace(b'"size_bytes":12345', b'"size_bytes":NaN')
    else: raw += b" " * optional.QUOTE_MAXIMUM
    with pytest.raises(ValueError):
        optional.validate_acquisition_quote(raw, (fixture / "engine-acquisition-quote-descriptor.v2.json").read_bytes(),
            msix.read_json(fixture / "engine-acquisition-quote-profile.v2.json"))


def test_quote_reuses_admitted_engine_without_claiming_older_core_is_current(tmp_path):
    selected, _, engine, inputs, descriptor = _engine_quote_inputs(tmp_path)
    quote = optional.acquisition_quote(selected, descriptor, inputs, engine)
    assert quote["related_set_id"] == "related.11"
    assert quote["engine"]["package_version"] == "1.0.42.0"
    assert quote["bundle"]["sha256"] == hashlib.sha256(engine.read_bytes()).hexdigest()
    assert quote["disk"]["temporary_space_budget_bytes"] == 0
    optional.verify_quote_artifact(quote, engine, selected)
    engine.write_bytes(engine.read_bytes() + b"changed after admission")
    with pytest.raises(ValueError, match="final signed Engine bundle pin differs"):
        optional.acquisition_quote(selected, descriptor, inputs, engine)
    with pytest.raises(ValueError, match="final artifact bytes"):
        optional.verify_quote_artifact(quote, engine, selected)


def test_engine_payload_inventory_command_records_exact_external_payload(tmp_path, monkeypatch):
    from tools import msix_packaging as msix
    _, profile_path, engine, _, _ = _engine_quote_inputs(tmp_path)
    output = tmp_path / "emitted.inventory.json"
    monkeypatch.setattr("sys.argv", ["msix_packaging", "engine-payload-inventory", "--profile", str(profile_path),
        "--engine-bundle", str(engine), "--output", str(output)])
    assert msix.main() == 0
    assert msix.read_json(output) == optional.engine_payload_inventory(engine, msix.load_profile(profile_path))


@pytest.mark.parametrize("change", ["missing", "bundle_pin", "inventory_pin", "inventory_bytes", "budget_pin", "budget_binding",
    "missing_budget", "small_installed", "receipt_pin", "unsigned", "publisher", "receipt_engine", "self_attestation"])
def test_quote_inputs_refuse_changed_or_unadmitted_engine_and_budgets(tmp_path, change):
    from tools import msix_packaging as msix
    selected, _, engine, path, descriptor = _engine_quote_inputs(tmp_path)
    inputs = msix.read_json(path)
    if change == "missing": del inputs["disk_budget_declaration"]
    elif change == "bundle_pin": inputs["engine_bundle"]["sha256"] = "0" * 64
    elif change in {"inventory_pin", "budget_pin", "receipt_pin"}:
        key = {"inventory_pin": "engine_payload_inventory", "budget_pin": "disk_budget_declaration", "receipt_pin": "direct_signed_verification"}[change]
        inputs[key]["sha256"] = "0" * 64
    elif change == "self_attestation": inputs["signed"] = True
    else:
        key = ("engine_payload_inventory" if change == "inventory_bytes" else "disk_budget_declaration"
               if change in {"budget_binding", "missing_budget", "small_installed"} else "direct_signed_verification")
        target = tmp_path / inputs[key]["file"]
        document = msix.read_json(target)
        if change == "inventory_bytes": document["files"][0]["sha256"] = "0" * 64
        elif change == "budget_binding": document["bundle_sha256"] = "0" * 64
        elif change == "missing_budget": del document["disk"]["temporary_space_budget_bytes"]
        elif change == "small_installed": document["disk"]["installed_space_budget_bytes"] = 1
        elif change == "unsigned": document["mode"] = "StoreSubmission"
        elif change == "publisher": document["engine_identity"]["Publisher"] = "CN=Another"
        elif change == "receipt_engine": document["artifact_set"]["engine"]["artifact"]["sha256"] = "0" * 64
        target.write_bytes(msix.canonical_json(document))
        inputs[key]["sha256"] = msix.sha256(target)
    path.write_bytes(msix.canonical_json(inputs))
    with pytest.raises(ValueError):
        optional.acquisition_quote(selected, descriptor, path, engine)


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
@pytest.mark.windows_compat
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
@pytest.mark.windows_compat
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
