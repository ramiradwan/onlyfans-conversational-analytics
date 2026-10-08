"""Join ordinary server shutdown to an optional parent-owned pipe lifetime."""

from __future__ import annotations

from contextlib import contextmanager
import os
import select
import stat
import threading


HANDLE_ENV = "BRAIN_PARENT_LIFETIME_HANDLE"


def _pipe_descriptor(value):
    if not value.isdecimal():
        raise ValueError("parent lifetime requires an inherited pipe")
    handle = int(value)
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileType.argtypes = [ctypes.c_void_p]
        kernel.GetFileType.restype = ctypes.c_uint32
        if kernel.GetFileType(handle) != 3:
            raise ValueError("parent lifetime requires an inherited pipe")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    else:
        descriptor = handle
        if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise ValueError("parent lifetime requires an inherited pipe")
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _parent_closed(descriptor):
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        kernel.PeekNamedPipe.restype = ctypes.c_int
        available = ctypes.c_uint32()
        if not kernel.PeekNamedPipe(msvcrt.get_osfhandle(descriptor), None, 0,
                                     None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232, 233):
                return True
            raise OSError(error, "parent lifetime pipe unavailable")
        return available.value > 0 and os.read(descriptor, min(available.value, 4096)) == b""
    readable, _, _ = select.select([descriptor], [], [], 0)
    return bool(readable) and os.read(descriptor, 4096) == b""


@contextmanager
def parent_lifetime(request_exit):
    """Request normal shutdown when the supervising parent closes its pipe."""
    value = os.environ.pop(HANDLE_ENV, None)
    if value is None:
        yield
        return
    descriptor = _pipe_descriptor(value)
    stopped = threading.Event()

    def observe():
        while not stopped.is_set():
            try:
                closed = _parent_closed(descriptor)
            except OSError:
                closed = True
            if closed:
                request_exit()
                return
            stopped.wait(.05)

    thread = threading.Thread(target=observe, name="parent-lifetime", daemon=True)
    try:
        thread.start()
        yield
    finally:
        stopped.set()
        if thread.ident is not None:
            thread.join(timeout=1)
        os.close(descriptor)
        if thread.is_alive():
            raise RuntimeError("parent lifetime observer did not join")
