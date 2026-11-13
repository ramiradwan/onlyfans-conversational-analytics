import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.onboarding import helper_events
from app.onboarding.admission_routes import ORIGIN
from app.onboarding.browser_routes import browser_router
from app.onboarding.helper_events import BrowserTaskEventResponse
from app.onboarding.routes import DesktopEventResponse
from test_desktop_browser_routes import headers
from test_desktop_browser_tasks import browser_access, setup as browser_setup

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class Served:
    """Drive one streaming response in-process and collect its frames."""

    def __init__(self, response):
        self.response, self.chunks, self.status = response, asyncio.Queue(), None
        self.disconnect = asyncio.Event()

    async def _receive(self):
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body" and message.get("body"):
            await self.chunks.put(message["body"].decode())

    def start(self):
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "asgi": {"spec_version": "2.4"}}
        self.task = asyncio.create_task(self.response(scope, self._receive, self._send))
        return self

    async def frame(self, timeout=1):
        while True:
            chunk = await asyncio.wait_for(self.chunks.get(), timeout)
            if not chunk.startswith(":"):
                event, data = chunk.strip().split("\n")
                return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))

    async def close(self):
        self.disconnect.set()
        self.task.cancel()
        await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 2)


def task_row(coordinator, browser):
    return coordinator.admissions.task(browser.identifier, generation=browser.generation)


@pytest.mark.asyncio
async def test_browser_task_events_frame_state_changes_and_end_after_terminal(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = await browser_access(setup)
    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers)).start()
    try:
        event, value = await served.frame()
        assert event == "browser-task"
        assert value == {"task_id": task["task_id"], "admission_generation": browser.generation, "state": "pending"}
        assert tasks.helpers.live(task["task_id"], browser.generation)
        second = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers)).start()
        await asyncio.wait_for(second.task, 1)
        assert second.status == 429
        tasks.complete(task["operation_id"])
        event, value = await served.frame()
        assert value["state"] == "completed"
        await asyncio.wait_for(served.task, 1)
        assert not tasks.helpers.live(task["task_id"], browser.generation)
    finally:
        await served.close()


def test_browser_task_events_require_the_helper_role(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    asyncio.run(browser_access(setup))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/browser-tasks/events", headers=headers(native)).status_code == 401
        assert client.get("/api/v1/browser-tasks/events?x=1", headers=headers(native)).status_code == 400


@pytest.mark.asyncio
async def test_focus_is_answered_only_by_the_live_helper_of_that_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(helper_events, "FOCUS_SECONDS", 0.2)
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = await browser_access(setup)
    assert await tasks.helpers.focus(task["task_id"], browser.generation) is False
    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers)).start()
    try:
        await served.frame()
        pending = asyncio.create_task(tasks.helpers.focus(task["task_id"], browser.generation, timeout=1))
        event, value = await served.frame()
        assert event == "browser-task-focus"
        assert value == {"task_id": task["task_id"], "admission_generation": browser.generation}
        assert tasks.helpers.focused(task["task_id"], "other-generation") is False
        assert tasks.helpers.focused(task["task_id"], browser.generation) is True
        assert await pending is True
        # Without a confirmation, focus gives up and the caller opens Chrome instead.
        assert await tasks.helpers.focus(task["task_id"], browser.generation) is False
    finally:
        await served.close()


def test_open_mints_an_entry_only_without_a_session_or_live_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(helper_events, "FOCUS_SECONDS", 0.05)
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    path = "/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open"
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post(path, json={}, headers=headers(browser)).status_code == 401
        response = client.post(path, json={}, headers=headers(native))
        assert response.status_code == 200
        assert set(response.json()) == {"state", "entry_code"} and response.json()["state"] == "entry"
        # The reissue fenced the earlier helper generation.
        assert client.get("/api/v1/browser-tasks/payload", headers=headers(browser)).status_code == 401
        tasks.complete(task["operation_id"])
        assert client.post(path, json={}, headers=headers(native)).json() == {"state": "unavailable"}


def test_open_prefers_the_paired_session_and_mints_no_entry(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    calls = []

    async def dispatcher(request, session, task_id):
        calls.append(task_id)
        return "dispatched"

    tasks.dispatcher = dispatcher
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    path = "/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open"
    with TestClient(app, base_url=ORIGIN) as client:
        # Only sign-in tasks are dispatched; this access task still uses the helper route.
        assert client.post(path, json={}, headers=headers(native)).json()["state"] == "entry"
        assert calls == []
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET purpose='browser.authentication' WHERE task_id=?",
                           (task["task_id"],))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post(path, json={}, headers=headers(native)).json() == {"state": "dispatched"}
        assert calls == [task["task_id"]]


@pytest.mark.asyncio
async def test_open_focuses_a_live_helper_instead_of_opening_chrome(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = await browser_access(setup)
    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers)).start()
    try:
        await served.frame()
        app = FastAPI()
        app.include_router(browser_router(coordinator, tasks, None))
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            opening = asyncio.create_task(client.post("/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open",
                                                      json={}, headers=headers(native)))
            event, _ = await served.frame()
            assert event == "browser-task-focus"
            again = await client.post("/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open",
                                      json={}, headers=headers(native))
            assert again.status_code == 429
            confirmed = await client.post("/api/v1/browser-tasks/focused", json={}, headers=headers(browser))
            assert confirmed.status_code == 200
            assert (await opening).json() == {"state": "focused"}
    finally:
        await served.close()


def test_ended_records_one_display_notice_and_changes_no_task_state(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    with TestClient(app, base_url=ORIGIN) as client:
        before = task_row(coordinator, browser)["state"]
        for bad in ({}, {"reason": "completed"}, {"reason": "task_expired", "extra": 1}):
            assert client.post("/api/v1/browser-tasks/ended", json=bad, headers=headers(browser)).status_code == 400
        assert client.post("/api/v1/browser-tasks/ended", json={"reason": "profile_mismatch"},
                           headers=headers(browser)).status_code == 200
        assert client.post("/api/v1/browser-tasks/ended", json={"reason": "task_expired"},
                           headers=headers(browser)).status_code == 200
        assert task_row(coordinator, browser)["state"] == before
        notices = client.get("/api/v1/desktop/browser-tasks/notices", headers=headers(native))
        assert notices.json() == {"notices": [{"task_id": task["task_id"], "reason": "profile_mismatch"}]}
        assert client.get("/api/v1/desktop/browser-tasks/notices", headers=headers(browser)).status_code == 401


@pytest.mark.asyncio
async def test_the_desktop_stream_shows_a_helper_notice_and_its_clearing(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = await browser_access(setup)

    def notices(session):
        return tasks.helpers.notices({row["task_id"] for row in tasks.current_native(session)})

    served = Served(DesktopEventResponse(coordinator, native, actor=lambda: None, active=set(),
                                         deadline=lambda: 1800.0, notices=notices)).start()
    try:
        event, _ = await served.frame()
        assert event == "setup"
        tasks.helpers.notice(task["task_id"], browser.generation, "profile_mismatch")
        event, value = await served.frame()
        assert (event, value) == ("browser-notices", {"notices": [{"task_id": task["task_id"], "reason": "profile_mismatch"}]})
        tasks.helpers.clear_notice(task["task_id"])
        event, value = await served.frame()
        assert (event, value) == ("browser-notices", {"notices": []})
    finally:
        await served.close()
