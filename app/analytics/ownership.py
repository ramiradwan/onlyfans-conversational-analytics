"""Process identity evidence for reclaimable projection build leases."""

from __future__ import annotations

import os
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_PROCESS_INSTANCE_NONCE = uuid4().hex


@dataclass(frozen=True, slots=True)
class BuildOwner:
    owner_id: str
    pid: int
    process_started_at: str
    instance_nonce: str
    capability_secret: str = field(repr=False)
    capability_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capability_digest",
            capability_digest(self.capability_secret),
        )

    @property
    def persisted_identity(self) -> tuple[str, int, str, str, str]:
        return (
            self.owner_id,
            self.pid,
            self.process_started_at,
            self.instance_nonce,
            self.capability_digest,
        )


def capability_digest(secret: str) -> str:
    if not secret:
        raise ValueError("writer_capability_invalid")
    return "sha256:" + hashlib.sha256(
        b"ofca:projection-writer-capability:v1\0" + secret.encode("ascii")
    ).hexdigest()


def current_build_owner(owner_id: str | None = None) -> BuildOwner:
    secret = secrets.token_hex(32)
    return BuildOwner(
        owner_id=owner_id or str(uuid4()),
        pid=os.getpid(),
        process_started_at=_PROCESS_STARTED_AT,
        instance_nonce=_PROCESS_INSTANCE_NONCE,
        capability_secret=secret,
    )


def process_is_definitely_dead(pid: int | None) -> bool:
    """Return true only when the local OS positively reports no such process."""

    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            open_process.restype = ctypes.c_void_p
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            get_exit_code.restype = ctypes.c_int
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            ctypes.set_last_error(0)
            handle = open_process(
                process_query_limited_information, False, pid
            )
            if not handle:
                return ctypes.get_last_error() in {87, 1168}
            try:
                exit_code = ctypes.c_ulong()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value != still_active
            finally:
                close_handle(handle)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False
