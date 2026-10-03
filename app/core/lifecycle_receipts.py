"""Passive, bounded lifecycle receipts for an inherited supervisor pipe."""

from __future__ import annotations

import hashlib
from functools import wraps
import json
import os
import queue
import re
import stat
import threading
import time

_sink = None
_FIELDS = frozenset({
    "account_ref", "canonical_revision", "generation_id", "origin",
    "admitted_events", "before_message_count", "after_message_count",
    "source_event_ref", "pending_accounts", "recovery_requests",
    "active_publications", "workers_joined", "clock_resolution_ns",
    "clock_implementation", "projection_digest", "graph_digest",
    "canonical_content_digest", "complete",
    "attempt_id", "full_rebuild",
})


def _passive(operation):
    @wraps(operation)
    def observe(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            if _sink is not None:
                _sink.failed = True
            return None
    return observe


class ReceiptSink:
    def __init__(self, descriptor: int, run_id: str, capacity: int = 4096):
        self.descriptor = descriptor
        self.run_id = run_id
        self.queue = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.sequence = 0
        self.failed = False
        self.closed = False
        self.thread = threading.Thread(target=self._write, daemon=True,
                                       name="lifecycle-receipts")
        self.thread.start()

    def emit(self, event_type: str, fields: dict):
        with self.lock:
            if self.closed:
                return
            if not fields.keys() <= _FIELDS:
                self.failed = True
                return
            self.sequence += 1
            record = dict(schema="analytics-lifecycle.v1", sequence=self.sequence,
                          run_id=self.run_id, pid=os.getpid(), event_type=event_type,
                          monotonic_ns=time.monotonic_ns(), **fields)
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self.failed = True

    def _write(self):
        try:
            while True:
                record = self.queue.get()
                payload = (json.dumps(record, separators=(",", ":")) + "\n").encode()
                while payload:
                    count = os.write(self.descriptor, payload)
                    if count <= 0:
                        raise OSError("receipt pipe closed")
                    payload = payload[count:]
                if record["event_type"] == "footer":
                    return
        except (OSError, ValueError, TypeError):
            self.failed = True
        finally:
            os.close(self.descriptor)

    def finish(self, workers_joined: bool):
        with self.lock:
            if self.closed:
                return
            self.sequence += 1
            footer = dict(schema="analytics-lifecycle.v1", sequence=self.sequence,
                          run_id=self.run_id, pid=os.getpid(), event_type="footer",
                          monotonic_ns=time.monotonic_ns(),
                          complete=workers_joined and not self.failed)
            try:
                self.queue.put_nowait(footer)
            except queue.Full:
                self.failed = True
            self.closed = True
        self.thread.join(timeout=1)


@_passive
def start():
    """Accept only a pipe supplied by the process supervisor."""
    global _sink
    if _sink is not None:
        return
    handle = os.environ.pop("ANALYTICS_RECEIPT_HANDLE", "")
    run_id = os.environ.pop("ANALYTICS_RECEIPT_RUN_ID", "")
    if not re.fullmatch(r"[0-9a-f]{64}", run_id) or not handle.isdecimal():
        return
    descriptor = None
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetFileType.argtypes = [ctypes.c_void_p]
            kernel.GetFileType.restype = ctypes.c_uint32
            if kernel.GetFileType(int(handle)) != 3:
                return
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
        else:
            descriptor = int(handle)
            if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
                return
        os.set_inheritable(descriptor, False)
        _sink = ReceiptSink(descriptor, run_id)
        clock = time.get_clock_info("monotonic")
        emit("startup", clock_resolution_ns=max(1, int(clock.resolution * 1e9)),
             clock_implementation=clock.implementation)
    except (OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)


def enabled():
    return _sink is not None and not _sink.closed


def opaque(value: str, kind: str = "account") -> str:
    if _sink is None:
        return ""
    return hashlib.sha256(f"{_sink.run_id}\0{kind}\0{value}".encode()).hexdigest()


@_passive
def emit(event_type: str, *, account_id: str | None = None, **fields):
    if not enabled():
        return
    if account_id is not None:
        fields["account_ref"] = opaque(account_id)
    _sink.emit(event_type, fields)


@_passive
def stage(connection, event_type: str, *, account_id: str, **fields):
    if enabled():
        pending = getattr(connection, "_lifecycle_receipts", None)
        if pending is None:
            pending = connection._lifecycle_receipts = []
        pending.append((event_type, dict(account_id=account_id, **fields)))


@_passive
def committed(connection):
    for event_type, fields in getattr(connection, "_lifecycle_receipts", ()):
        emit(event_type, **fields)
    if hasattr(connection, "_lifecycle_receipts"):
        connection._lifecycle_receipts = []


@_passive
def begin_canonical(connection, account_id: str, origin: str, event_id: str):
    if enabled():
        count = connection.execute(
            "SELECT COUNT(*) FROM account_messages WHERE creator_account_id=?",
            (account_id,),
        ).fetchone()[0]
        connection._canonical_receipt = (account_id, origin, opaque(event_id, "event"), count)


@_passive
def end_canonical(connection, revision: int, admitted_events: int = 1):
    context = getattr(connection, "_canonical_receipt", None)
    if enabled() and context is not None:
        account_id, origin, event_ref, before = context
        after = connection.execute(
            "SELECT COUNT(*) FROM account_messages WHERE creator_account_id=?",
            (account_id,),
        ).fetchone()[0]
        stage(connection, "canonical_commit", account_id=account_id,
              canonical_revision=revision, origin=origin, source_event_ref=event_ref,
              admitted_events=admitted_events, before_message_count=before,
              after_message_count=after)


@_passive
def finish(workers_joined: bool):
    if enabled():
        emit("shutdown", workers_joined=workers_joined)
        _sink.finish(workers_joined)
