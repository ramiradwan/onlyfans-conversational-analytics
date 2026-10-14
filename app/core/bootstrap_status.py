"""Bounded child-owned startup observations; these never admit a listener."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import stat
import time

HANDLE_ENV = "BRAIN_BOOTSTRAP_STATUS_HANDLE"
SCHEMA = "ofca-bootstrap-status/v1"
MAX_FRAME_BYTES = 512
MAX_FRAMES = 256
MAX_TOTAL_BYTES = 65536
HEARTBEAT_SECONDS = 5
PHASES = ("starting", "checking", "checkpointing", "migrating", "validating",
          "ready-to-check-listener", "recovery-required")
REASONS = {"retained-data", "writer-conflict", "maintenance", "cancelled", "installation"}


def _descriptor(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise ValueError("bootstrap status requires an inherited pipe")
    handle = int(value)
    if os.name == "nt":
        import ctypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetFileType.argtypes = [ctypes.c_void_p]
        kernel.GetFileType.restype = ctypes.c_uint32
        if kernel.GetFileType(handle) != 3:
            raise ValueError("bootstrap status requires an inherited pipe")
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    else:
        descriptor = handle
        if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise ValueError("bootstrap status requires an inherited pipe")
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class BootstrapStatus:
    def __init__(self, descriptor: int | None, stopped):
        self.descriptor = descriptor
        self.stopped = stopped
        self.phase = None
        self.sequence = 0
        self.total_bytes = 0
        self.last_emitted = 0.0
        self.terminal = False

    def emit(self, phase: str, reason: str | None = None) -> None:
        if (phase not in PHASES or self.terminal or (self.phase is not None and
                PHASES.index(phase) < PHASES.index(self.phase))
                or (phase == "recovery-required" and reason not in REASONS)
                or (phase != "recovery-required" and reason is not None)):
            raise ValueError("bootstrap status transition refused")
        self.phase = phase
        self.last_emitted = time.monotonic()
        self.terminal = phase in {"ready-to-check-listener", "recovery-required"}
        if self.descriptor is None:
            return
        self.sequence += 1
        frame = (json.dumps({"schema": SCHEMA, "seq": self.sequence, "phase": phase, "reason": reason},
                            separators=(",", ":")) + "\n").encode("utf-8")
        self.total_bytes += len(frame)
        if len(frame) > MAX_FRAME_BYTES or self.sequence > MAX_FRAMES or self.total_bytes > MAX_TOTAL_BYTES:
            self.stopped.set()
            return
        try:
            if os.write(self.descriptor, frame) != len(frame):
                self.stopped.set()
        except OSError:
            self.stopped.set()

    def cancelled(self) -> bool:
        if not self.terminal and self.phase is not None and time.monotonic() - self.last_emitted >= HEARTBEAT_SECONDS:
            self.emit(self.phase)
        return self.stopped.is_set()

    def maintenance_phase(self, phase: str) -> None:
        self.emit("validating" if phase == "complete" else phase)

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@contextmanager
def bootstrap_status(stopped):
    value = os.environ.pop(HANDLE_ENV, None)
    status = BootstrapStatus(_descriptor(value) if value is not None else None, stopped)
    try:
        status.emit("starting")
        yield status
    finally:
        status.close()
