"""Scoped browser task observations; no browser operator or desktop rights."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, authorize
from app.onboarding.routes import body
from app.onboarding.task_reports import AdmittedAuthenticationTask, AuthenticationTaskReports
from app.security.companion_pairing import CompanionPairingError
from app.security.hosted_grants import HostedGrantUnavailable


def browser_router(coordinator, tasks, ceremonies, *, pairing=None):
    router = APIRouter()
    reports = AuthenticationTaskReports(coordinator, ceremonies)

    def authentication_task(session):
        def admitted(connection):
            _, task = coordinator.admissions._require(connection, session.identifier,
                role="browser_task", generation=session.generation)
            return task
        return AdmittedAuthenticationTask(session.task_id, session.generation, admitted)

    def task_session(request, *, mutation=False):
        if request.url.query:
            raise HTTPException(400, "Invalid browser task request")
        session = authorize(coordinator.admissions, request, role="browser_task", mutation=mutation)
        try:
            return session, coordinator.admissions.task(session.identifier, generation=session.generation)
        except AdmissionRefused:
            raise HTTPException(401, "Browser task admission is unavailable") from None

    @router.get("/api/v1/desktop/browser-tasks")
    def pending(request: Request):
        if request.url.query:
            raise HTTPException(400, "Invalid browser task request")
        session = authorize(coordinator.admissions, request, role="desktop")
        return JSONResponse({"tasks": tasks.current_native(session)}, headers=HEADERS)

    @router.get("/api/v1/browser-tasks/payload")
    def payload(request: Request):
        _, task = task_session(request)
        entry = (ceremonies.browser_entry(task["operation_id"]) if ceremonies is not None
            and task["purpose"] == "browser.authentication" and task["state"] != "completed" else None)
        return JSONResponse({"profile": "browser-task.v1", "task": task,
            "extension_id": tasks.extension_id, "ceremony": entry, **tasks.attachment(task)}, headers=HEADERS)

    @router.post("/api/v1/browser-tasks/observation")
    async def observe(request: Request):
        session, _ = task_session(request, mutation=True)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        value = await body(request)
        try:
            tasks.observe(session, value)
        except AdmissionRefused:
            raise HTTPException(409, "Browser task changed") from None
        # Reconcile this operation by reading domain state. An observation never
        # authorizes another operation or calls a consent/permission controller.
        with coordinator.store.database.read() as connection:
            operation = dict(connection.execute("SELECT o.* FROM desktop_setup_operations o "
                "JOIN desktop_browser_tasks t ON t.operation_id=o.operation_id WHERE t.task_id=?", (session.task_id,)).fetchone())
        if operation["state"] in {"accepted", "waiting", "outcome_unknown"}:
            coordinator._start(operation, reconcile=True, actor=None)
        return JSONResponse({"state": "observed"}, headers=HEADERS)

    @router.post("/api/v1/browser-tasks/notification")
    async def notification(request: Request):
        session, _ = task_session(request, mutation=True)
        value = await body(request)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        try:
            reports.report(authentication_task(session), state="checking", notification=value)
        except (AdmissionRefused, HostedGrantUnavailable, ValueError):
            raise HTTPException(409, "Browser task changed") from None
        return JSONResponse({"state": "checking"}, status_code=202, headers=HEADERS)

    @router.post("/api/v1/browser-tasks/closed")
    async def closed(request: Request):
        session, _ = task_session(request, mutation=True)
        await body(request, empty=True)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        try:
            reports.report(authentication_task(session), state="closed")
        except (AdmissionRefused, HostedGrantUnavailable, ValueError):
            raise HTTPException(409, "Browser task changed") from None
        return JSONResponse({"state": "checking"}, status_code=202, headers=HEADERS)

    @router.post("/api/v1/browser-tasks/pairing")
    async def compare_pairing(request: Request):
        session, task = task_session(request, mutation=True)
        value = await body(request)
        if coordinator._closed:
            raise HTTPException(503, "Local setup is stopping")
        if pairing is None or task["purpose"] != "browser.pairing-confirmation":
            raise HTTPException(409, "Browser task changed")
        with coordinator.store.database.read() as connection:
            operation = dict(connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                (task["operation_id"],)).fetchone())
        try:
            await pairing.compare(session, operation, value)
        except (AdmissionRefused, CompanionPairingError, HostedGrantUnavailable):
            # A mismatch may have committed a decline. Observe that exact attempt
            # even when comparison's reply is refused; never begin a replacement.
            coordinator._start(operation, reconcile=True, actor=None)
            raise HTTPException(409, "Pairing could not be confirmed") from None
        coordinator._start(operation, reconcile=True, actor=None)
        return JSONResponse({"state": "checking"}, status_code=202, headers=HEADERS)

    return router
