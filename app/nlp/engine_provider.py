"""Explicit process-provider ownership; no provider is installed by importing it."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import threading
import time
from uuid import uuid4

from app.nlp import adapter, v2
from app.nlp.child_client import ChildClient
from app.nlp.child_protocol import OwnedLaunchError
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profiles import ResourcePolicy, validate_resource_policy
from app.nlp.retained_artifacts import retain_data


class ProcessEngineProvider:
    """Launchers supply observed installed identity and exact child-tree joins."""

    def __init__(self, launcher, *, resource_policy, monotonic=time.monotonic):
        if type(resource_policy) is not ResourcePolicy:
            raise NlpBoundaryError('nlp_resource_limit')
        self.launcher = launcher
        self.policy = ResourcePolicy.model_validate_json(resource_policy.model_dump_json())
        self.monotonic = monotonic
        self._condition = threading.Condition()
        self._stopping = threading.Event()
        self._sessions = {}
        self._opening = {}
        self._failed_owners = {}
        self._residency = threading.Lock()

    def current(self, identity):
        return not self._stopping.is_set() and self.launcher.current(identity) is True

    def drained(self, profile):
        digest = profile.identity_digest()
        with self._condition:
            return not self._opening.get(digest, 0) and not any(
                session.profile.identity_digest() == digest for session in self._sessions) and not any(
                retained_digest == digest for retained_digest, _ in self._failed_owners.values())

    @contextmanager
    def open(self, profile, available, check, *, deadline=None):
        deadline = self.monotonic() + 120 if deadline is None else deadline
        digest = profile.identity_digest()

        def admitted():
            if self._stopping.is_set():
                raise NlpBoundaryError('nlp_service_stopping')
            if self.monotonic() >= deadline:
                raise NlpBoundaryError('nlp_deadline')
            check()

        admitted()
        validate_resource_policy(profile, self.policy)
        with self._condition:
            if self._stopping.is_set() or self.monotonic() >= deadline:
                raise NlpBoundaryError('nlp_service_stopping')
            self._opening[digest] = self._opening.get(digest, 0) + 1
        stack, session, completed = ExitStack(), None, False
        try:
            while not self._residency.acquire(timeout=min(.02, max(0, deadline - self.monotonic()))):
                admitted()
            stack.callback(self._residency.release)
            admitted()
            files = stack.enter_context(retain_data(profile, available, admitted))
            admitted()
            nonce = uuid4().hex
            try:
                owner = self.launcher.open(profile, files, nonce, admitted, deadline=deadline)
            except OwnedLaunchError as error:
                if not error.owner.abort(5):
                    with self._condition:
                        self._failed_owners[error.owner] = (digest, stack)
                    stack = None
                raise
            try:
                client = ChildClient(profile, owner, nonce, bind=False, monotonic=self.monotonic)
            except BaseException:
                # The launcher retains failures that have not physically joined.
                if not owner.abort(5):
                    with self._condition:
                        self._failed_owners[owner] = (digest, stack)
                    stack = None
                raise
            session = _Session(profile, client, self.policy)
            with self._condition:
                self._sessions[session] = stack
                stack = None
            client.bind(nonce, timeout=max(0, min(20, deadline - self.monotonic())))
            admitted()
            yield session
            completed = True
        finally:
            with self._condition:
                self._opening[digest] -= 1
                if not self._opening[digest]:
                    self._opening.pop(digest)
                self._condition.notify_all()
            if session is not None:
                drained = self._release(session, 5)
                if not drained or (completed and not session.client.graceful):
                    raise NlpBoundaryError('nlp_worker_not_drained')
            if stack is not None:
                stack.close()

    def _release(self, session, timeout):
        if not session.close(timeout):
            return False
        with self._condition:
            stack = self._sessions.pop(session, None)
            if stack is not None:
                stack.close()
            self._condition.notify_all()
        return True

    def close(self, timeout=5):
        deadline = self.monotonic() + max(0, timeout)
        self._stopping.set()
        with self._condition:
            sessions = tuple(self._sessions)
            failed = tuple(self._failed_owners)
        for session in sessions:
            session.request_stop()
        for session in sessions:
            self._release(session, max(0, deadline - self.monotonic()))
        for owner in failed:
            if owner.abort(max(0, deadline - self.monotonic())):
                with self._condition:
                    _, stack = self._failed_owners.pop(owner)
                    stack.close()
        launcher_drained = self.launcher.close(max(0, deadline - self.monotonic()))
        with self._condition:
            while self._opening and self.monotonic() < deadline:
                self._condition.wait(min(.02, max(0, deadline - self.monotonic())))
            return not self._sessions and not self._failed_owners and not self._opening and launcher_drained


class _Session:
    def __init__(self, profile, client, policy):
        self.profile, self.client, self.policy = profile, client, policy
        self._gate = threading.Lock()

    def request_stop(self):
        self.client.request_stop()

    @contextmanager
    def _operation(self, deadline, seconds):
        deadline = self.client.monotonic() + seconds if deadline is None else deadline
        if not self._gate.acquire(timeout=max(0, deadline - self.client.monotonic())):
            raise NlpBoundaryError('nlp_deadline')
        try:
            yield deadline
        finally:
            self._gate.release()

    def handshake(self, request, *, deadline=None):
        with self._operation(deadline, 20) as deadline:
            return self.client.handshake(request, deadline=deadline)

    def prepare(self, request, *, deadline=None, cancelled=None):
        adapter.validate_prepare_request(self.profile, request.model_dump_json().encode(), resource_policy=self.policy)
        with self._operation(deadline, 120) as deadline:
            return self.client.prepare(request, deadline=deadline, cancelled=cancelled)

    def analyze(self, request, cancelled, *, deadline=None):
        v2.validate_request(self.profile, request.model_dump_json().encode())
        with self._operation(deadline, request.deadline_ms / 1000) as deadline:
            return self.client.analyze(request, cancelled, deadline=deadline)

    def unload(self, request, *, deadline=None):
        with self._operation(deadline, 5) as deadline:
            return self.client.unload(request, deadline=deadline)

    def close(self, timeout=5):
        deadline = self.client.monotonic() + max(0, timeout)
        self.request_stop()
        if not self._gate.acquire(timeout=max(0, deadline - self.client.monotonic())):
            return False
        try:
            return self.client.close(max(0, deadline - self.client.monotonic()))
        finally:
            self._gate.release()
