"""Scoped dispatch through an injected authenticated companion transport."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.onboarding import timing
from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, authorize
from app.onboarding.routes import body
from app.onboarding.task_ends import BrowserTaskEnds
from app.onboarding.task_reports import AdmittedAuthenticationTask, AuthenticationTaskReports
from app.persistence.onboarding import require_journey
from app.provisioning.events import events
from app.security.hosted_grants import HostedGrantUnavailable

WAITING_SECONDS = 60
WAITING_LIMIT = 8


class WaitingWindows:
    """Opens the paired extension's waiting window for a freshly accepted sign-in command.

    The window is presentation only. It is ended unless the operation's browser
    task was dispatched to the extension, which then shows the ceremony in it.
    """

    def __init__(self, coordinator, manager):
        self.coordinator, self.manager = coordinator, manager
        self._holds: dict[str, asyncio.Task] = {}
        self._deliveries: dict[str, str] = {}
        self._wakes: dict[str, asyncio.Event] = {}

    def _state(self, operation_id):
        with self.coordinator.store.database.read() as connection:
            row = connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
                                     (operation_id,)).fetchone()
        return None if row is None else row["state"]

    def accepted(self, operation):
        operation_id = operation["operation_id"]
        if self.coordinator._closed or operation_id in self._holds or len(self._holds) >= WAITING_LIMIT:
            return
        scope = json.loads(operation["owner_scope_json"])
        intent = scope.get("authentication_intent")
        if (operation["action"] not in {"account.connect", "auth.select", "auth.switch"} or not isinstance(intent, dict)
                or intent.get("verb") not in {"select", "switch"} or scope.get("browser") is None
                or not isinstance(scope.get("creator_account_id"), str)):
            return
        hold = asyncio.create_task(self._hold(operation_id, scope["creator_account_id"]))
        self._holds[operation_id] = hold

        def released(done):
            if self._holds.get(operation_id) is done:
                self._holds.pop(operation_id, None)
                self._deliveries.pop(operation_id, None)
        hold.add_done_callback(released)

    def delivered(self, operation_id, state):
        """Record how the operation's browser task was delivered: only "dispatched" keeps the window."""
        if operation_id in self._holds:
            self._deliveries[operation_id] = state
            if operation_id in self._wakes:
                self._wakes[operation_id].set()

    async def _hold(self, operation_id, account):
        target = self.manager.browser_task_target(account)
        if target is None:
            return
        started = time.monotonic()
        expires_at = int(time.time()) + WAITING_SECONDS
        if not await self.manager.send_browser_task_waiting(account, target, operation_id, expires_at=expires_at):
            return
        timing.observe("paired", "waiting", "ok", (time.monotonic() - started) * 1000)
        listener = events.subscribe()
        wake = self._wakes[operation_id] = asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAITING_SECONDS
        try:
            while not self.coordinator._closed and operation_id not in self._deliveries:
                listener[1].clear()
                if self._state(operation_id) not in {"accepted", "waiting"}:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                waiters = [asyncio.ensure_future(listener[1].wait()), asyncio.ensure_future(wake.wait())]
                try:
                    await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
        finally:
            events.unsubscribe(listener)
            self._wakes.pop(operation_id, None)
            dispatched = self._deliveries.pop(operation_id, None) == "dispatched"
        if not dispatched:
            # A window already showing the dispatched ceremony is not a waiting window; the extension keeps it.
            await self.manager.send_browser_task_waiting(account, target, operation_id, expires_at=None)


