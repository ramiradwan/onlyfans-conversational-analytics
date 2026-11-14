"""The paired waiting window: advertised sessions only, opened on accept, ended unless dispatched."""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bootstrap import transport_manager
from app.onboarding import timing
from app.onboarding.companion import WaitingWindows
from app.provisioning.events import events
from test_companion_browser_controls import ACCOUNT, REPORT, Channel, link

pytestmark = [pytest.mark.ci_tier("fast")]
ADVERTISEMENT = {"controls": ["browser.task.end"], "task_purposes": []}


@pytest.fixture(autouse=True)
def reset_manager():
    transport_manager.reset()
    transport_manager.browser_task_advertised = None
    timing.reset()
    yield
    transport_manager.reset()
    transport_manager.browser_task_advertised = None


async def opened(*, waiting=True):
    channel = Channel()
    session = link(channel)
    await session.report(REPORT)
    await session.capabilities(ADVERTISEMENT)
    if waiting:
        assert await session.waiting({}) == {}
    return channel, session


def waits(channel):
    return [value for value in channel.sent if str(value.get("action", "")).startswith("browser.task.waiting")]


class Operations:
    """The coordinator surface the waiting windows read: closure and operation state."""

    def __init__(self):
        self.states: dict[str, str] = {}
        self._closed = False
        self.store = SimpleNamespace(database=SimpleNamespace(read=self.read))

    @contextmanager
    def read(self):
        states = self.states

        class Connection:
            def execute(self, sql, params):
                state = states.get(params[0])
                return SimpleNamespace(fetchone=lambda: None if state is None else {"state": state})
        yield Connection()


def operation(action="auth.select", *, verb="select", browser=True, account=ACCOUNT):
    scope = {"creator_account_id": account, "browser": {"extension_id": "a" * 32} if browser else None}
    if verb is not None:
        scope["authentication_intent"] = {"verb": verb}
    return {"operation_id": str(uuid4()), "action": action, "owner_scope_json": json.dumps(scope)}


async def settle(rounds=10):
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_waiting_advertisement_follows_the_end_advertisement_and_carries_nothing():
    channel = Channel()
    session = link(channel)
    await session.report(REPORT)
    with pytest.raises(ValueError):
        await session.waiting({})
    await session.capabilities(ADVERTISEMENT)
    with pytest.raises(ValueError):
        await session.waiting({"controls": ["browser.task.waiting"]})
    assert await session.waiting({}) == {}


@pytest.mark.asyncio
async def test_waiting_controls_reach_only_an_advertising_session_and_are_closed():
    silent, quiet = await opened(waiting=False)
    operation_id = str(uuid4())
    assert await transport_manager.send_browser_task_waiting(ACCOUNT, quiet.token, operation_id, expires_at=1) is False
    assert waits(silent) == []
    channel, session = await opened()
    assert await transport_manager.send_browser_task_waiting(ACCOUNT, session.token, operation_id, expires_at=1900000000)
    assert await transport_manager.send_browser_task_waiting(ACCOUNT, session.token, operation_id, expires_at=None)
    opened_control, ended = waits(channel)
    assert set(opened_control) == {"type", "id", "action", "operation_id", "expires_at"}
    assert (opened_control["action"], opened_control["operation_id"], opened_control["expires_at"]) == (
        "browser.task.waiting", operation_id, 1900000000)
    assert set(ended) == {"type", "id", "action", "operation_id"}
    assert (ended["action"], ended["operation_id"]) == ("browser.task.waiting.end", operation_id)


@pytest.mark.asyncio
async def test_an_accepted_sign_in_opens_its_window_at_once_and_ends_it_when_refused():
    channel, _ = await opened()
    operations = Operations()
    windows = WaitingWindows(operations, transport_manager)
    value = operation()
    operations.states[value["operation_id"]] = "accepted"
    windows.accepted(value)
    await settle()
    assert [(item["action"], item["operation_id"]) for item in waits(channel)] == [("browser.task.waiting", value["operation_id"])]
    operations.states[value["operation_id"]] = "refused"
    events.publish()
    await settle()
    assert [item["action"] for item in waits(channel)] == ["browser.task.waiting", "browser.task.waiting.end"]


@pytest.mark.asyncio
async def test_a_dispatched_sign_in_keeps_its_window_and_any_other_delivery_ends_it():
    channel, _ = await opened()
    operations = Operations()
    windows = WaitingWindows(operations, transport_manager)
    kept, ended = operation(), operation("account.connect")
    for value in (kept, ended):
        operations.states[value["operation_id"]] = "waiting"
        windows.accepted(value)
    await settle()
    windows.delivered(kept["operation_id"], "dispatched")
    windows.delivered(ended["operation_id"], "unreachable")
    await settle()
    actions = {(item["action"], item["operation_id"]) for item in waits(channel)}
    assert ("browser.task.waiting.end", kept["operation_id"]) not in actions
    assert ("browser.task.waiting.end", ended["operation_id"]) in actions


@pytest.mark.asyncio
async def test_only_sign_in_selection_with_an_extension_and_a_paired_session_opens_a_window():
    channel, _ = await opened()
    operations = Operations()
    windows = WaitingWindows(operations, transport_manager)
    for value in (operation("auth.cancel", verb="cancel"), operation("account.connect", verb=None),
                  operation(browser=False), operation(account=None), operation(account="another-creator"),
                  operation("browser.access", verb=None)):
        operations.states[value["operation_id"]] = "accepted"
        windows.accepted(value)
    await settle()
    assert waits(channel) == []
    operations._closed = True
    windows.accepted(operation())
    await settle()
    assert waits(channel) == []
