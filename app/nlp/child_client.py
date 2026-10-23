"""Single-flight calls over an explicitly owned private worker channel."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import queue
import threading
import time
from uuid import uuid4

from app.nlp import adapter, v2
from app.nlp.child_protocol import (BootstrapRequest, BootstrapResult, CloseRequest, CloseResult,
    TransportFailure, decode_envelope, encode_envelope, read_frame, write_frame)
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profiles import canonical_bytes


@dataclass
class _Pending:
    validate: object
    event: threading.Event = field(default_factory=threading.Event)
    body: bytes | None = None
    failed: bool = False


class ChildClient:
    """The channel owner must join its process tree before releasing its files."""

    def __init__(self, profile, owner, nonce, *, timeout=20, monotonic=time.monotonic, bind=True):
        self.profile, self.owner, self.monotonic = profile, owner, monotonic
        self._condition = threading.Condition()
        self._pending = {}
        self._writes = queue.Queue(maxsize=2)
        self._failed = False
        self._closing = False
        self._stopping = threading.Event()
        self._close_lock = threading.Lock()
        self._drained = False
        self.graceful = False
        self._reader = threading.Thread(target=self._read, name='nlp-reader', daemon=False)
        self._writer = threading.Thread(target=self._write, name='nlp-writer', daemon=False)
        self._reader.start()
        self._writer.start()
        if bind:
            self.bind(nonce, timeout=timeout)

    def bind(self, nonce, *, timeout=20):
        try:
            request = BootstrapRequest(channel_nonce=nonce, profile_json=canonical_bytes(self.profile).decode('ascii'),
                artifact_ids=tuple(a.artifact_id for a in self.profile.artifacts))

            def validate(body):
                result = v2._decode(BootstrapResult, body, 'bootstrap')
                if (result.channel_nonce != nonce or result.profile_sha256 != self.profile.identity_digest()
                        or result.outcome != 'bound'):
                    raise NlpBoundaryError('nlp_bootstrap_mismatch')

            self._rpc('bootstrap', request, validate, self.monotonic() + timeout)
        except BaseException:
            self._fail()
            self.close(timeout=5)
            raise

    @property
    def healthy(self):
        with self._condition:
            return not self._failed and not self._closing and not self._stopping.is_set()

    def request_stop(self):
        self._stopping.set()

    def _fail(self):
        with self._condition:
            self._failed = True
            for pending in self._pending.values():
                pending.failed = True
                pending.event.set()
            self._condition.notify_all()

    def _read(self):
        try:
            while True:
                operation, body = decode_envelope(read_frame(self.owner.stdout))
                if operation == 'transport_failure':
                    v2._decode(TransportFailure, body, 'transport_failure')
                    raise NlpBoundaryError('nlp_transport_failure')
                # Typed validation below rejects coercions, duplicate and extra members.
                payload = json.loads(body)
                key = (operation, None if operation == 'bootstrap' else payload.get('request_id'))
                with self._condition:
                    pending = self._pending.get(key)
                    if pending is None or pending.event.is_set():
                        raise NlpBoundaryError('nlp_unsolicited_response')
                    pending.validate(body)
                    self._pending.pop(key)
                    pending.body = body
                    pending.event.set()
                    self._condition.notify_all()
        except EOFError:
            with self._condition:
                expected_eof = self._closing and not self._pending
            if not expected_eof:
                self._fail()
        except Exception:
            self._fail()

    def _write(self):
        try:
            while True:
                body = self._writes.get()
                if body is None:
                    return
                write_frame(self.owner.stdin, body)
        except Exception:
            self._fail()

    def _submit(self, operation, request, validate, *, closing=False):
        body = encode_envelope(operation, request)
        key = (operation, None if operation == 'bootstrap' else request.request_id)
        with self._condition:
            if self._failed or (self._closing and not closing):
                raise NlpBoundaryError('nlp_channel_closed')
            active = {op for op, _ in self._pending}
            if self._pending and not (operation == 'cancel' and active in ({'analyze'}, {'prepare'})):
                raise NlpBoundaryError('nlp_channel_busy')
            pending = _Pending(validate)
            self._pending[key] = pending
            try:
                self._writes.put_nowait(body)
            except queue.Full:
                self._pending.pop(key)
                self._fail()
                raise NlpBoundaryError('nlp_channel_busy') from None
            return pending

    def _rpc(self, operation, request, validate, deadline, *, cancelled=None, closing=False):
        if self.monotonic() >= deadline:
            raise NlpBoundaryError('nlp_deadline')
        if not closing and (self._stopping.is_set() or (cancelled is not None and cancelled())):
            raise NlpBoundaryError('nlp_cancelled')
        pending = self._submit(operation, request, validate, closing=closing)
        stop_reason = None
        settlement = deadline
        while not pending.event.wait(min(.02, max(0, settlement - self.monotonic()))):
            if stop_reason is None:
                try:
                    stop = not closing and (self._stopping.is_set() or (cancelled is not None and cancelled()))
                except Exception:
                    stop = True
                expired = self.monotonic() >= deadline
                if stop or expired:
                    stop_reason = 'nlp_deadline' if expired else 'nlp_cancelled'
                    settlement = self.monotonic() + 2
                    if operation in ('prepare', 'analyze'):
                        cancel = adapter.CancelRequest(request_id=uuid4().hex,
                            target_request_id=request.request_id, profile_sha256=request.profile_sha256,
                            component_sha256=request.component_sha256)
                        try:
                            self._submit('cancel', cancel, lambda b: adapter.validate_lifecycle(cancel, b), closing=closing)
                        except ValueError:
                            self._fail()
                    else:
                        self._fail()
            if self.monotonic() >= settlement:
                self._fail()
                break
        if stop_reason is not None:
            raise NlpBoundaryError(stop_reason)
        if pending.failed or pending.body is None:
            raise NlpBoundaryError('nlp_transport_failure')
        if self.monotonic() >= deadline:
            raise NlpBoundaryError('nlp_deadline')
        if cancelled is not None:
            try:
                stop = self._stopping.is_set() or cancelled()
            except Exception:
                stop = True
            if stop:
                raise NlpBoundaryError('nlp_cancelled')
        return pending.body

    def handshake(self, request, *, deadline=None):
        return self._rpc('handshake', request,
            lambda b: adapter.validate_handshake(self.profile, request, b),
            deadline if deadline is not None else self.monotonic() + 20)

    def prepare(self, request, *, deadline=None, cancelled=None):
        return self._rpc('prepare', request, lambda b: adapter.validate_preparation(request, b),
            deadline if deadline is not None else self.monotonic() + 120, cancelled=cancelled)

    def analyze(self, request, cancelled, *, deadline=None):
        return self._rpc('analyze', request, lambda b: v2.validate_response(self.profile, request, b),
            deadline if deadline is not None else self.monotonic() + request.deadline_ms / 1000,
            cancelled=cancelled)

    def unload(self, request, *, deadline=None):
        return self._rpc('unload', request, lambda b: adapter.validate_lifecycle(request, b),
            deadline if deadline is not None else self.monotonic() + 5)

    def close(self, timeout=5):
        deadline = self.monotonic() + max(0, timeout)
        if not self._close_lock.acquire(timeout=max(0, deadline - self.monotonic())):
            return False
        try:
            if self._drained:
                return True
            self._stopping.set()
            with self._condition:
                self._closing = True
                while self._pending and not self._failed and self.monotonic() < deadline:
                    self._condition.wait(min(.02, max(0, deadline - self.monotonic())))
            graceful = False
            if not self._failed and not self._pending and self.monotonic() < deadline:
                request = CloseRequest(request_id=uuid4().hex)
                try:
                    body = self._rpc('close', request,
                        lambda b: v2._decode(CloseResult, b, 'close'), deadline, closing=True)
                    result = v2._decode(CloseResult, body, 'close')
                    graceful = result.request_id == request.request_id and result.outcome == 'drained'
                except ValueError:
                    self._fail()
            remaining = max(0, deadline - self.monotonic())
            physical = self.owner.close(remaining) if graceful else self.owner.abort(remaining)
            try:
                self._writes.put_nowait(None)
            except queue.Full:
                self._fail()
            for thread in (self._reader, self._writer):
                if thread is not threading.current_thread():
                    thread.join(max(0, deadline - self.monotonic()))
            self.graceful = graceful and physical and self.owner.graceful and not self._failed
            self._drained = physical and not self._reader.is_alive() and not self._writer.is_alive()
            return self._drained
        finally:
            self._close_lock.release()
