"""Build-time optional executable package and related-set metadata.

Construction does not establish signature trust, installation or execution.
Windows owns executable installation and replacement.
"""
from __future__ import annotations

import re
import hashlib
import io
import json
import stat
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

from app.nlp.contracts import EngineIdentity
from tools.msix_packaging import FOUNDATION, version_tuple

UAP3 = "http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
APPINSTALLER = "http://schemas.microsoft.com/appx/appinstaller/2018"
WORKER_PATH = "worker/TextAnalysisWorker.exe"
QUOTE_PATH = "app/engine-acquisition-quote.json"
QUOTE_MAXIMUM = 16384
MAX_SAFE_INTEGER = 9007199254740991


def _closed(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("invalid closed Engine quote object")


def _integer(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_SAFE_INTEGER:
        raise ValueError("Engine quote requires a safe integer")
    return value


def _decode_object(raw: bytes, maximum: int) -> dict:
    if not 0 < len(raw) <= maximum:
        raise ValueError("Engine quote JSON exceeds its bound")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Engine quote JSON member")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("nonfinite Engine quote JSON number")
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    if type(result) is not dict:
        raise ValueError("Engine quote JSON requires an object")
    return result


def validate_acquisition_quote(raw: bytes, descriptor: bytes, profile: dict) -> dict:
    """Validate publisher-expected bytes, without observing a Windows transfer."""
    from jsonschema import Draft202012Validator
    from tools.msix_packaging import CONFIG_ROOT, read_json, validate_profile, desktop_descriptor
    validate_profile(profile)
    quote = _decode_object(raw, QUOTE_MAXIMUM)
    schema = read_json(CONFIG_ROOT / "schemas/engine-acquisition-quote.schema.json")
    if list(Draft202012Validator(schema).iter_errors(quote)):
        raise ValueError("invalid Engine acquisition quote schema")
    desktop = _decode_object(descriptor, 65536)
    profile_digest = desktop.get("profile_sha256")
    if not isinstance(profile_digest, str) or re.fullmatch(r"[0-9a-f]{64}", profile_digest) is None:
        raise ValueError("invalid Engine quote desktop profile binding")
    if (profile["schema"] != "ofca-msix-package-profile/v2"
            or profile["distribution"] != "direct-appinstaller"
            or profile["servicing"]["installed_artifact"]["kind"] != "related_bundle"
            or desktop != desktop_descriptor(profile, profile_digest)):
        raise ValueError("Engine quote requires the exact related desktop descriptor")
    layout = profile["servicing"]["installed_artifact"]
    if (quote["desktop_descriptor_sha256"] != hashlib.sha256(descriptor).hexdigest()
            or quote["related_set_id"] != layout["related_set_id"] or quote["engine"] != layout["engine"]):
        raise ValueError("Engine quote descriptor or relation differs")
    bundle = quote["bundle"]
    if (bundle["file"] != artifact_filename("engine", layout["engine"]["package_version"])
            or bundle["uri"] != artifact_uri(profile["app_installer_uri"], purpose=profile["purpose"],
                kind="engine", version=layout["engine"]["package_version"], digest=bundle["sha256"])):
        raise ValueError("Engine quote artifact coordinate differs")
    _integer(bundle["size_bytes"], positive=True)
    disk = quote["disk"]
    for key, value in disk.items():
        _integer(value, positive=key == "installed_space_budget_bytes")
    _integer(sum(disk.values()))
    return quote


def _pinned_document(base: Path, pin: dict, *, maximum=16 * 1024 * 1024) -> tuple[dict, str]:
    from tools.msix_packaging import sha256
    _closed(pin, ("file", "sha256"))
    if not isinstance(pin["file"], str) or not pin["file"]:
        raise ValueError("Engine quote requires an explicit input file")
    path = base / pin["file"]
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum or sha256(path) != pin["sha256"]:
        raise ValueError("Engine quote input pin differs")
    return _decode_object(path.read_bytes(), maximum), pin["sha256"]


def engine_payload_inventory(bundle: Path, profile: dict) -> dict:
    """Exact installed payload records; deployment footprints are separate."""
    from tools.msix_packaging import FOOTPRINT_FILES, installed_archive_path
    verify_engine_bundle(bundle, profile)
    version = profile["servicing"]["installed_artifact"]["engine"]["package_version"]
    with zipfile.ZipFile(bundle) as outer:
        with zipfile.ZipFile(io.BytesIO(outer.read(engine_application_filename(version)))) as archive:
            records, names = [], set()
            for member in archive.infolist():
                name = installed_archive_path(member.filename)
                if name.casefold() in names or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Engine inventory has an alias or link")
                names.add(name.casefold())
                if name in FOOTPRINT_FILES:
                    continue
                _integer(member.file_size)
                digest = hashlib.sha256()
                with archive.open(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                records.append(dict(path=name, size=member.file_size, sha256=digest.hexdigest()))
    return dict(schema="optional-engine-payload-inventory/v1",
                engine=profile["servicing"]["installed_artifact"]["engine"],
                files=sorted(records, key=lambda record: record["path"]))


def acquisition_quote(profile: dict, descriptor: bytes, inputs_path: Path, engine_bundle: Path) -> dict:
    """Consume pinned outputs of prior independent signed-artifact admission."""
    from tools.msix_packaging import canonical_json, sha256
    if inputs_path.is_symlink() or inputs_path.stat().st_size > 65536:
        raise ValueError("Engine quote inputs exceed their bound or are linked")
    inputs = _decode_object(inputs_path.read_bytes(), 65536)
    _closed(inputs, ("schema", "engine_bundle", "engine_payload_inventory", "disk_budget_declaration",
                     "direct_signed_verification"))
    if inputs["schema"] != "optional-engine-acquisition-quote-inputs/v1":
        raise ValueError("invalid Engine quote inputs schema")
    if (profile.get("schema") != "ofca-msix-package-profile/v2" or profile["distribution"] != "direct-appinstaller"
            or profile["servicing"]["installed_artifact"]["kind"] != "related_bundle"):
        raise ValueError("Engine quote requires a direct related release")
    layout = profile["servicing"]["installed_artifact"]
    bundle = inputs["engine_bundle"]
    _closed(bundle, ("file", "sha256", "size_bytes"))
    _integer(bundle["size_bytes"], positive=True)
    if (engine_bundle.is_symlink() or not engine_bundle.is_file()
            or bundle["file"] != engine_bundle.name
            or bundle["file"] != artifact_filename("engine", layout["engine"]["package_version"])
            or bundle["sha256"] != sha256(engine_bundle) or bundle["size_bytes"] != engine_bundle.stat().st_size):
        raise ValueError("final signed Engine bundle pin differs")
    inventory, inventory_sha = _pinned_document(inputs_path.parent, inputs["engine_payload_inventory"])
    _closed(inventory, ("schema", "engine", "files"))
    if type(inventory["files"]) is not list or not 1 <= len(inventory["files"]) <= 50000:
        raise ValueError("invalid Engine payload inventory")
    for record in inventory["files"]:
        _closed(record, ("path", "sha256", "size"))
        _integer(record["size"])
    installed_bytes = _integer(sum(record["size"] for record in inventory["files"]))
    if inventory != engine_payload_inventory(engine_bundle, profile):
        raise ValueError("sealed Engine payload inventory differs")
    declaration, _ = _pinned_document(inputs_path.parent, inputs["disk_budget_declaration"], maximum=65536)
    _closed(declaration, ("schema", "engine", "bundle_sha256", "engine_payload_inventory_sha256", "disk"))
    if (declaration["schema"] != "optional-engine-disk-budget/v1" or declaration["engine"] != layout["engine"]
            or declaration["bundle_sha256"] != bundle["sha256"]
            or declaration["engine_payload_inventory_sha256"] != inventory_sha):
        raise ValueError("Engine disk declaration binding differs")
    # A pinned verifier receipt is an input from the signing admission job,
    # not a replacement for that job or proof of an arbitrary file's provenance.
    verification, _ = _pinned_document(inputs_path.parent, inputs["direct_signed_verification"])
    _closed(verification, ("schema", "purpose", "distribution", "mode", "identity", "file_count",
        "package_sha256", "profile_sha256", "inventory_sha256", "signature", "artifact_set", "engine_identity"))
    prior = verification["artifact_set"]
    _closed(prior, ("kind", "core", "related_set_id", "engine"))
    _closed(prior["core"], ("file", "sha256", "size"))
    _closed(verification["identity"], ("Name", "Publisher", "Version", "Architecture", "FullName", "FamilyName"))
    _closed(prior["engine"], ("identity", "package_version", "artifact"))
    _closed(prior["engine"]["artifact"], ("file", "sha256", "size"))
    _integer(prior["engine"]["artifact"]["size"], positive=True)
    _closed(verification["engine_identity"], ("Name", "Publisher", "Version", "Architecture", "FullName", "FamilyName"))
    artifact = dict(file=bundle["file"], sha256=bundle["sha256"], size=bundle["size_bytes"])
    expected_engine = dict(**layout["engine"], artifact=artifact)
    identity = layout["engine"]["identity"]
    observed_engine = dict(Name=identity["name"], Publisher=identity["publisher"], Architecture=identity["architecture"],
        Version=layout["engine"]["package_version"], FamilyName=identity["family"],
        FullName=f'{identity["name"]}_{layout["engine"]["package_version"]}_x64__{identity["family"].rsplit("_", 1)[1]}')
    if (verification.get("schema") != "ofca-msix-release-verification/v2"
            or verification.get("mode") != "DirectSigned" or verification.get("signature") != "trusted-timestamped"
            or verification.get("purpose") != profile["purpose"] or verification.get("distribution") != "direct-appinstaller"
            or prior["kind"] != "related_bundle" or prior["engine"] != expected_engine
            or verification["engine_identity"] != observed_engine):
        raise ValueError("independent DirectSigned Engine verification differs")
    # The receipt may describe an older Core release and related set. Only its
    # admitted Engine is reused; the target relation is checked independently.
    version_tuple(verification["identity"]["Version"])
    _integer(verification["file_count"], positive=True)
    _integer(prior["core"]["size"], positive=True)
    if (prior["core"]["file"] != artifact_filename("related_bundle", verification["identity"]["Version"])
            or prior["core"]["sha256"] != verification["package_sha256"]
            or not isinstance(prior["related_set_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", prior["related_set_id"])
            or any(not isinstance(verification[key], str) or not re.fullmatch(r"[a-f0-9]{64}", verification[key])
                   for key in ("package_sha256", "profile_sha256", "inventory_sha256"))):
        raise ValueError("invalid prior DirectSigned verification record")
    quote = dict(schema="optional-engine-acquisition-quote/v1", desktop_descriptor_sha256=hashlib.sha256(descriptor).hexdigest(),
                 related_set_id=layout["related_set_id"], engine=layout["engine"],
                 bundle=dict(**bundle, uri=artifact_uri(profile["app_installer_uri"], purpose=profile["purpose"],
                    kind="engine", version=layout["engine"]["package_version"], digest=bundle["sha256"])),
                 disk=declaration["disk"])
    validate_acquisition_quote(canonical_json(quote), descriptor, profile)
    if installed_bytes > quote["disk"]["installed_space_budget_bytes"]:
        raise ValueError("Engine installed budget is below its sealed payload")
    return quote


def verify_quote_artifact(quote: dict, engine_bundle: Path, profile: dict) -> None:
    from tools.msix_packaging import sha256
    if (quote["bundle"]["sha256"] != sha256(engine_bundle)
            or quote["bundle"]["size_bytes"] != engine_bundle.stat().st_size):
        raise ValueError("quoted Engine differs from the final artifact bytes")
    if sum(item["size"] for item in engine_payload_inventory(engine_bundle, profile)["files"]) > quote["disk"]["installed_space_budget_bytes"]:
        raise ValueError("quoted Engine installed budget is below its final payload")


def artifact_filename(kind: str, version: str) -> str:
    """One closed filename rule for versioned release artifacts."""
    version_tuple(version)
    if version_tuple(version)[0] == 0:
        raise ValueError("artifact version requires a nonzero major")
    names = {"main_package": ("ConversationAnalytics", ".msix"),
             "related_bundle": ("ConversationAnalytics", ".msixbundle"),
             "engine": ("TextAnalysisEngine", ".msixbundle")}
    if kind not in names:
        raise ValueError("unknown release artifact kind")
    prefix, suffix = names[kind]
    return f"{prefix}_{version}_x64{suffix}"


def engine_application_filename(version: str) -> str:
    return artifact_filename("engine", version).removesuffix("bundle")


def artifact_uri(descriptor_uri: str, *, purpose: str, kind: str,
                 version: str, digest: str) -> str:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("artifact requires its exact SHA256")
    _https_uri(descriptor_uri, ".appinstaller")
    suffix = ("windows/x64/stable/ConversationAnalytics.appinstaller"
              if purpose == "production" else "probe.appinstaller")
    if purpose not in {"production", "qualification"} or not descriptor_uri.endswith(suffix):
        raise ValueError("descriptor has no approved release base")
    base = descriptor_uri[:-len(suffix)]
    return base + "packages/" + digest + "/" + artifact_filename(kind, version)


def release_artifact_set(profile: dict, core: Path, engine: Path | None = None) -> dict:
    """Freeze actual bytes; signatures and release admission remain separate."""
    from tools.msix_packaging import file_record, package_version, validate_profile
    validate_profile(profile)
    if profile["schema"] != "ofca-msix-package-profile/v2":
        raise ValueError("artifact sets require a v2 profile")
    layout = profile["servicing"]["installed_artifact"]
    def record(path, kind, version):
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise ValueError("artifact requires a regular nonempty file")
        item = file_record(path, artifact_filename(kind, version))
        return dict(file=item["path"], sha256=item["sha256"], size=item["size"])
    result = dict(kind=layout["kind"], core=record(core, layout["kind"], package_version(profile["release_sequence"])))
    if layout["kind"] == "related_bundle":
        if engine is None:
            raise ValueError("related release requires its exact external Engine bundle")
        verify_release_bundle(core, profile)
        verify_engine_bundle(engine, profile)
        result.update(related_set_id=layout["related_set_id"],
                      engine=dict(identity=layout["engine"]["identity"],
                                  package_version=layout["engine"]["package_version"],
                                  artifact=record(engine, "engine", layout["engine"]["package_version"])))
    elif engine is not None:
        raise ValueError("MainPackage release cannot carry an Engine artifact")
    validate_artifact_set(result, profile)
    return result


def validate_artifact_set(artifacts: dict, profile: dict) -> None:
    """Closed external byte inventory; it never establishes signature trust."""
    from tools.msix_packaging import package_version
    def closed(item, names):
        if type(item) is not dict or set(item) != set(names):
            raise ValueError("invalid closed artifact set")
    layout = profile["servicing"]["installed_artifact"]
    closed(artifacts, ("kind", "core") if layout["kind"] == "main_package"
           else ("kind", "core", "related_set_id", "engine"))
    if artifacts["kind"] != layout["kind"]:
        raise ValueError("artifact layout differs")
    def record(item, kind, version):
        closed(item, ("file", "sha256", "size"))
        if (item["file"] != artifact_filename(kind, version) or not isinstance(item["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                or type(item["size"]) is not int or not 0 < item["size"] <= 4294967295):
            raise ValueError("invalid artifact bytes")
    record(artifacts["core"], layout["kind"], package_version(profile["release_sequence"]))
    if layout["kind"] == "related_bundle":
        closed(artifacts["engine"], ("identity", "package_version", "artifact"))
        if (artifacts["related_set_id"] != layout["related_set_id"]
                or artifacts["engine"]["identity"] != layout["engine"]["identity"]
                or artifacts["engine"]["package_version"] != layout["engine"]["package_version"]):
            raise ValueError("artifact pairing differs")
        record(artifacts["engine"]["artifact"], "engine", layout["engine"]["package_version"])


def release_appinstaller(profile: dict, artifacts: dict) -> bytes:
    """Render v2 coordinates from exact artifact records, never caller basenames."""
    from tools.msix_packaging import package_version, render_appinstaller, validate_profile
    validate_profile(profile)
    validate_artifact_set(artifacts, profile)
    layout = profile["servicing"]["installed_artifact"]
    if artifacts["kind"] != layout["kind"]:
        raise ValueError("artifact layout differs from the descriptor")
    version = package_version(profile["release_sequence"])
    core_uri = artifact_uri(profile["app_installer_uri"], purpose=profile["purpose"],
                            kind=layout["kind"], version=version, digest=artifacts["core"]["sha256"])
    if artifacts["core"]["file"] != artifact_filename(layout["kind"], version):
        raise ValueError("unexpected core filename")
    if layout["kind"] == "main_package":
        return render_appinstaller(profile, core_uri)
    if (artifacts["engine"]["identity"] != layout["engine"]["identity"]
            or artifacts["engine"]["package_version"] != layout["engine"]["package_version"]
            or artifacts["related_set_id"] != layout["related_set_id"]):
        raise ValueError("artifact pairing differs from the descriptor")
    engine = _release_relation(profile)
    uri = artifact_uri(profile["app_installer_uri"], purpose=profile["purpose"], kind="engine",
                       version=engine.package_version, digest=artifacts["engine"]["artifact"]["sha256"])
    if artifacts["engine"]["artifact"]["file"] != artifact_filename("engine", engine.package_version):
        raise ValueError("unexpected Engine filename")
    return appinstaller(engine, descriptor_uri=profile["app_installer_uri"], core_uri=core_uri, optional_uri=uri)


def _release_relation(profile: dict):
    from tools.msix_packaging import package_version
    engine = profile["servicing"]["installed_artifact"]["engine"]
    return SimpleNamespace(package_name=engine["identity"]["name"],
        package_family=engine["identity"]["family"], publisher=engine["identity"]["publisher"],
        package_version=engine["package_version"], core_package_name=profile["identity"]["name"],
        core_package_version=package_version(profile["release_sequence"]))


def verify_release_bundle(path: Path, profile: dict) -> None:
    relation = _release_relation(profile)
    with zipfile.ZipFile(path) as archive:
        root = _bounded_bundle_xml(archive, "AppxMetadata/AppxBundleManifest.xml")
    verify_related_bundle(path, relation)
    namespace = "http://schemas.microsoft.com/appx/2016/bundle"
    optional = root.find(f"{{{namespace}}}OptionalBundle")
    if optional.get("FileName") != artifact_filename("engine", relation.package_version):
        raise ValueError("external Engine filename is not canonical")
    main = root.find(f"{{{namespace}}}Packages/{{{namespace}}}Package")
    external = optional.find(f"{{{namespace}}}Package")
    if (main.get("FileName") != artifact_filename("main_package", relation.core_package_version)
            or external.get("FileName") != engine_application_filename(relation.package_version)):
        raise ValueError("contained application filenames are not canonical")


def _bounded_bundle_xml(archive, name):
    entries = [x for x in archive.infolist() if x.filename == name]
    if len(entries) != 1 or not 0 < entries[0].file_size <= 65536:
        raise ValueError("missing or oversized bundle evidence")
    body = archive.read(entries[0])
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("bundle evidence cannot declare entities")
    return ET.fromstring(body)


def verify_engine_bundle(path: Path, profile: dict) -> None:
    """Exact external bundle relation, without installing or admitting a worker."""
    import io
    relation = _release_relation(profile)
    with zipfile.ZipFile(path) as archive:
        root = _bounded_bundle_xml(archive, "AppxMetadata/AppxBundleManifest.xml")
        namespaces = ("http://schemas.microsoft.com/appx/2013/bundle",
                      "http://schemas.microsoft.com/appx/2016/bundle")
        namespace = next((ns for ns in namespaces if root.tag == f"{{{ns}}}Bundle"), None)
        if namespace is None or root.get("SchemaVersion") != "5.0":
            raise ValueError("unsupported Engine bundle")
        if len(root) != 2 or {child.tag for child in root} != {
                f"{{{namespace}}}Identity", f"{{{namespace}}}Packages"}:
            raise ValueError("Engine bundle requires only identity and packages")
        identities = root.findall(f"{{{namespace}}}Identity")
        packages = root.findall(f"{{{namespace}}}Packages/{{{namespace}}}Package")
        if len(identities) != 1 or len(packages) != 1 or root.findall(f"{{{namespace}}}OptionalBundle"):
            raise ValueError("Engine bundle requires one application without optional bundles")
        if any(identities[0].get(k) != v for k,v in {
                "Name":relation.package_name,"Publisher":relation.publisher,"Version":relation.package_version}.items()):
            raise ValueError("Engine bundle identity differs")
        package = packages[0]
        if any(package.get(k) != v for k,v in {
                "Type":"application","Architecture":"x64","Version":relation.package_version}.items()):
            raise ValueError("Engine application differs")
        name = _artifact_name(package.get("FileName"), ".msix")
        if name != engine_application_filename(relation.package_version):
            raise ValueError("Engine contained application filename is not canonical")
        entries = [x for x in archive.infolist() if x.filename == name]
        if len(entries) != 1 or not 0 < entries[0].file_size <= 512 * 1024**2:
            raise ValueError("Engine payload bound")
        with zipfile.ZipFile(io.BytesIO(archive.read(entries[0]))) as payload:
            manifest = _bounded_bundle_xml(payload, "AppxManifest.xml")
            identity = manifest.findall(f"{{{FOUNDATION}}}Identity")
            dependencies = manifest.findall(f"{{{FOUNDATION}}}Dependencies/{{{UAP3}}}MainPackageDependency")
            if len(identity) != 1 or len(dependencies) != 1 or manifest.find(f"{{{FOUNDATION}}}Applications") is not None:
                raise ValueError("Engine application relation differs")
            if any(identity[0].get(k) != v for k,v in {
                    "Name":relation.package_name,"Publisher":relation.publisher,
                    "Version":relation.package_version,"ProcessorArchitecture":"x64"}.items()):
                raise ValueError("Engine application identity differs")
            if dependencies[0].attrib != {"Name":relation.core_package_name} or payload.namelist().count(WORKER_PATH) != 1:
                raise ValueError("Engine dependency or worker entry differs")


def validate_identity(engine: EngineIdentity) -> None:
    for name in (engine.package_name, engine.core_package_name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{2,49}", name):
            raise ValueError("invalid Windows package name")
    if engine.package_name.casefold() == engine.core_package_name.casefold():
        raise ValueError("optional and core identities must differ")
    if not engine.publisher.startswith("CN=") or any(ord(c) < 32 for c in engine.publisher):
        raise ValueError("invalid package publisher")
    if not re.fullmatch(re.escape(engine.package_name) + r"_[a-z0-9]{13}", engine.package_family):
        raise ValueError("invalid optional package family")
    for value in (engine.package_version, engine.core_package_version):
        if version_tuple(value)[0] == 0:
            raise ValueError("package version must have a nonzero major")


def optional_manifest(engine: EngineIdentity, *, minimum_os: str, tested_os: str) -> bytes:
    validate_identity(engine)
    if version_tuple(minimum_os) < (10, 0, 26100, 0) or version_tuple(tested_os) < version_tuple(minimum_os):
        raise ValueError("unsupported optional package OS range")
    ET.register_namespace("", FOUNDATION)
    ET.register_namespace("uap3", UAP3)
    root = ET.Element(f"{{{FOUNDATION}}}Package", IgnorableNamespaces="uap3")
    def child(parent, name, **attrs):
        return ET.SubElement(parent, f"{{{FOUNDATION}}}{name}", attrs)
    child(root, "Identity", Name=engine.package_name, Publisher=engine.publisher,
          Version=engine.package_version, ProcessorArchitecture="x64")
    props = child(root, "Properties")
    for name, value in (("DisplayName", "Optional text analysis engine"),
                        ("PublisherDisplayName", "Package publisher"),
                        ("Description", "Optional local computation executable."),
                        ("Logo", "Assets/StoreLogo.png")):
        child(props, name).text = value
    deps = child(root, "Dependencies")
    child(deps, "TargetDeviceFamily", Name="Windows.Desktop", MinVersion=minimum_os,
          MaxVersionTested=tested_os)
    ET.SubElement(deps, f"{{{UAP3}}}MainPackageDependency", Name=engine.core_package_name)
    child(child(root, "Resources"), "Resource", Language="en-us")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _artifact_name(value: str, suffix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value) or not value.endswith(suffix):
        raise ValueError("invalid immutable artifact filename")
    return value


def bundle_mapping(core_package: str, optional_bundle: str) -> bytes:
    """MakeAppx records the external optional bundle in the main bundle."""
    main = _artifact_name(core_package, ".msix")
    optional = _artifact_name(optional_bundle, ".msixbundle")
    return f'[Files]\n"{main}" "{main}"\n[ExternalPackages]\n"{optional}" "{optional}"\n'.encode("utf-8")


def _https_uri(value: str, suffix: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or not parsed.path.endswith(suffix) or "\\" in value
            or any(ord(c) <= 32 for c in value)):
        raise ValueError("fixed credential-free HTTPS artifact URI required")
    if any(part in (".", "..") for part in parsed.path.split("/")) or "%" in parsed.path:
        raise ValueError("noncanonical artifact path")
    return value


def appinstaller(engine: EngineIdentity, *, descriptor_uri: str, core_uri: str, optional_uri: str) -> bytes:
    validate_identity(engine)
    for value, suffix in ((descriptor_uri, ".appinstaller"), (core_uri, ".msixbundle"),
                          (optional_uri, ".msixbundle")):
        _https_uri(value, suffix)
    if len({urlsplit(v).netloc for v in (descriptor_uri, core_uri, optional_uri)}) != 1:
        raise ValueError("related-set artifacts must share the descriptor origin")
    ET.register_namespace("", APPINSTALLER)
    root = ET.Element(f"{{{APPINSTALLER}}}AppInstaller", Version=engine.core_package_version, Uri=descriptor_uri)
    ET.SubElement(root, f"{{{APPINSTALLER}}}MainBundle", Name=engine.core_package_name,
                  Publisher=engine.publisher, Version=engine.core_package_version, Uri=core_uri)
    related = ET.SubElement(root, f"{{{APPINSTALLER}}}RelatedPackages")
    ET.SubElement(related, f"{{{APPINSTALLER}}}Bundle", Name=engine.package_name,
                  Publisher=engine.publisher, Version=engine.package_version, Uri=optional_uri)
    settings = ET.SubElement(root, f"{{{APPINSTALLER}}}UpdateSettings")
    ET.SubElement(settings, f"{{{APPINSTALLER}}}OnLaunch", HoursBetweenUpdateChecks="6",
                  ShowPrompt="false", UpdateBlocksActivation="false")
    ET.SubElement(settings, f"{{{APPINSTALLER}}}AutomaticBackgroundTask")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def verify_related_bundle(path: Path, engine: EngineIdentity) -> None:
    """Verify exact bundle links; signature verification remains separate."""
    validate_identity(engine)
    with zipfile.ZipFile(path) as bundle:
        members = bundle.namelist()
        name = "AppxMetadata/AppxBundleManifest.xml"
        if members.count(name) != 1 or bundle.getinfo(name).file_size > 65536:
            raise ValueError("missing or oversized bundle manifest")
        root = ET.fromstring(bundle.read(name))
    namespace = root.tag.removesuffix("Bundle").removeprefix("{").removesuffix("}")
    if namespace != "http://schemas.microsoft.com/appx/2016/bundle" or root.tag != f"{{{namespace}}}Bundle":
        raise ValueError("unsupported bundle manifest namespace")
    if root.get("SchemaVersion") != "5.0":
        raise ValueError("unsupported bundle schema version")
    def children(tag):
        return root.findall(f"{{{namespace}}}{tag}")
    identities, optionals = children("Identity"), children("OptionalBundle")
    if len(identities) != 1 or len(optionals) != 1:
        raise ValueError("one main identity and one optional bundle required")
    for node, name, version in ((identities[0], engine.core_package_name, engine.core_package_version),
                                (optionals[0], engine.package_name, engine.package_version)):
        if any(node.get(k) != v for k, v in {"Name": name, "Publisher": engine.publisher, "Version": version}.items()):
            raise ValueError("bundle identity or related-set version mismatch")
    _artifact_name(optionals[0].get("FileName"), ".msixbundle")
    packages = root.findall(f"{{{namespace}}}Packages/{{{namespace}}}Package")
    if len(packages) != 1 or any(packages[0].get(k) != v for k, v in {
            "Type": "application", "Architecture": "x64", "Version": engine.core_package_version}.items()):
        raise ValueError("one exact x64 main application package required")
    _artifact_name(packages[0].get("FileName"), ".msix")
    if members.count(packages[0].get("FileName")) != 1:
        raise ValueError("main bundle must contain its exact application package")
    external = optionals[0].findall(f"{{{namespace}}}Package")
    if len(external) != 1 or any(external[0].get(k) != v for k, v in {
            "Type": "application", "Architecture": "x64", "Version": engine.package_version}.items()):
        raise ValueError("one exact x64 external application package required")
    _artifact_name(external[0].get("FileName"), ".msix")
    if optionals[0].get("FileName") in members or external[0].get("FileName") in members:
        raise ValueError("optional executable must remain outside the main bundle")


def verified_worker_path(root: Path) -> Path:
    """Refuse symlink/reparse traversal before sealing an executable payload."""
    def refuse_link(path):
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise ValueError("worker must be inside the optional package") from error
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("worker path cannot traverse a link")
    root = root.absolute()
    for ancestor in (root, *root.parents):
        refuse_link(ancestor)
    root = root.resolve(strict=True)
    candidate = root
    for part in PurePosixPath(WORKER_PATH).parts:
        candidate = candidate / part
        refuse_link(candidate)
    if not candidate.is_file() or not candidate.resolve(strict=True).is_relative_to(root):
        raise ValueError("worker must be inside the optional package")
    return candidate
