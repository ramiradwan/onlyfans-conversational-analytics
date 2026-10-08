"""Fail-closed private-file security and durable atomic publication helpers."""

from __future__ import annotations

import ctypes
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
            # A read connection must verify current permissions, not rewrite an
            # already-private DACL. Recheck on every call; never cache this fact.
            if not _windows_acl_is_owner_only(target):
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
    """Flush directory metadata after an atomic publication.

    CPython on Windows does not expose ``O_DIRECTORY`` or a supported directory
    handle ``fsync`` equivalent. Files are still flushed before atomic replace.
    Retention policy is intentionally owned by the recovery publication and
    runtime-maintenance layers rather than this generic durability primitive.
    """

    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is not None:
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


def _windows_acl_is_owner_only(path: Path, *, handle=None) -> bool:
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
    get_security = advapi32.GetNamedSecurityInfoW if handle is None else advapi32.GetSecurityInfo
    get_security.argtypes = [
        ctypes.c_wchar_p if handle is None else ctypes.c_void_p,
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
        str(path) if handle is None else handle,
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
        acl = Acl.from_address(dacl.value)
        if acl.AceCount != 1:
            return False
        ace = ctypes.c_void_p()
        if not get_ace(dacl, 0, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        header = AceHeader.from_address(ace.value)
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



def private_file_identity(path: str | Path, *, missing_ok: bool = False) -> tuple[int, int] | None:
    """Observe current owner-only permissions and identity on the same open file.

    Each call opens the path anew. No handle or authorization result is cached.
    The caller must make a new observation at its final currentness boundary.
    """
    target = Path(path)
    try:
        if os.name == 'nt':
            first = _windows_private_file_observation(target, missing_ok=missing_ok)
            if first is None:
                return None
            identity, private = first
            if not private:
                # Keep the established repair path; re-open and independently
                # verify both security and identity afterward. Replacement or a
                # failed repair cannot inherit the first observation.
                _set_windows_owner_only_acl(target)
                checked = _windows_private_file_observation(target)
                if checked != (identity, True):
                    raise OSError('file changed or private DACL repair not verified')
            return identity
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            handle = os.open(target, flags)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        try:
            metadata = os.fstat(handle)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError('private file must be a regular file')
            os.fchmod(handle, 0o600)
            metadata = os.fstat(handle)
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OSError('owner-only mode verification failed')
            return int(metadata.st_dev), int(metadata.st_ino)
        finally:
            os.close(handle)
    except OSError as error:
        raise PrivateFileSecurityError('private file identity/security could not be verified') from error


# Fixed ABI layouts: do not create a new cached pointer type per observation.
class _WindowsFileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]

class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [('attributes', ctypes.c_uint32), ('creation', _WindowsFileTime),
                ('access', _WindowsFileTime), ('write', _WindowsFileTime),
                ('volume', ctypes.c_uint32), ('size_high', ctypes.c_uint32),
                ('size_low', ctypes.c_uint32), ('links', ctypes.c_uint32),
                ('index_high', ctypes.c_uint32), ('index_low', ctypes.c_uint32)]


def _windows_private_file_observation(path: Path, *, missing_ok: bool = False):
    import ctypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]; close.restype = ctypes.c_int
    info = kernel.GetFileInformationByHandle
    info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WindowsFileInformation)]
    info.restype = ctypes.c_int
    # READ_CONTROL | FILE_READ_ATTRIBUTES; share read/write/delete; OPEN_EXISTING.
    # OPEN_REPARSE_POINT prevents following a changed final path component. No
    # backup privilege, file creation, priority boost or cache hint is requested.
    handle = create(str(path), 0x00020000 | 0x0080, 1 | 2 | 4,
                    None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if missing_ok and error in (2, 3):
            return None
        raise ctypes.WinError(error)
    try:
        metadata = _WindowsFileInformation()
        if not info(handle, ctypes.byref(metadata)):
            raise ctypes.WinError(ctypes.get_last_error())
        if metadata.attributes & (0x400 | 0x10):
            raise OSError('private file cannot be a reparse point or directory')
        identity = (int(metadata.volume), (int(metadata.index_high) << 32) | int(metadata.index_low))
        return identity, _windows_acl_is_owner_only(path, handle=handle)
    finally:
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())
