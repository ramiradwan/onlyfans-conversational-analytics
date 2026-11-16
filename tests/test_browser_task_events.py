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
        launched = tasks.launch_signal(task["operation_id"])
        assert not launched.is_set()
        response = client.post(path, json={}, headers=headers(native))
        assert response.status_code == 200
        assert set(response.json()) == {"state", "entry_code"} and response.json()["state"] == "entry"
        # The returned route lets the click's deferred key preparation start.
        assert launched.is_set()
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


@pytest.mark.parametrize("reason", ["needs_account", "needs_consent", "needs_permission"])
def test_a_helper_reports_why_the_extension_cannot_open_its_task(tmp_path, reason):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    with TestClient(app, base_url=ORIGIN) as client:
        before = task_row(coordinator, browser)["state"]
        assert client.post("/api/v1/browser-tasks/ended", json={"reason": reason},
                           headers=headers(browser)).status_code == 200
        assert task_row(coordinator, browser)["state"] == before
        notices = client.get("/api/v1/desktop/browser-tasks/notices", headers=headers(native))
        assert notices.json() == {"notices": [{"task_id": task["task_id"], "reason": reason}]}


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


def test_open_of_a_sign_in_task_still_preparing_mints_no_entry(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))

    async def dispatcher(request, session, task_id):
        return "preparing"

    tasks.dispatcher = dispatcher
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET purpose='browser.authentication' WHERE task_id=?",
                           (task["task_id"],))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, None))
    path = "/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open"
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post(path, json={}, headers=headers(native)).json() == {"state": "unavailable"}
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_admission_entries").fetchone()[0] == 0


def test_a_preparing_sign_in_poll_reads_its_payload_through_one_store_connection(tmp_path, monkeypatch):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, _, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET purpose='browser.authentication' WHERE task_id=?",
                           (task["task_id"],))

    class Preparing:
        def browser_entry(self, operation_id):
            with coordinator.store.database.read() as connection:
                connection.execute("SELECT 1 FROM desktop_setup_operations WHERE operation_id=?", (operation_id,))
            return None

    database, opened = coordinator.store.database, []
    connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *args, **kwargs: opened.append(1) or connect(*args, **kwargs))
    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, Preparing()))
    with TestClient(app, base_url=ORIGIN) as client:
        opened.clear()
        response = client.get("/api/v1/browser-tasks/payload", headers=headers(browser))
        assert response.status_code == 200 and response.json()["ceremony"] is None
        assert response.json()["task"]["task_id"] == task["task_id"]
        assert len(opened) == 1


@pytest.mark.asyncio
async def test_a_preparing_sign_in_stream_frames_its_entry_opening_without_a_state_change(tmp_path):
    from app.provisioning.events import events
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, _, _ = setup
    _, task, browser = await browser_access(setup)
    opened = [False]
    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers,
                                             entry=lambda row: opened[0])).start()
    try:
        assert (await served.frame())[1]["state"] == "pending"
        events.publish()
        with pytest.raises(TimeoutError):
            await served.frame(timeout=0.2)
        opened[0] = True
        events.publish()
        event, value = await served.frame()
        assert event == "browser-task"
        assert value == {"task_id": task["task_id"], "admission_generation": browser.generation, "state": "pending"}
    finally:
        await served.close()


@pytest.mark.asyncio
async def test_open_reissues_at_once_for_a_sign_in_helper_still_waiting_for_its_ceremony(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    _, task, browser = await browser_access(setup)
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET purpose='browser.authentication' WHERE task_id=?",
                           (task["task_id"],))

    class Preparing:
        def browser_entry(self, operation_id):
            return None

    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers)).start()
    try:
        await served.frame()
        app = FastAPI()
        app.include_router(browser_router(coordinator, tasks, Preparing()))
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            response = await asyncio.wait_for(client.post("/api/v1/desktop/browser-tasks/" + task["task_id"] + "/open",
                                                          json={}, headers=headers(native)), 1)
        assert response.json()["state"] == "entry"
    finally:
        await served.close()

@pytest.mark.asyncio
async def test_a_helper_stream_reads_its_task_off_the_event_loop_and_keeps_a_wake_from_that_read(tmp_path):
    import threading
    from app.provisioning.events import events
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, _, _ = setup
    _, task, browser = await browser_access(setup)
    loop_thread, threads, opened = threading.get_ident(), [], [False, False]

    def entry(row):
        threads.append(threading.get_ident())
        if opened[1]:
            # A commit lands while this read is in flight. Its wake must reach the next pass.
            opened[0], opened[1] = True, False
            events.publish()
            return False
        return opened[0]

    served = Served(BrowserTaskEventResponse(coordinator, browser, task_row(coordinator, browser), tasks.helpers,
                                             entry=entry)).start()
    try:
        assert (await served.frame())[1]["state"] == "pending"
        opened[1] = True
        events.publish()
        event, value = await served.frame()
        assert event == "browser-task" and value["state"] == "pending"
        assert opened == [True, False] and len(threads) >= 3
        assert loop_thread not in threads
    finally:
        await served.close()

def test_a_sign_in_payload_read_records_its_queue_wait_and_handler_time(tmp_path, caplog):
    import logging
    import re
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, _, _ = setup
    _, task, browser = asyncio.run(browser_access(setup))

    class Preparing:
        def browser_entry(self, operation_id):
            return None

    app = FastAPI()
    app.include_router(browser_router(coordinator, tasks, Preparing()))
    caplog.set_level(logging.INFO, logger="app.onboarding.timing")
    with TestClient(app, base_url=ORIGIN) as client:
        # A browser-access helper polls for a long time and records nothing.
        assert client.get("/api/v1/browser-tasks/payload", headers=headers(browser)).status_code == 200
        assert not caplog.messages
        with coordinator.store.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_tasks SET purpose='browser.authentication' WHERE task_id=?",
                               (task["task_id"],))
        assert client.get("/api/v1/browser-tasks/payload", headers=headers(browser)).status_code == 200
    assert [re.sub(r"elapsed_ms=\d+", "elapsed_ms=n", message) for message in caplog.messages] == [
        "event=sign_in_phase operation=helper phase=payload-queue status=ok elapsed_ms=n",
        "event=sign_in_phase operation=helper phase=payload-handler status=ok elapsed_ms=n"]
