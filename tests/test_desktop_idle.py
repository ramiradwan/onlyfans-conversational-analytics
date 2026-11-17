import asyncio
import json
import threading
from datetime import timedelta

import pytest

from app.onboarding.admission import AdmissionRefused
from app.onboarding.authentication import DesktopAuthentication
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import CommandRefused
from app.onboarding.decisions import DesktopDecisions
from app.security.installation_key import InstallationKeyAuthority
from test_desktop_browser_tasks import browser_access, initial_ceremony_case, launched_key, observation, setup as setup
from test_desktop_coordinator import command
from test_desktop_decisions import DecisionClient, authentication_projection
from test_installation_key import EmulatedPlatformProvider

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


async def completed_access(setup):
    coordinator, tasks, native, _ = setup
    coordinator.owner.key = InstallationKeyAuthority(coordinator.store, EmulatedPlatformProvider())
    request, task, browser = await browser_access(setup)
    await launched_key(coordinator, tasks, request["operation_id"])
    value = observation(browser)
    tasks.observe(browser, value)
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    return request, task, browser, value


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [10, 30, 60])
async def test_idle_access_survives_deadlines_and_presentation_replacement(setup, minutes):
    coordinator, tasks, native, clock = setup
    _, task, browser, value = await completed_access(setup)
    before = coordinator.read(native)
    assert before["next_task"] == "account-connection"
    clock[0] += timedelta(minutes=minutes)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
        replacing_generation=native.generation), role="desktop")
    reopened = BrowserTasks(coordinator.admissions, extension_id=tasks.extension_id)
    coordinator.owner.browser_tasks = reopened
    for _ in range(3):
        snapshot = coordinator.read(replacement)
        assert snapshot["next_task"] == "account-connection"
        assert snapshot["facts"]["browser_access"] == "verified"
    assert reopened.progress(browser.journey_id) == value
    assert reopened.current_native(replacement) == []
    with pytest.raises(AdmissionRefused):
        tasks.observe(browser, value)
    accepted, fresh = coordinator._accept(replacement, command(snapshot, "account.connect"))
    assert fresh and accepted["action"] == "account.connect"
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_browser_tasks").fetchone()[0] == (minutes < 30)
    await coordinator.owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs-consent", "needs-permission", "needs-account", "unavailable"])
async def test_real_browser_invalidation_replaces_saved_progress(setup, state):
    coordinator, tasks, native, clock = setup
    _, _, browser, value = await completed_access(setup)
    tasks.observe(browser, value | {"state": state, "creator_account_id": None, "revision": 2})
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    assert coordinator.read(replacement)["next_task"] == "browser-access"
    await coordinator.owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_key", [False, True])
async def test_missing_or_changed_key_invalidates_saved_browser_progress(setup, replace_key):
    coordinator, tasks, native, clock = setup
    await completed_access(setup)
    with coordinator.store.database.transaction() as connection:
        connection.execute("DELETE FROM installation_key_reference")
    if replace_key:
        coordinator.owner.key.ensure_ready()
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    assert coordinator.read(replacement)["next_task"] == "browser-access"
    await coordinator.owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_key", [False, True])
