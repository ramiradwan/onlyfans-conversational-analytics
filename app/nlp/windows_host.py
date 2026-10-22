"""Private installed-launcher handles for an explicitly enabled Engine provider."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

from app.core.desktop_release import current_desktop_release
from app.nlp.child_protocol import (ArtifactHandle, HostControl, HostLaunch, HostObservation,
    HostStatus, OwnedLaunchError, payload_bytes, read_frame, write_frame)
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profiles import canonical_bytes
from app.nlp.v2 import _decode

OUTER_JOB_HANDLE_ENV = 'BRAIN_OUTER_JOB_QUERY_HANDLE'


class NativeHostLauncher:
    """Observation is explicit; current() never starts a process or reads a model."""

    def __init__(self, *, monotonic=time.monotonic):
        if os.name != 'nt' or not getattr(sys, 'frozen', False) or Path(sys.executable).name.lower() != 'brain.exe':
            raise NlpBoundaryError('nlp_host_unavailable')
        release = current_desktop_release()
        if release is None or release.update_owner != 'direct-appinstaller':
            raise NlpBoundaryError('nlp_host_unavailable')
        self.image = Path(sys.executable).parent / 'Bridge.exe'
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._owners = set()
        self._observed = None
        self._stopping = False

    def current(self, identity):
        with self._lock:
            return not self._stopping and self._observed == identity

    def observe(self, identity, *, timeout=20, check=lambda: None):
        """Inspect signed installed code without creating an inference worker."""
        nonce = uuid4().hex
        owner = self._spawn(nonce, (), check)
        deadline = self.monotonic() + timeout
        try:
            owner.send(HostObservation(channel_nonce=nonce, engine=identity), deadline, check)
            owner.wait_for('ready', deadline, check)
            if not owner.close(max(0, deadline - self.monotonic())) or not owner.graceful:
                raise NlpBoundaryError('nlp_host_unavailable')
            with self._lock:
                if self._stopping:
                    raise NlpBoundaryError('nlp_host_unavailable')
                self._observed = identity
        except BaseException:
            owner.abort(max(0, deadline - self.monotonic()))
            with self._lock:
                self._observed = None
            raise OwnedLaunchError(owner) from None

    def open(self, profile, files, nonce, check, *, deadline):
        if not self.current(profile.components[0].engine):
            raise NlpBoundaryError('nlp_engine_unavailable')
        owner = self._spawn(nonce, files.artifacts, check)
        launch_deadline = min(deadline, self.monotonic() + 20)
        try:
            owner.send(HostLaunch(channel_nonce=nonce, profile_json=canonical_bytes(profile).decode('ascii'),
                artifacts=owner.bindings), launch_deadline, check)
            owner.wait_for('ready', launch_deadline, check)
            owner.send(HostControl(channel_nonce=nonce, operation='commit'), launch_deadline, check)
            owner.wait_for('running', launch_deadline, check)
            return owner
        except BaseException:
            with self._lock:
                self._observed = None
            raise OwnedLaunchError(owner) from None

    def _spawn(self, nonce, artifacts, check):
        import ctypes
        import msvcrt
        from ctypes import wintypes

        check()
        with self._lock:
            if self._stopping:
                raise NlpBoundaryError('nlp_host_unavailable')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
        kernel.SetHandleInformation.restype = wintypes.BOOL
        process = kernel.GetCurrentProcess()
        owned, children = ExitStack(), ExitStack()

        def duplicate(handle, rights=None):
            value = wintypes.HANDLE()
            if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(value),
                    0 if rights is None else rights, True, 2 if rights is None else 0):
                raise NlpBoundaryError('nlp_host_handles')
            children.callback(kernel.CloseHandle, value)
            return value.value

        def pipe(parent_reads):
            read, write = os.pipe()
            parent, child = (read, write) if parent_reads else (write, read)
            children.callback(os.close, child)
            try:
                stream = os.fdopen(parent, 'rb' if parent_reads else 'wb', buffering=0)
            except BaseException:
                os.close(parent)
                raise
            owned.enter_context(stream)
            return stream, duplicate(msvcrt.get_osfhandle(child))

        try:
            command, command_read = pipe(False)
            status, status_write = pipe(True)
            stdin, stdin_read = pipe(False)
            stdout, stdout_write = pipe(True)
            lifetime, lifetime_read = pipe(False)
            parent = duplicate(process, 0x00101000)  # Query limited information and synchronize.
            locator = os.environ.get(OUTER_JOB_HANDLE_ENV, '')
            if re.fullmatch(r'[1-9][0-9]{0,19}', locator) is None or int(locator) >= 2**64 - 1:
                raise NlpBoundaryError('nlp_host_handles')
            job = int(locator)
            if not kernel.SetHandleInformation(job, 1, 0):
                raise NlpBoundaryError('nlp_host_handles')
            outer = duplicate(job, 4)  # JOB_OBJECT_QUERY only.
            bindings = tuple(ArtifactHandle(artifact=item.artifact, handle=duplicate(item.handle)) for item in artifacts)
            handles = [parent, outer, command_read, status_write, stdin_read, stdout_write, lifetime_read]
            inherited = handles + [item.handle for item in bindings]
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': inherited}
            system = os.environ.get('SystemRoot')
            if not system:
                raise NlpBoundaryError('nlp_host_unavailable')
            environment = {'SystemRoot': system, 'WINDIR': system, 'PATH': str(Path(system) / 'System32')}
            owner = _HostOwner(nonce, owned, command, status, stdin, stdout, lifetime,
                bindings, self.monotonic, self._finished)
            with self._lock:
                if self._stopping:
                    raise NlpBoundaryError('nlp_host_unavailable')
                self._owners.add(owner)
            try:
                owner.process = subprocess.Popen([str(self.image), '--nlp-worker-host',
                    *(str(value) for value in handles), nonce], executable=str(self.image),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, startupinfo=startup, creationflags=0x08000000, env=environment)
                owner.start()
            except BaseException:
                if owner.process is None:
                    self._finished(owner)
                    owned.close()
                else:
                    if not owner.abort(5):
                        raise OwnedLaunchError(owner) from None
                raise
            children.close()
            return owner
        except BaseException:
            children.close()
            # A started owner's resources remain with that owner until joined.
            if 'owner' not in locals() or owner.process is None:
                owned.close()
            raise

    def _finished(self, owner):
        with self._lock:
            self._owners.discard(owner)

    def close(self, timeout=5):
        deadline = self.monotonic() + max(0, timeout)
        with self._lock:
            self._stopping = True
            self._observed = None
            owners = tuple(self._owners)
        for owner in owners:
            owner.abort(max(0, deadline - self.monotonic()))
        with self._lock:
            return not self._owners


class _HostOwner:
    def __init__(self, nonce, resources, command, status, stdin, stdout, lifetime,
                 bindings, monotonic, finished):
        self.nonce, self.resources, self.command, self.status = nonce, resources, command, status
        self.stdin, self.stdout, self.lifetime, self.bindings = stdin, stdout, lifetime, bindings
        self.monotonic, self.finished = monotonic, finished
        self.process = None
        self._statuses = queue.Queue(maxsize=4)
        self._terminal = None
        self._failure = False
        self._terminal_event = threading.Event()
        self._lock = threading.Lock()
        self._writers = []
        self._reader = threading.Thread(target=self._read, name='nlp-host-status', daemon=False)
        self._closed = False
        self.graceful = False

    def start(self):
        self._reader.start()

    def _read(self):
        state, worker = None, None
        try:
            while True:
                status = _decode(HostStatus, read_frame(self.status), 'host_status')
                if status.channel_nonce != self.nonce or state in ('drained', 'failed'):
                    raise NlpBoundaryError('nlp_host_status')
                allowed = {None: {'ready', 'failed'}, 'ready': {'running', 'drained', 'failed'},
                           'running': {'drained', 'failed'}}
                if status.state not in allowed[state] or (worker is not None and status.worker_pid != worker):
                    raise NlpBoundaryError('nlp_host_status')
                state = status.state
                if state == 'running':
                    worker = status.worker_pid
                if state in ('drained', 'failed'):
                    self._terminal = status
                    self._terminal_event.set()
                self._statuses.put_nowait(status)
        except EOFError:
            if self._terminal is None:
                self._failure = True
                self._terminal_event.set()
        except Exception:
            self._failure = True
            self._terminal_event.set()

    def send(self, value, deadline, check):
        body, event, failure = payload_bytes(value), threading.Event(), []

        def write():
            try:
                write_frame(self.command, body)
            except Exception:
                failure.append(True)
            finally:
                event.set()

        thread = threading.Thread(target=write, name='nlp-host-command', daemon=False)
        self._writers.append(thread)
        thread.start()
        while not event.wait(.02):
            check()
            if self.monotonic() >= deadline:
                raise NlpBoundaryError('nlp_host_deadline')
        thread.join()
        if failure:
            raise NlpBoundaryError('nlp_host_io')
        check()
        if self.monotonic() >= deadline:
            raise NlpBoundaryError('nlp_host_deadline')

    def wait_for(self, state, deadline, check):
        while self.monotonic() < deadline:
            check()
            if self._failure:
                raise NlpBoundaryError('nlp_host_status')
            try:
                status = self._statuses.get(timeout=min(.02, max(0, deadline - self.monotonic())))
            except queue.Empty:
                continue
            if status.state != state:
                raise NlpBoundaryError('nlp_host_status')
            return status
        raise NlpBoundaryError('nlp_host_deadline')

    def close(self, timeout=5):
        return self._finish(timeout, force=False)

    def abort(self, timeout=5):
        return self._finish(timeout, force=True)

    def _finish(self, timeout, *, force):
        deadline = self.monotonic() + max(0, timeout)
        if not self._lock.acquire(timeout=max(0, deadline - self.monotonic())):
            return False
        try:
            if self._closed:
                return True
            if force:
                self.lifetime.close()
            elif self._terminal is None:
                try:
                    self.send(HostControl(channel_nonce=self.nonce, operation='close'), deadline, lambda: None)
                except ValueError:
                    self.lifetime.close()
            self._terminal_event.wait(max(0, deadline - self.monotonic()))
            try:
                self.process.wait(timeout=max(0, deadline - self.monotonic()))
            except subprocess.TimeoutExpired:
                return False
            for thread in (self._reader, *self._writers):
                if thread.ident is not None:
                    thread.join(max(0, deadline - self.monotonic()))
            joined = not self._reader.is_alive() and not any(t.is_alive() for t in self._writers)
            if (not joined or self._failure or self._terminal is None or not self._terminal.tree_drained):
                return False
            self.graceful = not force and self._terminal.state == 'drained' and self.process.returncode == 0
            self.resources.close()
            self._closed = True
            self.finished(self)
            return True
        finally:
            self._lock.release()
