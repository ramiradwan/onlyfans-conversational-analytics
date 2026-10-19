"""Build-time optional executable package and related-set metadata.

Construction does not establish signature trust, installation or execution.
Windows owns executable installation and replacement.
"""
from __future__ import annotations

import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import zipfile

from app.nlp.contracts import EngineIdentity
from tools.msix_packaging import FOUNDATION, version_tuple

UAP3 = "http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
APPINSTALLER = "http://schemas.microsoft.com/appx/appinstaller/2018"
WORKER_PATH = "worker/TextAnalysisWorker.exe"


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
