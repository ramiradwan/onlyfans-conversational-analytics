import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.onboarding import surface
from app.onboarding.access_document import BrowserAccessDocuments
from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.onboarding.admission_routes import ORIGIN
from app.persistence.auth import SQLiteAuthenticationStore

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


def test_browser_access_document_holds_admission_and_reports_once(tmp_path, monkeypatch):
    entry = "browser-tasks/index.html"
    template = Path(__file__).resolve().parents[1] / "frontend" / entry
    (tmp_path / "browser-tasks").mkdir()
    (tmp_path / entry).write_bytes(template.read_bytes())
    (tmp_path / "setup-theme-init.js").touch()
    (tmp_path / "manifest.json").write_text(json.dumps({entry: {"file": entry, "isEntry": True}}), encoding="utf-8")
    monkeypatch.setattr(surface, "resource_path", lambda _: tmp_path)

    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    admissions = ScopedAdmissions(store, stage="setup")
    desktop = admissions.consume(admissions.prepare_desktop(str(uuid4())), role="desktop")
    task, _ = admissions.prepare_task(desktop.identifier, generation=desktop.generation,
        journey_id=desktop.journey_id, operation_id=str(uuid4()), purpose="browser.access",
        scope_digest="a" * 64, source_revision=1, deadline=admissions._now() + 300)
    entry, generation = admissions.reissue_access_document(desktop.identifier, generation=desktop.generation,
        task_id=task)
    with pytest.raises(AdmissionRefused):
        admissions.consume(entry, role="browser_task")
    received = []
    documents = BrowserAccessDocuments(SimpleNamespace(admissions=admissions, _closed=False),
        SimpleNamespace(extension_id="a" * 32, attachment=lambda task: {"entry_source": "desktop"}),
        lambda session, value: received.append((session.task_id, value)))
    app = FastAPI()

    @app.post("/browser-tasks")
    async def page(request: Request):
        return await documents.page(request)

    @app.websocket("/api/v1/browser-tasks/access-document")
    async def access_document(socket: WebSocket):
        await documents.open(socket)

    client = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 34567))
    url = ORIGIN.replace("http:", "ws:") + "/api/v1/browser-tasks/access-document"
    options = {"headers": {"Origin": ORIGIN}, "subprotocols": ["ofca.browser-access.v1"]}
    headers = {"Origin": ORIGIN, "sec-fetch-site": "same-origin", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    data = {"code": entry, "presentation": generation}
    assert client.post("/browser-tasks", headers=headers | {"sec-fetch-dest": "iframe"}, data=data).status_code == 403
    page = client.post("/browser-tasks", headers=headers, data=data)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    ticket = re.search(r'<template id="access-document-entry">([A-Za-z0-9_-]{43})</template>', page.text)[1]
    assert entry not in page.text and ticket != entry
    assert client.post("/browser-tasks", headers=headers, data=data).status_code == 401
    with client.websocket_connect(url, **options) as connection:
        connection.send_text(ticket)
        payload = connection.receive_json()
        assert set(payload) == {"profile", "task", "extension_id", "ceremony", "entry_source"}
        assert payload["task"]["task_id"] == task
        connection.send_json({"state": "ready"})
        assert connection.receive_json() == {"state": "observed"}
    assert received == [(task, {"state": "ready"})]
    with client.websocket_connect(url, **options) as replacement:
        replacement.send_text(ticket)
        with pytest.raises(WebSocketDisconnect) as stopped:
            replacement.receive_json()
        assert stopped.value.code == 1008
