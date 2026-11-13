"""The paired end control: advertised sessions only, replay limited to the dispatching pairing."""
import asyncio
import logging
from uuid import uuid4

import pytest

from app.bootstrap import transport_manager
from app.onboarding import timing
from app.onboarding.task_ends import BrowserTaskEnds
from app.transport.companion_records import CompanionRecordError
from test_companion_browser_controls import ACCOUNT, REPORT, Channel, link
from test_desktop_browser_tasks import browser_access, setup as browser_setup

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


async def opened(pairing_id=b"p" * 32):
    channel = Channel()
    session = link(channel, pairing_id=pairing_id)
    await session.report(REPORT)
    return channel, session


def ends_of(channel):
    return [value for value in channel.sent if value.get("action") == "browser.task.end"]


def synthetic_task(coordinator, template, *, state, expires_in=600, pairing=None, ends=None):
    task_id, generation = str(uuid4()), str(uuid4())
    now = coordinator.admissions._now()
    with coordinator.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_browser_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, template["journey_id"], template["desktop_generation"], str(uuid4()), "browser.authentication",
             template["scope_digest"], 0, generation, "setup", state, now + expires_in))
        if pairing is not None:
            connection.execute("INSERT INTO desktop_browser_task_dispatches VALUES (?,?,?)", (task_id, generation, pairing))
    return task_id, generation


def first_task(coordinator):
    with coordinator.store.database.read() as connection:
        return dict(connection.execute("SELECT * FROM desktop_browser_tasks").fetchone())


@pytest.mark.asyncio
async def test_advertisement_is_closed_and_needs_a_reporting_session():
    channel = Channel()
    session = link(channel)
    with pytest.raises(CompanionRecordError):
        await session.capabilities(ADVERTISEMENT)
    await session.report(REPORT)
    for bad in ({}, {"controls": ["browser.task.end"]}, {**ADVERTISEMENT, "extra": 1},
                {"controls": ["browser.task.end", "x"], "task_purposes": []},
                {"controls": ["browser.task.end"], "task_purposes": ["browser.authentication"]},
                {"controls": ["browser.task.end"], "task_purposes": ["browser.identity", "browser.access"]}):
        with pytest.raises(ValueError):
            await session.capabilities(bad)
    assert await session.capabilities({"controls": ["browser.task.end"],
                                       "task_purposes": ["browser.access", "browser.identity"]}) == {}


@pytest.mark.asyncio
async def test_end_control_only_to_advertising_sessions_of_the_dispatching_pairing(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    await browser_access(setup)
    advertised, first = await opened()
    silent, _ = await opened()
    other, third = await opened(b"q" * 32)
    await first.capabilities(ADVERTISEMENT)
    await third.capabilities(ADVERTISEMENT)
    pairing = transport_manager.browser_task_pairing(ACCOUNT, first.token)
    ends = BrowserTaskEnds(coordinator, transport_manager)
    task_id, generation = synthetic_task(coordinator, first_task(coordinator), state="accepted", pairing=pairing)
    await ends.flush()
    assert ends_of(advertised) == []
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET state='completed' WHERE task_id=?", (task_id,))
    await ends.flush()
    await ends.flush()
    sent = ends_of(advertised)
    assert len(sent) == 1
    assert set(sent[0]) == {"type", "id", "action", "task_id", "admission_generation"}
    assert (sent[0]["task_id"], sent[0]["admission_generation"]) == (task_id, generation)
    assert ends_of(silent) == [] and ends_of(other) == []


@pytest.mark.asyncio
async def test_end_replayed_on_advertisement_only_for_this_pairings_ended_unexpired_tasks(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    await browser_access(setup)
    channel, session = await opened()
    pairing = transport_manager.browser_task_pairing(ACCOUNT, session.token)
    template = first_task(coordinator)
    expected = {synthetic_task(coordinator, template, state="completed", pairing=pairing, expires_in=600 + n)
                for n in range(9)}
    synthetic_task(coordinator, template, state="completed", pairing=pairing, expires_in=-1)
    synthetic_task(coordinator, template, state="accepted", pairing=pairing)
    synthetic_task(coordinator, template, state="completed", pairing="another-pairing")
    synthetic_task(coordinator, template, state="completed")
    ends = BrowserTaskEnds(coordinator, transport_manager)
    transport_manager.browser_task_advertised = ends.replay
    await session.capabilities(ADVERTISEMENT)
    sent = {(value["task_id"], value["admission_generation"]) for value in ends_of(channel)}
    assert len(sent) == 8 and sent <= expected
    await ends.stop()


@pytest.mark.asyncio
async def test_fenced_generation_ends_for_its_pairing(tmp_path):
    setup = browser_setup.__wrapped__(tmp_path)
    coordinator, tasks, native, _ = setup
    await browser_access(setup)
    channel, session = await opened()
    await session.capabilities(ADVERTISEMENT)
    pairing = transport_manager.browser_task_pairing(ACCOUNT, session.token)
    task_id, generation = synthetic_task(coordinator, first_task(coordinator), state="accepted", pairing=pairing)
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET admission_generation=? WHERE task_id=?", (str(uuid4()), task_id))
    await BrowserTaskEnds(coordinator, transport_manager).flush()
    assert [(value["task_id"], value["admission_generation"]) for value in ends_of(channel)] == [(task_id, generation)]


def test_sign_in_close_timing_record_has_no_identifiers(caplog):
    clock = iter([10.0, 10.2, 10.25, 10.3])
    operation = str(uuid4())
    timing.mark(operation, "frame", clock=lambda: next(clock))
    timing.mark(operation, "reconciled", clock=lambda: next(clock))
    timing.mark(operation, "completed", clock=lambda: next(clock))
    with caplog.at_level(logging.INFO, logger="app.onboarding.timing"):
        record = timing.close(operation, "paired", clock=lambda: next(clock))
    assert set(record) == {"event", "run", "route", "frame_to_reconciled_ms", "reconciled_to_completed_ms",
                           "completed_to_sent_ms"}
    assert record["frame_to_reconciled_ms"] == 200 and record["completed_to_sent_ms"] == 50
    assert all(type(record[name]) is int for name in record if name.endswith("_ms") or name == "run")
    assert operation not in caplog.text
    assert timing.close(operation, "paired") is None
