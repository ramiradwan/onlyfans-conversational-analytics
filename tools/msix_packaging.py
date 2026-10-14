"""Render, inventory and verify the MSIX adapter around a qualified runtime.

This is a build/release owner. It does not download software, sign packages,
install applications or replace the runtime's persistence authorities.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.packaging_policy import installed_runtime_files, verify_runtime_files  # noqa: E402

CONFIG_ROOT = ROOT / "packaging/msix"
FOUNDATION = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
NAMESPACES = {
    "": FOUNDATION,
    "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
    "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
    "uap17": "http://schemas.microsoft.com/appx/manifest/uap/windows10/17",
    "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
    "virtualization": "http://schemas.microsoft.com/appx/manifest/virtualization/windows10",
    "win32dependencies": "http://schemas.microsoft.com/appx/manifest/externaldependencies",
}
ASSETS = {"StoreLogo.png": 50, "Square44x44Logo.png": 44, "Square150x150Logo.png": 150}
FOOTPRINT_FILES = {"AppxBlockMap.xml", "[Content_Types].xml", "AppxSignature.p7x", "AppxMetadata/CodeIntegrity.cat"}
QUALIFICATION_NAME_PREFIX = "OFCA.Qualification."
QUALIFICATION_PUBLISHER_PREFIX = "CN=OFCA MSIX Qualification"


def canonical_json(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_json(path: Path, *, maximum: int = 65536) -> dict:
    if path.stat().st_size > maximum:
        raise ValueError("JSON input exceeds its bound")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result
    result = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    if not isinstance(result, dict):
        raise ValueError("JSON input must be an object")
    return result


def version_tuple(value: str) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ValueError("Windows versions require four canonical numeric components")
    numbers = tuple(int(part) for part in value.split("."))
    if any(part > 65535 for part in numbers):
        raise ValueError("Windows version component exceeds 65535")
    return numbers


def package_version(sequence: int) -> str:
    """One deployment counter; it neither reads nor alters licensed major."""
    if type(sequence) is not int or not 1 <= sequence <= 4294967295:
        raise ValueError("invalid deployment sequence")
    return f"1.{sequence // 65536}.{sequence % 65536}.0"


def validate_profile(profile: dict, *, production: bool = False) -> dict:
    schema = read_json(CONFIG_ROOT / "package-profile.schema.json")
    errors = sorted(Draft202012Validator(schema).iter_errors(profile), key=lambda item: str(item.path))
    if errors:
        raise ValueError("invalid package profile: " + errors[0].message)
    for value in (*profile["identity"].values(), profile["application_version"]):
        if isinstance(value, str) and (value != value.strip() or any(ord(character) < 32 for character in value)):
            raise ValueError("profile strings contain whitespace or control characters")
    minimum = version_tuple(profile["minimum_os_version"])
    tested = version_tuple(profile["maximum_os_version_tested"])
    if minimum < (10, 0, 26100, 0) or tested < minimum:
        raise ValueError("selected MSIX features require build 26100 or later and a valid tested maximum")
    if profile["purpose"] == "production" and minimum < (10, 0, 26200, 0):
        raise ValueError("production MSIX requires the approved Windows 11 25H2 customer baseline")
    if version_tuple(profile["webview2_minimum_version"])[0] == 0:
        raise ValueError("WebView2 dependency version must have a nonzero first component")
    qualification = profile["purpose"] == "qualification"
    synthetic_name = profile["identity"]["name"].casefold().startswith(QUALIFICATION_NAME_PREFIX.casefold())
    synthetic_publisher = profile["identity"]["publisher"].casefold().startswith(QUALIFICATION_PUBLISHER_PREFIX.casefold())
    synthetic_identity = synthetic_name and synthetic_publisher
    if qualification and not synthetic_identity:
        raise ValueError("qualification requires its isolated package name and publisher")
    if not qualification and (synthetic_name or synthetic_publisher):
        raise ValueError("qualification identities cannot become production profiles")
    if production and qualification:
        raise ValueError("production publication refuses qualification profiles")
    uri = profile["app_installer_uri"]
    if profile["distribution"] == "microsoft-store":
        if uri is not None:
            raise ValueError("Store profiles cannot register a direct feed")
    else:
        if not isinstance(uri, str):
            raise ValueError("direct profiles require an App Installer URI")
        parsed = urlsplit(uri)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or not parsed.path.endswith(".appinstaller")
                or "\\" in uri or any(ord(character) <= 32 for character in uri)):
            raise ValueError("App Installer URI must be a fixed credential-free HTTPS descriptor")
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                   for label in parsed.hostname.split(".")):
            raise ValueError("App Installer URI has an invalid host")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None or parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("App Installer URI cannot target a local or numeric address")
        if not qualification and (parsed.hostname.endswith((".example", ".test", ".invalid", ".example.com"))
                                   or parsed.hostname in {"example.com", "example.org", "example.net"}):
            raise ValueError("production profiles require actual hosting coordinates")
    return profile


def load_profile(path: Path, *, production: bool = False) -> dict:
    return validate_profile(read_json(path), production=production)


def render_manifest(profile: dict) -> bytes:
    validate_profile(profile)
    identity = profile["identity"]
    values = {
        "@NAME@": identity["name"], "@PUBLISHER@": identity["publisher"],
        "@PUBLISHER_DISPLAY_NAME@": identity["publisher_display_name"],
        "@APPLICATION_ID@": identity["application_id"],
        "@PACKAGE_VERSION@": package_version(profile["release_sequence"]),
        "@MINIMUM_OS@": profile["minimum_os_version"], "@TESTED_OS@": profile["maximum_os_version_tested"],
        "@WEBVIEW2_MINIMUM@": profile["webview2_minimum_version"],
    }
    root = ET.fromstring((CONFIG_ROOT / "AppxManifest.xml.in").read_bytes())
    for prefix, namespace in NAMESPACES.items():
        ET.register_namespace(prefix, namespace)
    for element in root.iter():
        for key, value in element.attrib.items():
            if value in values:
                element.set(key, values[value])
        if element.text in values:
            element.text = values[element.text]
    rendered = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    if re.search(rb"@[A-Z_]+@", rendered):
        raise ValueError("unresolved manifest input")
    return rendered


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def file_record(path: Path, relative: str) -> dict:
    return {"path": relative, "sha256": sha256(path), "size": path.stat().st_size}


def desktop_descriptor(profile: dict, profile_sha256: str) -> dict:
    descriptor = {
        "schema": "ofca-desktop-release/v1", "purpose": profile["purpose"],
        "identity": profile["identity"], "application_version": profile["application_version"],
        "package_version": package_version(profile["release_sequence"]), "profile_sha256": profile_sha256,
        "update_owner": profile["distribution"], "app_installer_uri": profile["app_installer_uri"],
        "storage_policy": "retained-product-root/v1", "executable_family": "brain-bridge/v1",
        "webview2_minimum_version": profile["webview2_minimum_version"],
    }
    Draft202012Validator(read_json(CONFIG_ROOT / "desktop-release.schema.json")).validate(descriptor)
    return descriptor


def validate_installed_path_budget(paths: list[str] | tuple[str, ...], profile: dict) -> int:
    """Keep legacy consumers usable without requiring a global long-path setting.

    The package publisher ID has 13 characters. Include the versioned package
    root, app directory and terminating NUL in the UTF-16 path budget.
    """
    full_name = f"{profile['identity']['name']}_{package_version(4294967295)}_x64__{'0' * 13}"
    prefix = f"C:\\Program Files\\WindowsApps\\{full_name}\\app\\"
    maximum = max((len((prefix + path.replace('/', '\\')).encode('utf-16-le')) // 2 + 1 for path in paths), default=0)
    if maximum > 260:
        raise ValueError("installed runtime exceeds the legacy Windows path budget; shorten the payload paths")
    return maximum


def _validate_assets() -> None:
    if {path.name for path in (CONFIG_ROOT / "Assets").iterdir()} != set(ASSETS):
        raise ValueError("MSIX assets differ from the reviewed asset closure")
    for name, dimension in ASSETS.items():
        data = (CONFIG_ROOT / "Assets" / name).read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (dimension, dimension):
            raise ValueError("MSIX logo is missing or has the wrong dimensions")


def prepare_payload(stage: Path, working: Path, profile_path: Path) -> dict:
    profile = load_profile(profile_path)
    # Qualification is first: adapters cannot turn a deficient stage into a pass.
    paths = installed_runtime_files(stage, "msix")
    validate_installed_path_budget(paths, profile)
    release = read_json(stage / "release-manifest.json")
    if release.get("version") != profile["application_version"] or release.get("architecture") != "x64":
        raise ValueError("desktop profile disagrees with the qualified runtime version or architecture")
    _validate_assets()
    if working.exists():
        raise ValueError("MSIX preparation requires a fresh directory")
    working_resolved = working.resolve()
    stage_resolved = stage.resolve()
    if working_resolved.is_relative_to(stage_resolved) or stage_resolved.is_relative_to(working_resolved):
        raise ValueError("MSIX output and staged input cannot contain one another")
    if working_resolved.is_relative_to(ROOT):
        raise ValueError("MSIX output must be outside the repository")
    if profile["purpose"] == "production":
        status = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True)
        if status.strip():
            raise ValueError("production MSIX requires committed clean producer source")
    records = [file_record(stage / path, path) for path in paths]
    payload = working / "payload"
    app = payload / "app"
    app.mkdir(parents=True)
    for path in paths:
        destination = app / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stage / path, destination)
    (payload / "AppxManifest.xml").write_bytes(render_manifest(profile))
    shutil.copytree(CONFIG_ROOT / "Assets", payload / "Assets")
    (app / "desktop-release.json").write_bytes(canonical_json(desktop_descriptor(profile, sha256(profile_path))))
    agent = read_json(stage / "Agent/build-meta.json")
    receipt = {
        "schema": "ofca-msix-preparation/v1", "profile_sha256": sha256(profile_path),
        "source_runtime_files": records, "runtime_release": release,
        "linked_agent": {key: agent[key] for key in ("schema", "extension_version", "legal_bindings", "signing_parameters") if key in agent},
    }
    (working / "preparation.json").write_bytes(canonical_json(receipt))
    return {"payload": str(payload), "package_version": package_version(profile["release_sequence"]),
            "package_name": profile["identity"]["name"]}


def seal_payload(working: Path, profile_path: Path, makeappx: Path) -> dict:
    profile = load_profile(profile_path)
    receipt = read_json(working / "preparation.json", maximum=16 * 1024 * 1024)
    if receipt["profile_sha256"] != sha256(profile_path):
        raise ValueError("profile changed after preparation")
    payload = working / "payload"
    app = payload / "app"
    findings = verify_runtime_files(app, target="msix")
    if findings:
        raise ValueError("installed MSIX runtime rejected: " + ", ".join(item.code for item in findings))
    for record in receipt["source_runtime_files"]:
        if file_record(app / record["path"], record["path"]) != record:
            raise ValueError("runtime changed after stage qualification")
    expected = {"app/" + record["path"] for record in receipt["source_runtime_files"]}
    expected.update({"app/desktop-release.json", "app/sha256sums.txt", "AppxManifest.xml"})
    expected.update("Assets/" + name for name in ASSETS)
    actual = {path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_file()}
    if expected != actual:
        raise ValueError("installed package has extra or missing files")
    if (app / "desktop-release.json").read_bytes() != canonical_json(desktop_descriptor(profile, sha256(profile_path))):
        raise ValueError("desktop descriptor changed after rendering")
    if (payload / "AppxManifest.xml").read_bytes() != render_manifest(profile):
        raise ValueError("manifest changed after rendering")
    digest_entries = {record["path"]: record["sha256"] for record in (
        file_record(path, path.relative_to(app).as_posix()) for path in app.rglob("*")
        if path.is_file() and path.name != "sha256sums.txt"
    )}
    digests = {}
    for line in (app / "sha256sums.txt").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) \*(.+)", line)
        if match is None or match[2] in digests:
            raise ValueError("invalid inner digest file")
        digests[match[2]] = match[1]
    if digests != digest_entries:
        raise ValueError("inner digests do not match installed runtime closure")
    inventory = {
        "schema": "ofca-msix-inventory/v1", "profile_sha256": sha256(profile_path),
        "identity": profile["identity"], "purpose": profile["purpose"], "distribution": profile["distribution"],
        "application_version": profile["application_version"], "package_version": package_version(profile["release_sequence"]),
        "runtime_release": receipt["runtime_release"], "linked_agent": receipt["linked_agent"],
        "runtime_policy_sha256": sha256(ROOT / "packaging/runtime-files.json"),
        "manifest_template_sha256": sha256(CONFIG_ROOT / "AppxManifest.xml.in"),
        "asset_source_sha256": sha256(ROOT / "packaging/windows/conversation-analytics.ico"),
        "makeappx_sha256": sha256(makeappx),
        "files": [file_record(payload / path, path) for path in sorted(actual)],
    }
    (working / "inventory.json").write_bytes(canonical_json(inventory))
    return inventory


def _safe_archive_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (bool(value) and not path.is_absolute() and "\\" not in value and ":" not in value
            and not any(ord(character) < 32 for character in value)
            and all(part not in {".", "..", ""} for part in value.split("/")) and str(path) == value)


def installed_archive_path(value: str) -> str:
    """MSIX ZIP parts escape names; deployed/unpacked paths are decoded once.

    MakeAppx escapes tzdata's GMT+ names as GMT%2B. Reject escapes introducing
    separators, traversal, control characters or ambiguous decoded aliases.
    Literal plus is never interpreted as a space.
    """
    if (not _safe_archive_path(value) or re.search(r"%(?![0-9A-Fa-f]{2})|%(?:2[Ff]|5[Cc])", value)):
        raise ValueError("unsafe MSIX part name")
    decoded = unquote(value, encoding="utf-8", errors="strict")
    if not _safe_archive_path(decoded) or (decoded in FOOTPRINT_FILES and decoded != value):
        raise ValueError("unsafe MSIX installed path or footprint alias")
    return decoded


def verify_archive(package: Path, inventory_path: Path, profile_path: Path, *, production: bool = False) -> dict:
    if package.suffix.lower() != ".msix" or not package.is_file():
        raise ValueError("verification requires an MSIX file")
    profile = load_profile(profile_path, production=production)
    inventory = read_json(inventory_path, maximum=16 * 1024 * 1024)
    if (inventory.get("schema") != "ofca-msix-inventory/v1"
            or inventory.get("profile_sha256") != sha256(profile_path)
            or inventory.get("identity") != profile["identity"]
            or inventory.get("package_version") != package_version(profile["release_sequence"])
            or inventory.get("distribution") != profile["distribution"]
            or inventory.get("purpose") != profile["purpose"]):
        raise ValueError("package inventory disagrees with the expected profile")
    records = inventory.get("files")
    if not isinstance(records, list) or not 1 <= len(records) <= 50000:
        raise ValueError("invalid package inventory")
    expected = {}
    for record in records:
        if (not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}
                or not isinstance(record["path"], str) or not _safe_archive_path(record["path"])
                or record["path"] in expected or not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"]))
                or type(record["size"]) is not int or not 0 <= record["size"] <= 4294967295):
            raise ValueError("invalid package inventory entry")
        expected[record["path"]] = record
    if len({path.casefold() for path in expected}) != len(expected):
        raise ValueError("package inventory has case aliases")
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        names = [installed_archive_path(member.filename) for member in members]
        if (len(members) > 50004 or len(set(name.casefold() for name in names)) != len(names)
                or any(not _safe_archive_path(name) for name in names)):
            raise ValueError("package archive contains aliases, unsafe paths or excessive entries")
        if set(names) - FOOTPRINT_FILES != set(expected):
            raise ValueError("package has extra or missing installed files")
        if not {"AppxBlockMap.xml", "[Content_Types].xml"} <= set(names):
            raise ValueError("package has no MSIX deployment footprint")
        by_installed_path = dict(zip(names, members))
        for name, record in expected.items():
            member = by_installed_path[name]
            if member.file_size != record["size"] or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("package file size or type differs from inventory")
            hasher = hashlib.sha256()
            with archive.open(member) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != record["sha256"]:
                raise ValueError("package payload digest mismatch")
        if archive.read("AppxManifest.xml") != render_manifest(profile):
            raise ValueError("package manifest differs from the expected identity and policy")
        if archive.read("app/desktop-release.json") != canonical_json(desktop_descriptor(profile, sha256(profile_path))):
            raise ValueError("package desktop descriptor disagrees with its profile")
    return {"schema": "ofca-msix-verification/v1", "verification_scope": "payload-inventory", "signature": "not-checked",
            "package_sha256": sha256(package),
            "profile_sha256": sha256(profile_path), "inventory_sha256": sha256(inventory_path),
            "identity": profile["identity"], "package_version": package_version(profile["release_sequence"]),
            "file_count": len(expected)}


def verify_unpacked(directory: Path, inventory_path: Path) -> None:
    inventory = read_json(inventory_path, maximum=16 * 1024 * 1024)
    expected = {record["path"]: record for record in inventory["files"]}
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
    if actual - FOOTPRINT_FILES != set(expected):
        raise ValueError("MakeAppx unpacked closure differs from the sealed inventory")
    for path, record in expected.items():
        if file_record(directory / path, path) != record:
            raise ValueError("MakeAppx unpacked bytes differ from the sealed inventory")


def render_appinstaller(profile: dict, package_uri: str) -> bytes:
    validate_profile(profile)
    if profile["distribution"] != "direct-appinstaller":
        raise ValueError("Store distribution has no direct App Installer descriptor")
    descriptor = urlsplit(profile["app_installer_uri"])
    package = urlsplit(package_uri)
    if (package.scheme != "https" or package.netloc != descriptor.netloc or package.query or package.fragment
            or not package.path.endswith(".msix") or package.username is not None or package.password is not None
            or "\\" in package_uri or any(ord(character) <= 32 for character in package_uri)):
        raise ValueError("package URI must be immutable HTTPS bytes under the approved descriptor origin")
    namespace = "http://schemas.microsoft.com/appx/appinstaller/2018"
    ET.register_namespace("", namespace)
    root = ET.Element("{" + namespace + "}AppInstaller", {"Version": package_version(profile["release_sequence"]),
                                                       "Uri": profile["app_installer_uri"]})
    ET.SubElement(root, "{" + namespace + "}MainPackage", {
        "Name": profile["identity"]["name"], "Publisher": profile["identity"]["publisher"],
        "Version": package_version(profile["release_sequence"]), "ProcessorArchitecture": "x64", "Uri": package_uri,
    })
    settings = ET.SubElement(root, "{" + namespace + "}UpdateSettings")
    ET.SubElement(settings, "{" + namespace + "}OnLaunch", {
        "HoursBetweenUpdateChecks": "6", "ShowPrompt": "false", "UpdateBlocksActivation": "false",
    })
    ET.SubElement(settings, "{" + namespace + "}AutomaticBackgroundTask")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate-profile", "prepare", "seal", "verify", "verify-unpacked", "appinstaller", "desktop-descriptor"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--working", type=Path)
    parser.add_argument("--makeappx", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--package-uri")
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    try:
        if options.operation == "validate-profile":
            profile = load_profile(options.profile, production=options.production)
            result = {"package_version": package_version(profile["release_sequence"]), "identity": profile["identity"]}
        elif options.operation == "desktop-descriptor":
            if options.output is None:
                raise ValueError("desktop-descriptor requires an output file")
            options.output.write_bytes(canonical_json(desktop_descriptor(load_profile(options.profile, production=options.production), sha256(options.profile))))
            result = {"desktop_descriptor": str(options.output)}
        elif options.operation == "prepare":
            if options.stage is None or options.working is None:
                raise ValueError("prepare requires stage and working directories")
            result = prepare_payload(options.stage, options.working, options.profile)
        elif options.operation == "seal":
            if options.working is None or options.makeappx is None:
                raise ValueError("seal requires working directory and MakeAppx")
            result = seal_payload(options.working, options.profile, options.makeappx)
            result = {"inventory": str(options.working / "inventory.json"), "file_count": len(result["files"])}
        elif options.operation == "verify":
            if options.package is None or options.inventory is None:
                raise ValueError("verify requires package and inventory")
            result = verify_archive(options.package, options.inventory, options.profile, production=options.production)
        elif options.operation == "verify-unpacked":
            if options.directory is None or options.inventory is None:
                raise ValueError("verify-unpacked requires directory and inventory")
            verify_unpacked(options.directory, options.inventory)
            result = {"unpacked": "verified"}
        else:
            if options.package_uri is None or options.output is None:
                raise ValueError("appinstaller requires package URI and output")
            options.output.write_bytes(render_appinstaller(load_profile(options.profile), options.package_uri))
            result = {"appinstaller": str(options.output)}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        print("msix-packaging: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
