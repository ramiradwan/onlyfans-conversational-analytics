import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from app.onboarding.admission import csrf_token
from app.onboarding.admission_routes import COOKIE_NAMES, CSRF, GENERATION, ORIGIN
from app.onboarding.browser_routes import browser_router
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import OperationResult
from test_desktop_browser_tasks import observation, setup as browser_setup, browser_access
from test_desktop_ceremonies import setup as ceremony_setup, authorized

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "shared/browser-tasks/schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def headers(session):
    return {"Cookie": f"{COOKIE_NAMES[session.role]}={session.identifier}", GENERATION: session.generation,
        CSRF: csrf_token(session.identifier), "Origin": ORIGIN}


def test_popup_timing_is_local_closed_and_cannot_reconcile(tmp_path, caplog):
    import logging
    from starlette.websockets import WebSocketDisconnect
    from app.transport.companion_origin import CompanionOriginBoundary
    coordinator, tasks, _, _ = browser_setup.__wrapped__(tmp_path)
    app = FastAPI()
    app.add_middleware(CompanionOriginBoundary)
    app.include_router(browser_router(coordinator, tasks, None))
    caplog.set_level(logging.INFO, logger="app.onboarding.timing")
    value = {"operation": "extension", "phase": "popup-remove", "status": "ok", "elapsed_ms": 7}
    socket_headers = {"Host": "127.0.0.1:17871", "Origin": "chrome-extension://" + "a" * 32}
    with TestClient(app, base_url="http://127.0.0.1:17871") as client:
        path = "/api/v1/browser-tasks/timing"
        with client.websocket_connect(path, headers=socket_headers) as socket:
            socket.send_json(value)
            with pytest.raises(WebSocketDisconnect) as ended:
                socket.receive_text()
            assert ended.value.code == 1000
        with client.websocket_connect(path, headers=socket_headers) as socket:
            socket.send_json({**value, "detail": "private"})
            with pytest.raises(WebSocketDisconnect) as ended:
                socket.receive_text()
            assert ended.value.code == 1008
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={**socket_headers, "Origin": "chrome-extension://" + "b" * 32}):
                pass
    assert caplog.messages == ["event=sign_in_phase operation=extension phase=popup-remove status=ok elapsed_ms=7"]
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] == 0


def test_browser_payload_and_observation_are_closed_and_role_scoped(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    with TestClient(app, base_url=ORIGIN) as client:
        path = "/api/v1/browser-tasks/payload"
        assert client.get(path, headers=headers(native)).status_code == 401
        response = client.get(path, headers=headers(browser))
        assert response.status_code == 200
        VALIDATOR.validate(response.json())
        assert response.json()["task"]["task_id"] == task["task_id"] and response.json()["ceremony"] is None
        assert client.get("/api/v1/desktop/browser-tasks", headers=headers(native)).json()["tasks"][0]["task_id"] == task["task_id"]
        value = observation(browser)
        VALIDATOR.validate(value)
        assert client.post("/api/v1/browser-tasks/observation", json=value, headers=headers(browser)).status_code == 200
        assert client.post("/api/v1/browser-tasks/observation", json={**value, "account_authorized": True},
            headers=headers(browser)).status_code == 409
        assert client.get("/api/v1/desktop/browser-tasks", headers=headers(browser)).status_code == 401
        assert client.get("/api/v1/desktop/browser-tasks", headers=headers(native)).json()["tasks"] == []


def test_browser_notification_only_reconciles_its_original_saved_ceremony(tmp_path):
    coordinator, ceremonies, _, operation, binding, _ = ceremony_setup.__wrapped__(tmp_path)
    asyncio.run(ceremonies.prepare(operation["operation_id"], binding, authorize=authorized))
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    task_id = tasks.prepare(operation, "browser.authentication")
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    entry = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task_id)
    browser = coordinator.admissions.consume(entry, role="browser_task")
    coordinator.owner.result = OperationResult("waiting")
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, ceremonies))
    saved = ceremonies.row(operation["operation_id"])
    value = {"ceremony_id": saved["ceremony_id"], "attempt_id": binding["attempt_id"], "notification": "changed"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post("/api/v1/browser-tasks/notification", json=value, headers=headers(native)).status_code == 401
        for extra in ({"attempt_id": "A" * 43}, {"completed": True}, {"continuation": "clr1." + "A" * 43}):
            assert client.post("/api/v1/browser-tasks/notification", json={**value, **extra}, headers=headers(browser)).status_code == 409
        assert not coordinator.owner.calls
        response = client.post("/api/v1/browser-tasks/notification", json=value, headers=headers(browser))
        assert response.status_code == 202 and response.json() == {"state": "checking"}
        VALIDATOR.validate(client.get("/api/v1/browser-tasks/payload", headers=headers(browser)).json())
    assert coordinator.owner.calls
    assert all(row["operation_id"] == operation["operation_id"] and recover for row, recover in coordinator.owner.calls)
    assert coordinator.receipt(native, operation["operation_id"])["state"] == "waiting"


def test_completed_authentication_payload_withdraws_launch_authority(tmp_path, monkeypatch):
    coordinator, ceremonies, _, operation, binding, _ = ceremony_setup.__wrapped__(tmp_path)
    asyncio.run(ceremonies.prepare(operation["operation_id"], binding, authorize=authorized))
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    task_id = tasks.prepare(operation, "browser.authentication")
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    entry = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task_id)
    browser = coordinator.admissions.consume(entry, role="browser_task")
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, ceremonies))
    with TestClient(app, base_url=ORIGIN) as client:
        before = client.get("/api/v1/browser-tasks/payload", headers=headers(browser)).json()
        assert before["ceremony"] is not None
        tasks.complete(operation["operation_id"])
        monkeypatch.setattr(ceremonies, "browser_entry", lambda _: pytest.fail("Completed task cannot read launch authority"))
        response = client.get("/api/v1/browser-tasks/payload", headers=headers(browser))
        assert response.status_code == 200
        value = response.json()
        VALIDATOR.validate(value)
        assert value["ceremony"] is None and value["task"]["state"] == "completed"
        assert {key: item for key, item in value["task"].items() if key != "state"} == {
            key: item for key, item in before["task"].items() if key != "state"}