async def test_old_browser_task_cannot_rebind_progress_after_key_invalidation(setup, replace_key):
    coordinator, tasks, native, clock = setup
    _, _, browser, value = await completed_access(setup)
    with coordinator.store.database.transaction() as connection:
        connection.execute("DELETE FROM installation_key_reference")
    if replace_key:
        coordinator.owner.key.ensure_ready()
    tasks.observe(browser, value | {"revision": 2})
    assert coordinator.read(native)["next_task"] == "browser-access"
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    assert coordinator.read(replacement)["next_task"] == "browser-access"
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_new_access_click_records_progress_for_a_replacement_key(setup):
    coordinator, tasks, native, clock = setup
    await completed_access(setup)
    with coordinator.store.database.transaction() as connection:
        connection.execute("DELETE FROM installation_key_reference")
    coordinator.owner.key.ensure_ready()
    assert coordinator.read(native)["next_task"] == "browser-access"
    request, _, browser = await browser_access(setup)
    await launched_key(coordinator, tasks, request["operation_id"])
    tasks.observe(browser, observation(browser))
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    assert coordinator.read(replacement)["next_task"] == "account-connection"
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_changed_creator_fences_the_previous_screen(setup):
    coordinator, tasks, native, _ = setup
    _, _, browser, value = await completed_access(setup)
    previous_click = command(coordinator.read(native), "account.connect")
    changed = value | {"creator_account_id": "different-test-creator", "revision": 2, "account_generation": 2}
    tasks.observe(browser, changed)
    assert tasks.progress(browser.journey_id) == changed
    with pytest.raises(CommandRefused):
        coordinator._accept(native, previous_click)
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_saved_progress_refuses_ambiguous_live_observations(setup, monkeypatch):
    coordinator, tasks, _, _ = setup
    _, _, browser, value = await completed_access(setup)
    monkeypatch.setattr(tasks, "_observations", lambda *_: [{"observation_json": json.dumps(value)}] * 2)
    assert tasks.progress(browser.journey_id) is None
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_saved_progress_requires_the_supported_extension(setup):
    coordinator, tasks, _, clock = setup
    _, _, browser, _ = await completed_access(setup)
    clock[0] += timedelta(minutes=31)
    changed = BrowserTasks(coordinator.admissions, extension_id="b" * 32)
    assert changed.progress(browser.journey_id) is None
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_browser_progress_survives_a_key_that_finishes_after_observation(setup):
    coordinator, tasks, native, clock = setup
    started, release = threading.Event(), threading.Event()

    class DelayedKey(InstallationKeyAuthority):
        def ensure_ready(self):
            started.set()
            assert release.wait(10)
            return super().ensure_ready()

    coordinator.owner.key = DelayedKey(coordinator.store, EmulatedPlatformProvider())
    request, _, browser = await browser_access(setup)
    tasks.launched(request["operation_id"])
    try:
        assert await asyncio.to_thread(started.wait, 5)
        tasks.observe(browser, observation(browser))
        await coordinator.reconcile(native, request["operation_id"])
        await coordinator.drain()
        assert coordinator.store.installation_key_reference() is None
    finally:
        release.set()
        await coordinator.owner._key_task
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    assert coordinator.read(replacement)["next_task"] == "account-connection"
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_idle_progress_reads_create_no_work_or_polling(setup):
    coordinator, tasks, native, clock = setup
    await completed_access(setup)
    clock[0] += timedelta(minutes=61)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    with coordinator.store.database.read() as connection:
        before = connection.total_changes
        for _ in range(5):
            assert coordinator.read(replacement)["next_task"] == "account-connection"
        assert connection.total_changes == before
    coordinator.start()
    await asyncio.sleep(0.05)
    assert not coordinator._workers and not coordinator._probes and not coordinator._waits
    assert not coordinator._prefetches
    await coordinator.stop()
    await coordinator.owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [11, 31, 61])
async def test_confirm_idle_uses_sign_in_choices_without_repeating_browser_access(setup, minutes):
    coordinator, initial, client, operation, scope, clock, native = await initial_ceremony_case(setup)
    tasks, ceremonies = coordinator.owner.browser_tasks, coordinator.owner.ceremonies
    with coordinator.store.database.read() as connection:
        access = connection.execute("SELECT operation_id FROM desktop_setup_operations WHERE action='browser.access'").fetchone()[0]
    tasks.complete(access)
    authentication = DesktopAuthentication(coordinator.store, ceremonies, None)
    ceremonies.authentication = authentication
    row = ceremonies.row(operation["operation_id"])
    authentication._save(row, authentication_projection(row))
    decisions = DesktopDecisions(coordinator.store, ceremonies, DecisionClient())
    ceremonies.decisions = decisions
    await decisions.refresh(operation["operation_id"], authorize=lambda: None)
    before = coordinator.read(native)
    assert before["next_task"] == "account-connection" and before["decision"] is not None
    stale_click = command(before, "account.confirm")
    clock[0] += timedelta(minutes=minutes)
    result = await coordinator.owner.execute(operation, reconcile=True)
    coordinator._settle(operation, result)
    if minutes >= 30:
        native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
            replacing_generation=native.generation), role="desktop")
    snapshot = coordinator.read(native)
    assert snapshot["next_task"] == "account-connection"
    assert snapshot["decision"] is None and "account.confirm" not in snapshot["allowed_actions"]
    assert snapshot["authentication"]["actions"]["select"]
    assert snapshot["authentication"]["operation_id"] is None
    assert "account.connect" in snapshot["allowed_actions"]
    with pytest.raises(CommandRefused):
        coordinator._accept(native, stale_click)
    selected, fresh = coordinator._accept(native, command(snapshot, "account.connect") | {"provider": "default"})
    assert fresh and json.loads(selected["owner_scope_json"])["browser"]["creator_account_id"] == scope["creator_account_id"]
    assert len(initial.client.preparations) == 1
    assert not any(verb == "submit" for verb, _ in decisions.client.calls)
    coordinator.owner.registered = None
    await coordinator.owner.stop()
