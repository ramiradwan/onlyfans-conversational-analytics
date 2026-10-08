"""Bounded local notifications published after owner commits, without status polling."""
from __future__ import annotations

import asyncio
import json
from threading import RLock
from uuid import uuid4

from app.persistence.catchup_events import subscribe_authority_changes

CAPABILITIES = "local-onboarding.v1,persistent-workspace.v1,first-enrollment-session.v1"


class OnboardingEvents:
    def __init__(self) -> None:
        self.epoch = str(uuid4())
        self._lock = RLock()
        self._listeners = set()
        self._states = {}
        self._commit_serial = 0
        self._focus = {}
        subscribe_authority_changes(self._authority_committed)

    def _authority_committed(self, path, committed_at) -> None:
        del path, committed_at
        self.publish()

    def publish(self) -> None:
        with self._lock:
            self._commit_serial += 1
            listeners = tuple(self._listeners)
        for loop, signal, _ in listeners:
            if not loop.is_closed():
                loop.call_soon_threadsafe(signal.set)

    def subscribe(self, journey_id=None):
        listener = (asyncio.get_running_loop(), asyncio.Event(), journey_id)
        with self._lock:
            self._listeners.add(listener)
        return listener

    def request_focus(self, journey_id: str | None = None) -> str | None:
        """Signal only a live authenticated workspace; never infer a hosted tab."""
        with self._lock:
            candidates = {item[2] for item in self._listeners if item[2] is not None}
            if journey_id is None and len(candidates) == 1:
                journey_id = next(iter(candidates))
            if journey_id is None or journey_id not in candidates:
                return None
            self._focus[journey_id] = self._focus.get(journey_id, 0) + 1
            listeners = tuple(item for item in self._listeners if item[2] == journey_id)
        for loop, signal, _ in listeners:
            if not loop.is_closed():
                loop.call_soon_threadsafe(signal.set)
        return journey_id

    def unsubscribe(self, listener) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def snapshot(self, *, journey_id: str, facts: dict, reason: str = "none",
                 pending_operation=None, account_generation: int = 0, consent_generation: int = 0) -> dict:
        content = {"facts": facts, "reason": reason, "pending_operation": pending_operation,
                   "account_generation": account_generation, "consent_generation": consent_generation}
        with self._lock:
            previous = self._states.get(journey_id)
            if previous is None and len(self._states) >= 128:
                raise ValueError("Too many active onboarding views")
            revision = 0 if previous is None else previous[0] + (previous[1] != content or previous[2] != self._commit_serial)
            self._states[journey_id] = (revision, content, self._commit_serial)
        return {"profile": "local-onboarding-state.v1", "source": "brain", "kind": "snapshot",
                "journey_id": journey_id, "epoch": self.epoch, "revision": revision, **content}

    async def stream(self, read_snapshot, authorize, *, journey_id=None, expires_in=None):
        # Subscribe first; a commit during snapshot reads keeps the signal set.
        listener = self.subscribe(journey_id)
        signal = listener[1]
        last = None
        focused = self._focus.get(journey_id, 0)
        try:
            while True:
                # Drain only before reading. A commit or focus request while a
                # yielded frame is being sent must survive to the next read.
                signal.clear()
                authorize()
                requested = self._focus.get(journey_id, 0)
                if requested != focused:
                    yield "event: workspace-focus\ndata: " + json.dumps({"journey_id": journey_id}, separators=(",", ":")) + "\n\n"
                    focused = requested
                value = read_snapshot()
                expiry_delay = None if expires_in is None else expires_in()
                deadline = None if expiry_delay is None else asyncio.get_running_loop().time() + max(0, expiry_delay)
                if last != value["revision"]:
                    if last is not None:
                        value = {**value, "kind": "event"}
                    yield "event: onboarding\ndata: " + json.dumps(value, separators=(",", ":")) + "\n\n"
                    last = value["revision"]
                while not signal.is_set():
                    remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
                    if remaining is not None and remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(signal.wait(), 15 if remaining is None else min(15, remaining))
                    except TimeoutError:
                        authorize()
                        yield ": keepalive\n\n"
        finally:
            self.unsubscribe(listener)


events = OnboardingEvents()
