import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.authentication import DesktopAuthentication
from app.onboarding.coordinator import OperationResult
from app.onboarding.decisions import DesktopDecisions
from app.persistence.onboarding import OnboardingJourneyStore
from app.provisioning.events import events
from test_desktop_browser_tasks import initial_ceremony_case, setup as setup
from test_desktop_coordinator import _until, command
from test_desktop_decisions import DecisionClient, authentication_projection

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


async def confirmation(setup, remaining):
    coordinator, _, _, clock = setup
    source = OnboardingJourneyStore(coordinator.store).open()
    clock[0] += timedelta(seconds=1800 - remaining)
    coordinator, initial, client, operation, scope, clock, native = await initial_ceremony_case(setup)
    assert scope["source_journey_id"] == source["journey_id"]
    client.mode, client.state = "live", "active"
    tasks, ceremonies = coordinator.owner.browser_tasks, coordinator.owner.ceremonies
    with coordinator.store.database.read() as connection:
        access = connection.execute("SELECT operation_id FROM desktop_setup_operations WHERE action='browser.access'").fetchone()[0]
    tasks.complete(access)
    reads = []
    row = ceremonies.row(operation["operation_id"])

    def read(*args, before_send, **kwargs):
        before_send()
        reads.append("authentication")
        return authentication_projection(row)

    authentication = DesktopAuthentication(coordinator.store, ceremonies,
        SimpleNamespace(read=read, transport=SimpleNamespace()))
    ceremonies.authentication = authentication
    authentication._save(row, authentication_projection(row))
    decisions = DesktopDecisions(coordinator.store, ceremonies, DecisionClient())
    ceremonies.decisions = decisions
    await decisions.refresh(operation["operation_id"], authorize=lambda: None)
    coordinator._settle(operation, OperationResult("waiting"))
    return coordinator, initial, client, operation, clock, native, reads


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [57, 82, 300])
async def test_confirmation_and_wait_survive_presentation_and_source_boundary(setup, remaining):
    coordinator, initial, client, operation, clock, native, reads = await confirmation(setup, remaining)
    owner, opened = coordinator.owner, []
    deadline = clock[0].timestamp() + 600

    async def wait(current):
        opened.append(current["operation_id"])
        yield (1, "active", "awaiting-confirmation")
        await asyncio.Event().wait()

    owner.wait = wait
    coordinator.start()
    try:
        await _until(lambda: coordinator._held and not coordinator._workers)
        await coordinator.drain()
        assert owner.confirmation_deadline(operation) == deadline
        assert owner._guard(operation).expires_at.timestamp() >= deadline
        before = len(reads), len(client.calls), len(owner.ceremonies.decisions.client.calls)
        preparations = len(initial.client.preparations)
        clock[0] += timedelta(seconds=remaining + 1)
        replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
            replacing_generation=native.generation), role="desktop")
        coordinator._waits[operation["operation_id"]].cancel()
        events.publish()
        await _until(lambda: len(opened) == 2 and coordinator._held and not coordinator._workers)
        assert coordinator.read(replacement)["decision"] is not None
        assert owner.confirmation_deadline(operation) == deadline
        after = len(reads), len(client.calls), len(owner.ceremonies.decisions.client.calls)
        assert all(new - old <= 1 for old, new in zip(before, after))
        clock[0] += timedelta(seconds=598 - remaining)
        events.publish()
        await asyncio.sleep(0.1)
        assert (len(reads), len(client.calls), len(owner.ceremonies.decisions.client.calls)) == after
        snapshot = coordinator.read(replacement)
        assert snapshot["decision"] is not None
        clicked = command(snapshot, "account.confirm")
        await coordinator.command(replacement, clicked)
        await coordinator.drain()
        assert coordinator.receipt(replacement, clicked["operation_id"])["state"] == "completed"
        assert [verb for verb, _ in owner.ceremonies.decisions.client.calls].count("submit") == 1
        assert len(initial.client.preparations) == preparations
    finally:
        await coordinator.stop()
        owner.registered = None
        await owner.stop()


@pytest.mark.asyncio
async def test_read_reconnect_and_confirmation_click_never_refresh_a_prepared_source(setup):
    coordinator, initial, client, operation, clock, native, _ = await confirmation(setup, 82)
    owner = coordinator.owner
    source = owner._guard(operation)
    clock[0] += timedelta(seconds=20)
    try:
        for _ in range(3):
            coordinator.read(native)
            owner.probe_interval(operation)
            owner.confirmation_deadline(operation)
            await owner.execute(operation, reconcile=True)
        assert owner._guard(operation).expires_at == source.expires_at
        request = command(coordinator.read(native), "account.confirm")
        await coordinator.command(native, request)
        await coordinator.drain()
        assert coordinator.receipt(native, request["operation_id"])["state"] == "completed"
        assert owner._guard(operation).expires_at == source.expires_at
        assert len(initial.client.preparations) == 1
    finally:
        await coordinator.stop()
        owner.registered = None
        await owner.stop()
