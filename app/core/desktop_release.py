"""Immutable desktop profile bound to the calling process's Windows identity.

This describes executable delivery and storage isolation, never account or
commercial authority. It has no downloader, registry writer or update fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


class DesktopReleaseError(RuntimeError):
    """Missing, invalid or inconsistent packaged installation metadata."""


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    name: str
    publisher: str
    version: str
    architecture: str
    family: str | None = None


@dataclass(frozen=True, slots=True)
class EngineWindowsIdentity:
    name: str
    publisher: str
    family: str
    architecture: str


@dataclass(frozen=True, slots=True)
class EngineRelease:
    identity: EngineWindowsIdentity
    package_version: str


@dataclass(frozen=True, slots=True)
class MainPackage:
    kind: str


@dataclass(frozen=True, slots=True)
class RelatedBundle:
    kind: str
    related_set_id: str
    engine: EngineRelease


@dataclass(frozen=True, slots=True)
class MainPackageOnly:
    kind: str


@dataclass(frozen=True, slots=True)
class MainPackageAndRelatedBundle:
    kind: str
    engine_identity: EngineWindowsIdentity


@dataclass(frozen=True, slots=True)
class Servicing:
    installed_artifact: MainPackage | RelatedBundle
    allowed_targets: MainPackageOnly | MainPackageAndRelatedBundle


@dataclass(frozen=True, slots=True)
class DesktopRelease:
    purpose: str
    identity: PackageIdentity
    application_id: str
    update_owner: str
    app_installer_uri: str | None
    schema: str = "ofca-desktop-release/v1"
    application_version: str | None = None
    servicing: Servicing | None = None

    def allowed_target_kinds(self) -> frozenset[str]:
        if self.servicing is None or isinstance(self.servicing.allowed_targets, MainPackageOnly):
            declared = {"main_package"}
        else:
            declared = {"main_package", "related_bundle"}
        layout = ({"related_bundle"} if self.servicing is not None
                  and isinstance(self.servicing.installed_artifact, RelatedBundle)
                  else {"main_package", "related_bundle"})
        return frozenset(declared & layout)

    def admit_target(self, kind: str) -> None:
        if kind not in self.allowed_target_kinds():
            raise DesktopReleaseError("update_target_layout_not_allowed")


def parse_servicing(value: object, *, main_name: str, publisher: str,
                    main_family: str | None = None) -> Servicing:
    """Closed structure; family authority, when supplied, comes from Windows."""
    if main_family is not None and (
            not isinstance(main_family, str) or
            re.fullmatch(re.escape(main_name) + r"_[a-z0-9]{13}", main_family) is None):
        raise ValueError("invalid Windows Main family")
    def closed(item, keys):
        if type(item) is not dict or set(item) != set(keys):
            raise ValueError("invalid servicing members")
    def engine(item):
        closed(item, ("name", "publisher", "family", "architecture"))
        name, family = item["name"], item["family"]
        if (not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9.-]{3,50}", name) is None
                or name.casefold() == main_name.casefold() or item["publisher"] != publisher
                or item["architecture"] != "x64" or not isinstance(family, str)
                or re.fullmatch(re.escape(name) + r"_[a-z0-9]{13}", family) is None):
            raise ValueError("invalid engine identity")
        if main_family is not None:
            prefix = main_name + "_"
            if (not isinstance(main_family, str) or not main_family.startswith(prefix)
                    or re.fullmatch(r"[a-z0-9]{13}", main_family[len(prefix):]) is None
                    or family != name + "_" + main_family[len(prefix):]):
                raise ValueError("engine publisher family mismatch")
        return EngineWindowsIdentity(**item)
    closed(value, ("installed_artifact", "allowed_targets"))
    allowed, installed = value["allowed_targets"], value["installed_artifact"]
    if type(allowed) is dict and allowed.get("kind") == "main_package_only":
        closed(allowed, ("kind",))
        capability = MainPackageOnly(**allowed)
    else:
        closed(allowed, ("kind", "engine_identity"))
        if allowed["kind"] != "main_package_and_related_bundle":
            raise ValueError("unknown target capability")
        capability = MainPackageAndRelatedBundle(allowed["kind"], engine(allowed["engine_identity"]))
    if type(installed) is dict and installed.get("kind") == "main_package":
        closed(installed, ("kind",))
        layout = MainPackage(**installed)
    else:
        closed(installed, ("kind", "related_set_id", "engine"))
        if (installed["kind"] != "related_bundle" or not isinstance(installed["related_set_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", installed["related_set_id"]) is None):
            raise ValueError("invalid related set")
        item = installed["engine"]
        closed(item, ("identity", "package_version"))
        version = item["package_version"]
        if (not isinstance(version, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", version) is None
                or any(int(n) > 65535 for n in version.split(".")) or int(version.split(".")[0]) == 0):
            raise ValueError("invalid engine version")
        release = EngineRelease(engine(item["identity"]), version)
        if not isinstance(capability, MainPackageAndRelatedBundle) or release.identity != capability.engine_identity:
            raise ValueError("related layout requires its exact engine capability")
        layout = RelatedBundle(installed["kind"], installed["related_set_id"], release)
    return Servicing(layout, capability)


def windows_package_identity() -> PackageIdentity | None:
    """Get identity from Windows, without trusting environment overrides."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    class PackageId(ctypes.Structure):
        _pack_ = 4
        _fields_ = [("reserved", wintypes.DWORD), ("architecture", wintypes.DWORD),
                    ("version", ctypes.c_uint64), ("name", ctypes.c_void_p),
                    ("publisher", ctypes.c_void_p), ("resource", ctypes.c_void_p),
                    ("publisher_id", ctypes.c_void_p)]
    get = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentPackageId
    get.argtypes = [ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    get.restype = wintypes.LONG
    size = wintypes.DWORD()
    status = get(ctypes.byref(size), None)
    if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE
        return None
    if status != 122 or not ctypes.sizeof(PackageId) <= size.value <= 65536:
        raise DesktopReleaseError("package_identity_unavailable")
    data = ctypes.create_string_buffer(size.value)
    if get(ctypes.byref(size), data) != 0:
        raise DesktopReleaseError("package_identity_unavailable")
    package = PackageId.from_buffer(data)
    start, end = ctypes.addressof(data), ctypes.addressof(data) + size.value
    def bounded_string(pointer):
        if not pointer or pointer < start or pointer >= end or pointer % 2:
            raise DesktopReleaseError("package_identity_unavailable")
        limit = (end - pointer) // 2
        values = (ctypes.c_uint16 * limit).from_address(pointer)
        for index, value in enumerate(values):
            if value == 0:
                return ctypes.wstring_at(pointer, index)
        raise DesktopReleaseError("package_identity_unavailable")
    version = package.version
    return PackageIdentity(bounded_string(package.name), bounded_string(package.publisher),
                           ".".join(str((version >> shift) & 65535) for shift in (48, 32, 16, 0)),
                           "x64" if package.architecture == 9 else "unsupported",
                           bounded_string(package.name) + "_" + bounded_string(package.publisher_id))


def load_desktop_release(path: Path, identity: PackageIdentity | None) -> DesktopRelease | None:
    if not path.exists():
        if identity is not None:
            raise DesktopReleaseError("packaged_descriptor_missing")
        return None
    if identity is None:
        raise DesktopReleaseError("descriptor_without_package_identity")
    if path.stat().st_size > 65536:
        raise DesktopReleaseError("desktop_descriptor_invalid")
    try:
        return parse_desktop_release(path.read_bytes(), identity)
    except OSError as error:
        raise DesktopReleaseError("desktop_descriptor_invalid") from error


def parse_desktop_release(body: bytes, identity: PackageIdentity, *,
                          expected_application_version: str | None = None) -> DesktopRelease:
    """Parse against an actual identity; future targets omit the old build version.

    The current-process native caller supplies its compiled build version.
    A staged target is validated against its own Windows package identity.
    """
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DesktopReleaseError("desktop_descriptor_invalid")
            result[key] = value
        return result
    try:
        if not 0 < len(body) <= 65536:
            raise ValueError()
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        fields = {"schema", "purpose", "identity", "application_version", "package_version", "profile_sha256",
                  "update_owner", "app_installer_uri", "storage_policy", "executable_family", "webview2_minimum_version"}
        if isinstance(value, dict) and value.get("schema") == "ofca-desktop-release/v2":
            fields.add("servicing")
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError()
        declared = value["identity"]
        if not isinstance(declared, dict) or set(declared) != {"name", "publisher", "publisher_display_name", "application_id", "architecture"}:
            raise ValueError()
        if (any(not isinstance(item, str) or not item or item != item.strip() or any(ord(char) < 32 for char in item)
                for item in declared.values()) or re.fullmatch(r"[A-Za-z0-9.-]{3,50}", declared["name"]) is None
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", declared["application_id"]) is None
                or len(declared["publisher"]) > 8192 or len(declared["publisher_display_name"]) > 256):
            raise ValueError()
        if (value["schema"] not in {"ofca-desktop-release/v1", "ofca-desktop-release/v2"} or value["purpose"] not in {"production", "qualification"}
                or value["storage_policy"] != "retained-product-root/v1" or value["executable_family"] != "brain-bridge/v1"
                or re.fullmatch(r"[0-9a-f]{64}", value["profile_sha256"]) is None
                or not isinstance(value["application_version"], str)
                or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?", value["application_version"]) is None):
            raise ValueError()
        for field in ("package_version", "webview2_minimum_version"):
            text = value[field]
            if (not isinstance(text, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", text) is None
                    or any(int(part) > 65535 for part in text.split("."))):
                raise ValueError()
        expected = PackageIdentity(declared["name"], declared["publisher"], value["package_version"], declared["architecture"])
        if ((expected.name, expected.publisher, expected.version, expected.architecture) !=
                (identity.name, identity.publisher, identity.version, identity.architecture)
                or identity.architecture != "x64"):
            raise DesktopReleaseError("desktop_identity_mismatch")
        synthetic_name = identity.name.lower().startswith("ofca.qualification.")
        synthetic_publisher = identity.publisher.lower().startswith("cn=ofca msix qualification")
        if ((value["purpose"] == "qualification" and not (synthetic_name and synthetic_publisher))
                or (value["purpose"] == "production" and (synthetic_name or synthetic_publisher))):
            raise ValueError()
        uri = value["app_installer_uri"]
        owner = value["update_owner"]
        if owner == "microsoft-store":
            if uri is not None:
                raise ValueError()
        elif owner == "direct-appinstaller":
            if not isinstance(uri, str) or len(uri) > 2048 or "\\" in uri or any(ord(char) <= 32 for char in uri):
                raise ValueError()
            parsed = urlsplit(uri)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment or not parsed.path.endswith(".appinstaller")):
                raise ValueError()
        else:
            raise ValueError()
        servicing = None
        if value["schema"] == "ofca-desktop-release/v2":
            if owner != "direct-appinstaller" or identity.family is None or len(value["application_version"]) > 64:
                raise ValueError()
            servicing = parse_servicing(value["servicing"], main_name=identity.name,
                                        publisher=identity.publisher, main_family=identity.family)
        if expected_application_version is not None and value["application_version"] != expected_application_version:
            raise ValueError()
        return DesktopRelease(value["purpose"], identity, declared["application_id"], owner, uri,
                              value["schema"], value["application_version"], servicing)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DesktopReleaseError("desktop_descriptor_invalid") from error


def current_desktop_release() -> DesktopRelease | None:
    return load_desktop_release(Path(sys.executable).parent / "desktop-release.json", windows_package_identity())
