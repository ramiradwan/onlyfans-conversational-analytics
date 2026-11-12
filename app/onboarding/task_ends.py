"""Push the end of a dispatched sign-in task to its pairing's advertised companion sessions."""
from __future__ import annotations

import asyncio

from app.onboarding import timing
from app.provisioning.events import events

LIVE = ("pending", "accepted", "outcome_unknown")
REPLAY_LIMIT = 8
_ENDED = ("SELECT d.task_id,d.admission_generation,d.pairing_id,t.operation_id FROM desktop_browser_task_dispatches d "
          "JOIN desktop_browser_tasks t ON t.task_id=d.task_id "
          "WHERE t.purpose='browser.authentication' AND t.expires_at>? "
          "AND (t.state NOT IN ('pending','accepted','outcome_unknown') OR t.admission_generation<>d.admission_generation)")


class BrowserTaskEnds:
    """The control carries only the task and its generation. It settles nothing."""

    def __init__(self, coordinator, manager):
        self.coordinator, self.manager = coordinator, manager
        self.store = coordinator.store
        self._sent: set[tuple[str, str]] = set()
        self._watcher: asyncio.Task | None = None

    def record(self, task, pairing_id):
        """Remember which pairing a task generation was sent to, so a reconnect can replay its end."""
        if pairing_id is None:
            return
        with self.store.database.transaction() as connection:
            connection.execute("INSERT INTO desktop_browser_task_dispatches VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE "
                "SET admission_generation=excluded.admission_generation,pairing_id=excluded.pairing_id",
                (task["task_id"], task["admission_generation"], pairing_id))
        self._ensure_watching()

    def _ended(self, pairing_id=None):
        now = self.coordinator.admissions._now()
        with self.store.database.read() as connection:
            if pairing_id is None:
                rows = connection.execute(_ENDED, (now,)).fetchall()
            else:
                rows = connection.execute(_ENDED + " AND d.pairing_id=? ORDER BY t.expires_at DESC LIMIT ?",
                                          (now, pairing_id, REPLAY_LIMIT)).fetchall()
        return [dict(row) for row in rows]

    async def _send(self, sessions, row):
        delivered = False
        for session in sessions:
            delivered = await self.manager.send_browser_task_end(
                session, row["task_id"], row["admission_generation"]) or delivered
        return delivered

    async def flush(self):
        """Send each newly ended dispatched task's end once to its pairing's advertised sessions."""
        rows = self._ended()
        current = {(row["task_id"], row["admission_generation"]) for row in rows}
        self._sent &= current
        for row in rows:
            key = (row["task_id"], row["admission_generation"])
            if key in self._sent:
                continue
            self._sent.add(key)
            if await self._send(self.manager.browser_sessions_for_pairing(row["pairing_id"]), row):
                timing.close(row["operation_id"], "paired")

    async def replay(self, session):
        """On advertisement, resend ends of this pairing's unexpired ended tasks to that session only."""
        for row in self._ended(session.pairing_id):
            await self._send([session], row)
        self._ensure_watching()

    def _ensure_watching(self):
        if self._watcher is None or self._watcher.done():
            try:
                self._watcher = asyncio.get_running_loop().create_task(self._watch())
            except RuntimeError:
                self._watcher = None

    async def _watch(self):
        listener = events.subscribe()
        signal = listener[1]
        try:
            while not self.coordinator._closed:
                signal.clear()
                await self.flush()
                try:
                    await asyncio.wait_for(signal.wait(), 15)
                except TimeoutError:
                    pass
        finally:
            events.unsubscribe(listener)

    async def stop(self):
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
