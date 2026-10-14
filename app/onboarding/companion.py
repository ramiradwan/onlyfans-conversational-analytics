"""Scoped dispatch through an injected authenticated companion transport."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, authorize
from app.onboarding.routes import body
from app.onboarding.task_reports import AdmittedAuthenticationTask, AuthenticationTaskReports
from app.persistence.onboarding import require_journey
from app.security.hosted_grants import HostedGrantUnavailable


def companion_task_router(coordinator, tasks, ceremonies, *, manager, actor):
    router = APIRouter()
    reports = AuthenticationTaskReports(coordinator, ceremonies)

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

    @router.post("/api/v1/desktop/browser-tasks/{task_id}/dispatch")
    async def dispatch(request: Request, task_id: str):
        session = authorize(coordinator.admissions, request, role="desktop", mutation=True)
        await body(request, empty=True)
        operator = actor(request, True)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        try:
            task, account = current(session, task_id, operator)
            target = manager.browser_task_target(account)
            if target is None:
                return JSONResponse({"state": "unreachable"}, headers=HEADERS)
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

            state = await manager.send_browser_task(account, target, payload, authorize=still_current, receive=report)
            return JSONResponse({"state": state}, headers=HEADERS)
        except (AdmissionRefused, HostedGrantUnavailable, ValueError):
            raise HTTPException(409, "Browser task changed") from None

    return router
