"""A single finite browser.access exchange owned by the admitted Chrome document."""
import asyncio
import hashlib
import json
import re
import secrets

from fastapi import HTTPException, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import HEADERS, HOST, ORIGIN, _browser
from app.onboarding.surface import setup_shell
from app.provisioning.events import events


class BrowserAccessDocuments:
    def __init__(self, coordinator, tasks, observe):
        self.coordinator = coordinator
        self.tasks = tasks
        self.observe = observe
        self.active = 0
        self.tickets = {}

    async def page(self, request):
        _browser(request, mutation=True)
        if (request.url.query or not request.client or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.getlist("sec-fetch-site") != ["same-origin"]
                or request.headers.getlist("sec-fetch-mode") != ["navigate"]
                or request.headers.getlist("sec-fetch-dest") != ["document"]
                or request.headers.getlist("content-type") != ["application/x-www-form-urlencoded"]):
            raise HTTPException(403, "Open the browser from the desktop app")
        data = bytearray()
        async for part in request.stream():
            if len(data) + len(part) > 128:
                raise HTTPException(400, "Invalid browser entry")
            data.extend(part)
        fields = re.fullmatch(rb"code=([A-Za-z0-9_-]{43})&presentation=([0-9a-f-]{36})", data)
        if fields is None:
            raise HTTPException(400, "Invalid browser entry")
        admissions = self.coordinator.admissions
        try:
            session = await run_in_threadpool(admissions.consume, fields[1].decode(), role="browser_task", access_document=True)
            if session.generation != fields[2].decode():
                raise AdmissionRefused("task_changed")
        except AdmissionRefused:
            return HTMLResponse("This setup link has expired or was already used. Return to the desktop app to continue.",
                status_code=401, headers=HEADERS | {"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
        now = admissions._now()
        self.tickets = {key: binding for key, binding in self.tickets.items() if binding[0] > now}
        if self.coordinator._closed or len(self.tickets) >= 16:
            raise HTTPException(503, "Browser setup is unavailable")
        # The first-party POST commits Chrome's final sender URL. Only its response
        # can bind a socket. The extension never receives the native entry or ticket.
        ticket = secrets.token_urlsafe(32)
        response = setup_shell(request, browser=True, access_ticket=ticket)
        self.tickets[hashlib.sha256(ticket.encode()).digest()] = (min(now + 15, session.expires_at), session)
        return response

    async def open(self, socket):
        if (self.active >= 16 or socket.url.query or not socket.client
                or socket.client.host not in {"127.0.0.1", "::1"}
                or socket.headers.getlist("host") != [HOST] or socket.headers.getlist("origin") != [ORIGIN]
                or socket.headers.getlist("sec-websocket-protocol") != ["ofca.browser-access.v1"]):
            await socket.close(1008)
            return
        self.active += 1
        try:
            await socket.accept(subprotocol="ofca.browser-access.v1")
            # The server never returns the identifier. A replaced document needs a new desktop entry.
            ticket = await asyncio.wait_for(socket.receive_text(), 5)
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
                raise ValueError("invalid browser document")
            deadline, session = self.tickets.pop(hashlib.sha256(ticket.encode()).digest())
            if self.coordinator.admissions._now() >= deadline:
                raise AdmissionRefused("task_expired")
            await self.review(socket, session)
            await socket.close(1000)
        except WebSocketDisconnect:
            pass
        except (AdmissionRefused, HTTPException, KeyError, TypeError, ValueError, TimeoutError):
            await socket.close(1008)
        finally:
            self.active -= 1

    async def review(self, socket, session):
        admissions = self.coordinator.admissions
        listener = events.subscribe()
        signal = listener[1]
        incoming = asyncio.create_task(socket.receive_text())
        try:
            first = True
            while True:
                signal.clear()
                task = await run_in_threadpool(admissions.task, session.identifier, generation=session.generation)
                if self.coordinator._closed or task["purpose"] != "browser.access" or task["state"] not in {"pending", "accepted"}:
                    raise AdmissionRefused("task_changed")
                remaining = session.expires_at - admissions._now()
                if remaining <= 0:
                    raise AdmissionRefused("task_expired")
                if first:
                    first = False
                    attachment = await run_in_threadpool(self.tasks.attachment, task)
                    await socket.send_json({"profile": "browser-task.v1", "task": task,
                        "extension_id": self.tasks.extension_id, "ceremony": None, **attachment})
                wake = asyncio.create_task(signal.wait())
                try:
                    done, _ = await asyncio.wait((wake, incoming), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
                if not done:
                    raise AdmissionRefused("task_expired")
                if incoming not in done:
                    continue
                raw = incoming.result()
                if len(raw) > 4096:
                    raise ValueError("invalid observation")
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("state") not in {"ready", "paused"}:
                    raise ValueError("invalid observation")
                self.observe(session, value)
                await socket.send_json({"state": "observed"})
                return
        finally:
            events.unsubscribe(listener)
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)
