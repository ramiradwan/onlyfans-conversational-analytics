"""Fail-closed private-file security and durable atomic publication helpers."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class PrivateFileSecurityError(RuntimeError):
    """Raised when owner-only file security cannot be established or verified."""


def reject_path_aliases(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if ".." in candidate.parts:
        raise PrivateFileSecurityError("parent path aliases are not allowed")
    absolute = Path(os.path.abspath(os.fspath(candidate)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            raise PrivateFileSecurityError("links and reparse points are not allowed")
    return absolute


def apply_private_file_security(
    path: str | Path, *, platform_name: str | None = None
) -> None:
    target = Path(path)
    selected = os.name if platform_name is None else platform_name
    try:
        if selected == "nt":
            _set_windows_owner_only_acl(target)
            if not _windows_acl_is_owner_only(target):
                raise OSError("owner-only DACL verification failed")
        else:
            os.chmod(target, 0o600)
            if stat.S_IMODE(target.stat().st_mode) != 0o600:
                raise OSError("owner-only mode verification failed")
    except OSError as error:
        raise PrivateFileSecurityError(
            "private file security could not be established"
        ) from error


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_file(path: str | Path) -> None:
    with open(path, "r+b") as handle:
        os.fsync(handle.fileno())


def sync_directory(path: str | Path) -> None:
    """Flush directory metadata where the host exposes a portable directory fd.

    CPython on Windows does not expose ``O_DIRECTORY`` or a supported directory
    handle ``fsync`` equivalent. Files are still flushed before atomic replace,
    but the containing-directory metadata flush cannot be requested there.
    """

    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    descriptor = os.open(path, os.O_RDONLY | directory_flag)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sid_string(sid: object, advapi32: object, kernel32: object) -> str:
    import ctypes

    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert.restype = ctypes.c_int
    value = ctypes.c_wchar_p()
    if not convert(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    try:
        return str(value.value)
    finally:
        local_free(ctypes.cast(value, ctypes.c_void_p))


def _set_windows_owner_only_acl(path: Path) -> None:
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = get_security(
        str(path),
        1,
        0x00000001,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        owner_sid = _sid_string(owner, advapi32, kernel32)
    finally:
        local_free(descriptor)

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    convert.restype = ctypes.c_int
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    get_dacl.restype = ctypes.c_int
    set_security = advapi32.SetNamedSecurityInfoW
    set_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = ctypes.c_uint32
    private_descriptor = ctypes.c_void_p()
    if not convert(
        f"D:P(A;;FA;;;{owner_sid})",
        1,
        ctypes.byref(private_descriptor),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = ctypes.c_int()
        defaulted = ctypes.c_int()
        dacl = ctypes.c_void_p()
        if not get_dacl(
            private_descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise OSError("private DACL was not created")
        result = set_security(
            str(path),
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ctypes.WinError(result)
    finally:
        local_free(private_descriptor)


def _windows_acl_is_owner_only(path: Path) -> bool:
    import ctypes

    class Acl(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", ctypes.c_ushort),
            ("AceCount", ctypes.c_ushort),
            ("Sbz2", ctypes.c_ushort),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", ctypes.c_ushort),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.restype = ctypes.c_int
    get_ace = advapi32.GetAce
    get_ace.restype = ctypes.c_int
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = get_security(
        str(path),
        1,
        0x00000001 | 0x00000004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        control = ctypes.c_ushort()
        revision = ctypes.c_uint32()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not dacl.value or not control.value & 0x1000:
            return False
        acl = ctypes.cast(dacl, ctypes.POINTER(Acl)).contents
        if acl.AceCount != 1:
            return False
        ace = ctypes.c_void_p()
        if not get_ace(dacl, 0, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
        mask = ctypes.c_uint32.from_address(ace.value + 4).value
        ace_sid = ctypes.c_void_p(ace.value + 8)
        return bool(
            header.AceType == 0
            and mask & 0x001F01FF == 0x001F01FF
            and _sid_string(ace_sid, advapi32, kernel32)
            == _sid_string(owner, advapi32, kernel32)
        )
    finally:
        local_free(descriptor)
