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


# One opt-in observer in a qualification child. There is no service, database
# write, per-record receipt or change to the decisions made by the runtime.
_startup_observer = None


class StartupTrace:
    MAX_SPANS = 256
    MAX_COUNTERS = 32

    def __init__(self, instance, started_at=None, *, clock=time.monotonic):
        self.instance, self.clock = instance, clock
        self.started = clock() if started_at is None else started_at
        self.lock = threading.RLock()
        self.spans, self.counters, self.stacks = [], {}, {}
        self.failed, self.closed, self.result = False, False, None
        self.ready_at = None

    def start(self):
        global _startup_observer
        if _startup_observer is not None:
            self.failed = True
            return
        _startup_observer = self

    def begin(self, name, fields):
        with self.lock:
            if self.closed or self.ready_at is not None:
                return None
            if len(self.spans) >= self.MAX_SPANS:
                self.failed = True
                return None
            thread = threading.get_ident()
            stack = self.stacks.setdefault(thread, [])
            index = len(self.spans)
            self.spans.append(dict(id=index, phase=name, thread=thread,
                parent=stack[-1] if stack else None, started=self.clock(), ended=None,
                status="running", **fields))
            stack.append(index)
            return index

    def end(self, index, ok):
        if index is None:
            return
        with self.lock:
            if self.closed:
                self.failed = True
                return
            row = self.spans[index]
            stack = self.stacks.get(row["thread"], [])
            if not stack or stack[-1] != index:
                self.failed = True
            else:
                stack.pop()
            row.update(ended=self.clock(), status="complete" if ok else "failed")
            self.failed |= not ok and (self.ready_at is None or row["ended"] <= self.ready_at)

    def count(self, name, value=1):
        with self.lock:
            if self.closed or self.ready_at is not None:
                return
            if name not in self.counters and len(self.counters) >= self.MAX_COUNTERS:
                self.failed = True
                return
            self.counters[name] = self.counters.get(name, 0) + value

    @staticmethod
    def union(intervals):
        used, end = 0.0, None
        for start, finish in sorted(intervals):
            used += max(0.0, finish - max(start, end if end is not None else start))
            end = finish if end is None else max(end, finish)
        return used

    def mark_ready(self):
        """Freeze the measured boundary, not the still-running operation handles."""
        global _startup_observer
        with self.lock:
            if self.ready_at is None and not self.closed:
                self.ready_at = self.clock()
                if _startup_observer is self:
                    _startup_observer = None

    def finish(self, successful=True):
        global _startup_observer
        with self.lock:
            if self.closed:
                return self.result
            observed_through = self.clock()
            ended = self.ready_at if self.ready_at is not None else observed_through
            if _startup_observer is self:
                _startup_observer = None
            elif _startup_observer is not None:
                self.failed = True
            complete = successful and not self.failed and not any(self.stacks.values())
            spans = [dict(row) for row in self.spans]
            for row in spans:
                row['observed_end'] = row['ended']
                row['continued_after_readiness'] = row['ended'] is not None and row['ended'] > ended
                if row['ended'] is not None:
                    row['ended'] = min(row['ended'], ended)
            for row in spans:
                if row["ended"] is None:
                    complete = False
                    row["status"] = "incomplete"
                    row["inclusive_seconds"] = row["exclusive_seconds"] = None
                    continue
                row["inclusive_seconds"] = row["ended"] - row["started"]
                children = [(max(row["started"], x["started"]), min(row["ended"], x["ended"]))
                    for x in spans if x["parent"] == row["id"] and x["ended"] is not None]
                row["exclusive_seconds"] = max(0.0, row["inclusive_seconds"] - self.union(children))
                if row["started"] < self.started or row["observed_end"] > observed_through or row["inclusive_seconds"] < 0:
                    complete = False
            covered = self.union([(max(self.started, x["started"]), min(ended, x["ended"]))
                for x in spans if x["ended"] is not None])
            self.closed = True
            self.result = dict(schema="analytics-startup-timing.v1", process_instance=self.instance,
                started=self.started, ended=ended, observed_through=observed_through, total_seconds=ended-self.started,
                covered_seconds=covered, unexplained_seconds=max(0.0, ended-self.started-covered),
                complete=bool(complete), spans=spans, counters=dict(self.counters))
            return self.result


from contextlib import contextmanager


@contextmanager
def startup_span(name, **fields):
    observer = _startup_observer
    index = None
    if observer is not None:
        try:
            index = observer.begin(name, fields)
        except Exception:
            observer.failed = True
    ok = False
    try:
        yield
        ok = True
    finally:
        if observer is not None:
            try:
                observer.end(index, ok)
            except Exception:
                observer.failed = True


def startup_count(name, value=1):
    observer = _startup_observer
    if observer is not None:
        try:
            observer.count(name, value)
        except Exception:
            observer.failed = True


def startup_timed(name, counter=None):
    def decorate(operation):
        @wraps(operation)
        def measured(*args, **kwargs):
            if _startup_observer is None:
                return operation(*args, **kwargs)
            if counter:
                startup_count(counter)
            with startup_span(name):
                return operation(*args, **kwargs)
        return measured
    return decorate
