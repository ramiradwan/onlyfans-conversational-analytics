"""Content-free task invalidations for a desktop helper page, under its own task admission."""
from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse, StreamingResponse

from app.onboarding import timing
from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS
from app.provisioning.events import events

TERMINAL = frozenset({"completed", "cancelled", "expired"})
NOTICES = frozenset({"extension_unavailable", "profile_mismatch", "account_mismatch",
                     "task_changed", "task_expired", "unavailable"})
FOCUS_SECONDS = 2.0


class HelperStreams:
    """Which helper generations hold a stream now, and their pending focus requests."""

    def __init__(self):
        self._live: dict[tuple[str, str], dict] = {}
        self._notices: dict[str, tuple[str, str]] = {}

    def live(self, task_id, generation):
        return (task_id, generation) in self._live

    def _open(self, task_id, generation):
        if (task_id, generation) in self._live or len(self._live) >= 16:
            return None
        entry = {"focus": asyncio.Event(), "ack": None}
        self._live[(task_id, generation)] = entry
        return entry

    def _close(self, task_id, generation, entry):
        if self._live.get((task_id, generation)) is entry:
            del self._live[(task_id, generation)]
            if entry["ack"] is not None and not entry["ack"].done():
                entry["ack"].set_result(False)

    async def focus(self, task_id, generation, *, timeout=None):
        """Ask the live helper to focus itself. True only after it confirms."""
        timeout = FOCUS_SECONDS if timeout is None else timeout
        entry = self._live.get((task_id, generation))
        if entry is None:
            return False
        if entry["ack"] is None or entry["ack"].done():
            entry["ack"] = asyncio.get_running_loop().create_future()
        ack = entry["ack"]
        entry["focus"].set()
        try:
            return await asyncio.wait_for(asyncio.shield(ack), timeout)
        except TimeoutError:
            return False

    def focused(self, task_id, generation):
        entry = self._live.get((task_id, generation))
        if entry is None or entry["ack"] is None or entry["ack"].done():
            return False
        entry["ack"].set_result(True)
        return True

    def notice(self, task_id, generation, reason):
        """Record one display-only notice per generation. It changes no task state."""
        if reason not in NOTICES:
            raise ValueError("Unknown browser notice")
        if task_id in self._notices and self._notices[task_id][0] == generation:
            return False
        if task_id not in self._notices and len(self._notices) >= 64:
            self._notices.pop(next(iter(self._notices)))
        self._notices[task_id] = (generation, reason)
        events.publish()
        return True

    def notices(self, task_ids):
        return [{"task_id": task_id, "reason": self._notices[task_id][1]}
                for task_id in sorted(task_ids) if task_id in self._notices]

    def clear_notice(self, task_id):
        if self._notices.pop(task_id, None) is not None:
            events.publish()


class BrowserTaskEventResponse(StreamingResponse):
    """One stream per task generation. Frames say only that the task's state changed."""

    def __init__(self, coordinator, session, task, streams):
        self.coordinator, self.session, self.task, self.streams = coordinator, session, task, streams
        super().__init__(self.frames(), media_type="text/event-stream", headers=HEADERS | {"X-Accel-Buffering": "no"})
        self.listener = self.entry = None

    async def __call__(self, scope, receive, send):
        self.entry = self.streams._open(self.task["task_id"], self.session.generation)
        if self.entry is None:
            await JSONResponse({"reason": "stream_limit_reached"}, status_code=429, headers=HEADERS)(scope, receive, send)
            return
        self.listener = events.subscribe(self.task["journey_id"])
        try:
            await super().__call__(scope, receive, send)
        finally:
            events.unsubscribe(self.listener)
            self.streams._close(self.task["task_id"], self.session.generation, self.entry)

    def _state(self):
        with self.coordinator.store.database.read() as connection:
            _, task = self.coordinator.admissions._require(connection, self.session.identifier,
                role="browser_task", generation=self.session.generation)
        state = task["state"]
        if state not in TERMINAL and task["expires_at"] <= self.coordinator.admissions._now():
            state = "expired"
        return state, task["expires_at"]

    async def frames(self):
        signal, focus = self.listener[1], self.entry["focus"]
        previous = None
        try:
            while not self.coordinator._closed:
                # Clear before the authoritative read, so a commit during it wakes the next pass.
                signal.clear()
                state, expires_at = self._state()
                if state != previous:
                    previous = state
                    yield "event: browser-task\ndata: " + json.dumps({"task_id": self.task["task_id"],
                        "admission_generation": self.session.generation, "state": state},
                        separators=(",", ":")) + "\n\n"
                    if state in TERMINAL:
                        timing.close(self.task["operation_id"], "helper")
                        return
                if focus.is_set():
                    focus.clear()
                    yield "event: browser-task-focus\ndata: " + json.dumps({"task_id": self.task["task_id"],
                        "admission_generation": self.session.generation}, separators=(",", ":")) + "\n\n"
                remaining = expires_at - self.coordinator.admissions._now()
                if remaining <= 0:
                    continue
                waiters = {asyncio.ensure_future(signal.wait()), asyncio.ensure_future(focus.wait())}
                try:
                    done, _ = await asyncio.wait(waiters, timeout=min(15, remaining),
                                                 return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                if not done:
                    yield ": keepalive\n\n"
        except AdmissionRefused:
            return
