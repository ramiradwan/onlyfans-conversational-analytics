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


@dataclass(frozen=True, slots=True)
class DesktopRelease:
    purpose: str
    identity: PackageIdentity
    application_id: str
    update_owner: str
    app_installer_uri: str | None


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
                           "x64" if package.architecture == 9 else "unsupported")


def load_desktop_release(path: Path, identity: PackageIdentity | None) -> DesktopRelease | None:
    if not path.exists():
        if identity is not None:
            raise DesktopReleaseError("packaged_descriptor_missing")
        return None
    if identity is None:
        raise DesktopReleaseError("descriptor_without_package_identity")
    if path.stat().st_size > 65536:
        raise DesktopReleaseError("desktop_descriptor_invalid")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DesktopReleaseError("desktop_descriptor_invalid")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
        fields = {"schema", "purpose", "identity", "application_version", "package_version", "profile_sha256",
                  "update_owner", "app_installer_uri", "storage_policy", "executable_family", "webview2_minimum_version"}
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
        if (value["schema"] != "ofca-desktop-release/v1" or value["purpose"] not in {"production", "qualification"}
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
        if expected != identity or identity.architecture != "x64":
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
        return DesktopRelease(value["purpose"], identity, declared["application_id"], owner, uri)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DesktopReleaseError("desktop_descriptor_invalid") from error


def current_desktop_release() -> DesktopRelease | None:
    return load_desktop_release(Path(sys.executable).parent / "desktop-release.json", windows_package_identity())
