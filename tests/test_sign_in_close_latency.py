"""Sign-in close latency with a healthy wait: Provider admission to the helper frame and to the paired end control."""
import asyncio
import time

import pytest

from app.bootstrap import transport_manager
from app.onboarding import timing
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.helper_events import BrowserTaskEventResponse
from app.onboarding.task_ends import BrowserTaskEnds
from test_browser_task_ends import ADVERTISEMENT, ends_of, opened
from test_browser_task_events import Served
from test_companion_browser_controls import ACCOUNT
from test_desktop_authentication import accept, auth  # noqa: F401 - fixture
from test_desktop_ceremonies import authorized, setup  # noqa: F401 - fixture

pytestmark = [pytest.mark.ci_tier("fast")]
LATENCY = 0.1
BUDGET = 0.8


@pytest.fixture(autouse=True)
def reset_manager():
    transport_manager.reset()
    transport_manager.browser_task_advertised = None
    timing.reset()
    yield
    transport_manager.reset()
    transport_manager.browser_task_advertised = None


class Slow:
    """Every Provider request takes LATENCY before it reaches the scripted client."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def delayed(*args, **kwargs):
            time.sleep(LATENCY)
            return value(*args, **kwargs)
        return delayed


class Waits:
    """A held outcome wait. Its first frame and each later frame arrive LATENCY after the server state."""

    def __init__(self):
        self.revision = 1
        self.changed = asyncio.Event()

    def envelope(self, request, *, before_send):
        before_send()
        time.sleep(LATENCY)
        return request

    async def hints(self, envelope):
        sent = 0
        while True:
            if sent == self.revision:
                self.changed.clear()
                await self.changed.wait()
            await asyncio.sleep(LATENCY)
            sent = self.revision
            yield (sent, "retired", "committed") if sent > 1 else (sent, "active", "awaiting-authentication")

    def admit(self):
        self.revision += 1
        self.changed.set()


async def _until(condition, timeout=2):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.002)


@pytest.mark.asyncio
@pytest.mark.parametrize("run", range(5))
async def test_admission_reaches_the_helper_and_the_paired_session_within_budget(auth, run):
    coordinator, _, authentication, client, _, native, _ = auth
    authentication.client = Slow(client)
    authentication.ceremonies.client = Slow(authentication.ceremonies.client)
    waits = Waits()
    authentication.waits = waits
    operation, _ = accept(auth)
    identifier = operation["operation_id"]
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    owner = coordinator.owner

    async def execute(current, *, reconcile, actor=None):
        owner.calls.append((dict(current), reconcile))
        return await authentication.execute(current, authorize=authorized, read_guard=authorized, reconcile=reconcile)

    owner.execute = execute
    owner.automatic_recovery = lambda current: current["operation_id"] == identifier
    owner.probe_interval = lambda current: authentication.probe_interval(current["operation_id"])
    owner.probe = lambda current: authentication.probe(current["operation_id"], authorize=authorized)
    owner.wait = lambda current: authentication.wait(current["operation_id"], authorize=authorized)
    owner.settled = lambda current, result: tasks.complete(current["operation_id"]) if result.state == "completed" else None

    coordinator._start(operation, reconcile=False, actor=None)
    await coordinator.drain()
    task_id = tasks.prepare(operation, "browser.authentication")
    code = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task_id)
    browser = coordinator.admissions.consume(code, role="browser_task")
    task = coordinator.admissions.task(browser.identifier, generation=browser.generation)

    ends = BrowserTaskEnds(coordinator, transport_manager)
    channel, session = await opened()
    await session.capabilities(ADVERTISEMENT)
    ends.record(task, transport_manager.browser_task_pairing(ACCOUNT, session.token))
    served = Served(BrowserTaskEventResponse(coordinator, browser, task, tasks.helpers)).start()
    coordinator.start()
    try:
        event, value = await served.frame()
        assert (event, value["state"]) == ("browser-task", "pending")
        # Startup recovery and the wait's first frame settle before the server admits.
        await asyncio.sleep(0.5)
        await _until(lambda: not coordinator._workers)
        assert ends_of(channel) == []

        loop = asyncio.get_running_loop()
        client.attempts[client.current]["state"] = "admitted"
        admitted = loop.time()
        waits.admit()
        helper_closed = paired_closed = None

        async def helper():
            nonlocal helper_closed
            while True:
                event, value = await served.frame(timeout=BUDGET * 3)
                if value.get("state") == "completed":
                    helper_closed = loop.time() - admitted
                    return

        async def paired():
            nonlocal paired_closed
            await _until(lambda: ends_of(channel), timeout=BUDGET * 3)
            paired_closed = loop.time() - admitted

        await asyncio.gather(helper(), paired())
        assert helper_closed < BUDGET, f"helper frame after {helper_closed:.3f} s"
        assert paired_closed < BUDGET, f"paired end after {paired_closed:.3f} s"
        sent = ends_of(channel)
        assert len(sent) == 1 and (sent[0]["task_id"], sent[0]["admission_generation"]) == (
            task["task_id"], task["admission_generation"])
    finally:
        await served.close()
        await ends.stop()
        await coordinator.stop()
