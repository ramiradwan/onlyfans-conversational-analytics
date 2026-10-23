"""Retain verified model data while a private worker may read it."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from app.nlp.artifact_store import DATA_ROLES, _filename
from app.nlp.child_protocol import ArtifactHandle
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profiles import artifact_closure_digest
from app.persistence.private_files import (apply_private_directory_security,
    private_file_identity, reject_path_aliases)


@dataclass(frozen=True)
class RetainedData:
    artifacts: tuple[ArtifactHandle, ...]
    streams: tuple


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


@contextmanager
def _windows_handle(path, *, directory):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Ancestors deny rename; leaves additionally deny concurrent writes.
    access, share = (0x80, 3) if directory else (0x80000000, 1)
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = kernel.CreateFileW(str(path), access, share, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise NlpBoundaryError('nlp_artifact_open')
    try:
        yield handle
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _directory(path):
    path = reject_path_aliases(path)
    apply_private_directory_security(path, repair_permissions=False)
    before = path.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise NlpBoundaryError('nlp_artifact_directory')
    if os.name == 'nt':
        with _windows_handle(path, directory=True):
            if _identity(path.lstat()) != _identity(before):
                raise NlpBoundaryError('nlp_artifact_changed')
            yield
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise NlpBoundaryError('nlp_artifact_changed')
            yield
        finally:
            os.close(descriptor)


@contextmanager
def _file(path, artifact, check):
    path = reject_path_aliases(path)
    private_file_identity(path, repair_permissions=False)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise NlpBoundaryError('nlp_artifact_file')
    with ExitStack() as stack:
        if os.name == 'nt':
            import msvcrt
            import ctypes
            from ctypes import wintypes

            original = stack.enter_context(_windows_handle(path, directory=False))
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.DuplicateHandle.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            duplicate = wintypes.HANDLE()
            process = kernel.GetCurrentProcess()
            if not kernel.DuplicateHandle(process, original, process, ctypes.byref(duplicate), 0, False, 2):
                raise NlpBoundaryError('nlp_artifact_open')
            try:
                descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                kernel.CloseHandle(duplicate)
                raise
            native = original
        else:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            native = descriptor
        try:
            opened_stream = os.fdopen(descriptor, 'rb', buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        stream = stack.enter_context(opened_stream)
        opened = os.fstat(stream.fileno())
        if (_identity(opened) != _identity(before) or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1 or opened.st_size != artifact.size_bytes):
            raise NlpBoundaryError('nlp_artifact_changed')
        digest = hashlib.sha256()
        read = 0
        while block := stream.read(1024 * 1024):
            check()
            read += len(block)
            if read > artifact.size_bytes:
                raise NlpBoundaryError('nlp_artifact_mismatch')
            digest.update(block)
        if (read != artifact.size_bytes or digest.hexdigest() != artifact.sha256
                or _identity(path.lstat()) != _identity(opened)):
            raise NlpBoundaryError('nlp_artifact_mismatch')
        stream.seek(0)
        check()
        yield stream, native


@contextmanager
def retain_data(profile, available, check):
    """Availability paths are rechecked and never used as executable authority."""
    members = tuple(a for a in profile.artifacts if a.role in DATA_ROLES)
    expected = {a.artifact_id: a for a in members}
    actual = {a.artifact.artifact_id: a for a in available.artifacts}
    directory = Path(available.directory)
    closure = artifact_closure_digest(members)
    if (available.profile_sha256 != profile.identity_digest() or available.data_closure_sha256 != closure
            or not directory.is_absolute() or directory.name != closure
            or len(actual) != len(available.artifacts) or set(actual) != set(expected)
            or any(actual[key].artifact != value or actual[key].path != directory / _filename(value)
                   for key, value in expected.items())):
        raise NlpBoundaryError('nlp_artifact_mismatch')
    with ExitStack() as stack:
        stack.enter_context(_directory(directory.parent))
        stack.enter_context(_directory(directory))
        streams, handles = [], []
        for artifact in members:
            check()
            stream, handle = stack.enter_context(_file(actual[artifact.artifact_id].path, artifact, check))
            streams.append(stream)
            handles.append(ArtifactHandle(artifact=artifact, handle=handle))
        yield RetainedData(tuple(handles), tuple(streams))