def companion_task_router(coordinator, tasks, ceremonies, *, manager, actor):
    router = APIRouter()
    reports = AuthenticationTaskReports(coordinator, ceremonies)
    waiting = WaitingWindows(coordinator, manager)
    delivered = waiting.delivered
    def current(session, task_id, operator):
        with coordinator.store.database.read() as connection:
            _, parent = coordinator.admissions._require(connection, session.identifier, role="desktop", generation=session.generation)
            task = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (require_journey(task_id),)).fetchone()
            if (task is None or task["journey_id"] != parent["journey_id"] or task["purpose"] != "browser.authentication"
                    or task["expires_at"] <= coordinator.admissions._now() or task["state"] not in {"pending", "accepted", "outcome_unknown"}):
                raise AdmissionRefused("browser_task_unavailable")
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (task["operation_id"],)).fetchone()
            if operation is None or operation["state"] not in {"accepted", "waiting", "outcome_unknown"}:
                raise AdmissionRefused("browser_task_unavailable")
            task, operation = dict(task), dict(operation)
        policy = coordinator.owner.authorize(operation, operator)
        account = json.loads(operation["owner_scope_json"])["creator_account_id"]
        if policy.identity is None or policy.identity.creator_account_id != account:
            raise HostedGrantUnavailable("Operator authority changed")
        return task, account

    async def send(request, session, task_id):
        """Send one authentication task to the account's paired session. Returns the delivery state."""
        operator = actor(request, True)
        operation_id = None
        try:
            task, account = current(session, task_id, operator)
            operation_id = task["operation_id"]
            target = manager.browser_task_target(account)
            if target is None:
                delivered(task["operation_id"], "unreachable")
                return "unreachable"
            if ceremonies is None:
                raise AdmissionRefused("browser_task_unavailable")
            coordinator.admissions.rebind_paired_task(session.identifier, generation=session.generation, task_id=task_id)
            task, _ = current(session, task_id, operator)
            payload = {"profile": "browser-task.v1", "task": tasks.public(task), "extension_id": tasks.extension_id,
                "ceremony": ceremonies.browser_entry(task["operation_id"]), **tasks.attachment(task)}
            if payload["ceremony"] is None or payload["ceremony"]["completion"]["mode"] != "extension-auth-flow":
                raise AdmissionRefused("browser_task_unavailable")

            def still_current():
                latest, selected = current(session, task_id, operator)
                if selected != account or latest["admission_generation"] != task["admission_generation"]:
                    raise AdmissionRefused("browser_task_unavailable")

            async def report(value):
                # Closure withdraws only this exact child through its owner.
                # It cannot borrow the dispatcher's general operator authority.
                if value["state"] not in {"checking", "closed"} or coordinator._closed:
                    return
                def admitted(connection):
                    row = connection.execute("SELECT t.*, p.state AS presentation_state FROM desktop_browser_tasks t "
                        "JOIN desktop_presentations p ON p.generation=t.desktop_generation WHERE t.task_id=?", (task_id,)).fetchone()
                    if (row is None or row["admission_generation"] != task["admission_generation"]
                            or row["presentation_state"] != "active" or row["expires_at"] <= coordinator.admissions._now()
                            or row["state"] not in {"pending", "accepted", "outcome_unknown"}):
                        raise AdmissionRefused("browser_task_unavailable")
                    return row
                handle = AdmittedAuthenticationTask(task_id, task["admission_generation"], admitted)
                try:
                    reports.report(handle, state=value["state"], notification=value.get("notification"))
                except (AdmissionRefused, HostedGrantUnavailable, ValueError):
                    return

            ends.record(task, manager.browser_task_pairing(account, target))
            state = await manager.send_browser_task(account, target, payload, authorize=still_current, receive=report)
            delivered(task["operation_id"], state)
            return state
        except (AdmissionRefused, HostedGrantUnavailable, ValueError):
            if operation_id is not None:
                delivered(operation_id, "unavailable")
            raise HTTPException(409, "Browser task changed") from None

    tasks.dispatcher = send
    coordinator.accepted = waiting.accepted
    router.waiting = waiting
    ends = BrowserTaskEnds(coordinator, manager)
    manager.browser_task_advertised = ends.replay

    @router.post("/api/v1/desktop/browser-tasks/{task_id}/dispatch")
    async def dispatch(request: Request, task_id: str):
        session = authorize(coordinator.admissions, request, role="desktop", mutation=True)
        await body(request, empty=True)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        return JSONResponse({"state": await send(request, session, task_id)}, headers=HEADERS)

    router.task_ends = ends
    return router
