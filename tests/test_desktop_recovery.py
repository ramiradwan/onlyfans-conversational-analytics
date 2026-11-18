import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding import coordinator as module, timing
from app.provisioning.events import events
from test_desktop_coordinator import Hints, _hint, _until, _waited_connect, setup as scheduler_setup
from test_desktop_decisions import authentication_projection
from test_desktop_lifetimes import confirmation, setup as setup

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class VirtualTime:
    def __init__(self, monkeypatch, clock):
        self.seconds, self.sleepers = 0, []
        self.clock = clock
        monkeypatch.setattr(module, "asyncio", self)

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def get_running_loop(self):
        return SimpleNamespace(time=lambda: self.seconds)

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.seconds + delay, future, asyncio.current_task()))
        await future

    async def wait_for(self, awaitable, timeout):
        signal, timer = asyncio.create_task(awaitable), asyncio.create_task(self.sleep(timeout))
        try:
            done, _ = await asyncio.wait((signal, timer), return_when=asyncio.FIRST_COMPLETED)
            if signal in done:
                return signal.result()
            raise TimeoutError
        finally:
            signal.cancel()
            timer.cancel()
            await asyncio.gather(signal, timer, return_exceptions=True)

    async def advance(self, seconds, coordinator):
        self.seconds += seconds
        self.clock[0] += timedelta(seconds=seconds)
        woken = {task for due, future, task in self.sleepers if due <= self.seconds and not future.done()}
        for due, future, _ in self.sleepers:
            if due <= self.seconds and not future.done():
                future.set_result(None)
        self.sleepers = [entry for entry in self.sleepers if not entry[1].done()]
        await asyncio.sleep(0.02)
        await coordinator.drain()
        await asyncio.sleep(0.02)
        # Off-loop work can keep a woken task from sleeping again within the fixed pauses on a loaded host.
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline and any(
            not task.done() and all(entry[2] is not task for entry in self.sleepers) for task in woken
        ):
            await asyncio.sleep(0.01)
            await coordinator.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["deadline", "server"])
async def test_ended_decision_is_idle_for_ten_minutes_with_the_old_session_deadline(setup, monkeypatch, end):
    coordinator, tasks, native, clock = setup
    verified_at = clock[0]
    clock[0] += timedelta(seconds=1683)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
        replacing_generation=native.generation), role="desktop")
    coordinator, initial, client, operation, clock, native, reads = await confirmation(
        (coordinator, tasks, native, clock), 1700)
    assert (clock[0] - verified_at).total_seconds() == 1783
    owner, decisions = coordinator.owner, coordinator.owner.ceremonies.decisions
    row = owner.ceremonies.row(operation["operation_id"])
    session_end = verified_at + timedelta(seconds=1800)
    original = decisions.client.read

    def decision_read(request, **kwargs):
        if clock[0] >= session_end:
            decisions.client.state = "awaiting-authentication"
        value = original(request, **kwargs)
        if value["state"] == "confirmation-required":
            value["decision"]["expires_at"] = session_end.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return value

    authentication = owner.ceremonies.authentication
    original_authentication = authentication.client.read

    def authentication_read(*args, **kwargs):
        value = original_authentication(*args, **kwargs)
        if clock[0] >= session_end or decisions.client.state == "unavailable":
            value["state"] = "reauthentication-required"
            value["actions"]["select"] = True
        return value

    decisions.client.read = decision_read
    authentication.client.read = authentication_read
    await decisions.refresh(operation["operation_id"], authorize=lambda: None)
    hints = Hints((_hint(1, status="awaiting-confirmation"), "hold"))
    owner.wait = hints
    virtual = VirtualTime(monkeypatch, clock)
    coordinator.start()
    try:
        await _until(lambda: coordinator._held and not coordinator._workers
            and operation["operation_id"] in coordinator._confirmation_at
            and any(due <= 17 and not future.done() for due, future, _ in virtual.sleepers))
        passes, pass_facts = [], coordinator._pass_facts

        def counted():
            passes.append(True)
            return pass_facts()

        coordinator._pass_facts = counted
        before = len(reads)
        if end == "server":
            decisions.client.state = "unavailable"
            coordinator._reconcile_soon(operation, frame=_hint(2, status="unavailable"))
        else:
            await virtual.advance(18, coordinator)
        await _until(lambda: authentication._projection(operation["operation_id"])["state"] == "reauthentication-required"
            and not coordinator._workers)
        await coordinator.drain()
        await asyncio.sleep(0.04)
        if end == "deadline":
            native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
                replacing_generation=native.generation), role="desktop")
        snapshot = coordinator.read(native)
        assert snapshot["decision"] is None
        assert snapshot["authentication"]["actions"]["select"]
        # Every retry would perform these three signed protected reads.
        after_end = len(reads), len(client.calls), len(decisions.client.calls)
        settled_passes = len(passes)
        for _ in range(40):
            await virtual.advance(15, coordinator)
        assert len(reads) - before == 1
        assert (len(reads), len(client.calls), len(decisions.client.calls)) == after_end
        assert len(passes) - settled_passes <= 1
        assert not coordinator._waits and not coordinator._prefetches
        # A changed local fact still settles immediately, without a timer.
        owner.observe = lambda _: module.OperationResult("completed")
        events.publish()
        await _until(lambda: not coordinator._pending())
    finally:
        await coordinator.stop()
        owner.registered = None
        await owner.stop()


@pytest.mark.asyncio
async def test_live_confirmation_failure_recovery_uses_bounded_backoff(scheduler_setup, monkeypatch):
    coordinator, owner, _, clock = scheduler_setup
    calls = []
    virtual = VirtualTime(monkeypatch, clock)

    async def failed(operation):
        calls.append(virtual.seconds)
        raise OSError("wait failed")
        yield

    await _waited_connect(scheduler_setup, failed)
    deadline = coordinator.admissions._now() + 601
    owner.confirmation_deadline = lambda _: deadline
    try:
        await _until(lambda: calls and not coordinator._workers)
        baseline = len(owner.calls)
        for _ in range(40):
            await virtual.advance(15, coordinator)
        recovery = [reconcile for _, reconcile in owner.calls].count(True)
        assert calls == [0, 15, 45, 105, 165, 225, 285, 345, 405, 465, 525, 585]
        assert recovery == 12
        assert len(owner.calls) - baseline <= 11
        # A new frame does not wait for the recovery timer.
        coordinator._reconcile_soon(coordinator._pending()[0], frame=_hint(2))
        await coordinator.drain()
        assert [reconcile for _, reconcile in owner.calls].count(True) == recovery + 1
    finally:
        await coordinator.stop()


def test_wait_eligibility_marks_only_record_value_changes(scheduler_setup, caplog):
    coordinator, owner, _, _ = scheduler_setup
    caplog.set_level(logging.INFO, logger=timing.__name__)
    owner.probe = lambda _: None
    interval, deadline = [None], [None]
    owner.probe_interval = lambda _: interval[0]
    owner.confirmation_deadline = lambda _: deadline[0]
    operation = {"operation_id": "local-test-operation"}
    for new_interval, new_deadline in [(None, None), (2, coordinator.admissions._now() + 600), (None, None)]:
        interval[0], deadline[0] = new_interval, new_deadline
        for _ in range(100):
            coordinator._probe_interval(operation)
            coordinator._confirmation_deadline(operation)
    messages = [record.getMessage() for record in caplog.records]
    for phase, count in [("eligible", 1), ("ineligible", 2), ("confirmation-live", 1), ("confirmation-absent", 2)]:
        assert sum(f"phase={phase} " in message for message in messages) == count


@pytest.mark.asyncio
async def test_failed_reconciliation_without_a_wait_uses_bounded_backoff(scheduler_setup, monkeypatch):
    from test_desktop_coordinator import command
    coordinator, owner, native, clock = scheduler_setup
    owner.facts["browser_access"] = "verified"
    owner.automatic_recovery = lambda _: True
    owner.result = OSError("read failed")
    virtual = VirtualTime(monkeypatch, clock)
    coordinator.start()
    await coordinator.command(native, command(coordinator.read(native), "account.connect"))
    await coordinator.drain()
    try:
        for _ in range(40):
            await virtual.advance(15, coordinator)
        assert [reconcile for _, reconcile in owner.calls].count(True) == 11
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("live", False), ("expired", True), ("authentication-expired", True), ("reauthentication", True),
    ("unconfirmed", False), ("accepted", False), ("open-attempt", False), ("closure", False),
])
async def test_idle_classification_preserves_unconfirmed_and_explicit_recovery(setup, monkeypatch, case, expected):
    from app.onboarding.runtime import RuntimeSetupOwner
    coordinator, _, _, operation, clock, _, _ = await confirmation(setup, 1700)
    owner, ceremonies = coordinator.owner, coordinator.owner.ceremonies
    authentication, decisions = ceremonies.authentication, ceremonies.decisions
    row = ceremonies.row(operation["operation_id"])
    try:
        if case in {"expired", "open-attempt", "closure"}:
            value = authentication_projection(row)
            value["expires_at"] = (clock[0] + timedelta(seconds=1800)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            authentication._save(row, value)
            await decisions.refresh(operation["operation_id"], authorize=lambda: None)
            clock[0] += timedelta(seconds=601)
        if case == "authentication-expired":
            decisions.client.state = "unavailable"
            await decisions.refresh(operation["operation_id"], authorize=lambda: None)
            clock[0] += timedelta(seconds=601)
        if case == "reauthentication":
            authentication._save(row, authentication_projection(row, "reauthentication-required"))
        if case in {"unconfirmed", "accepted"}:
            decisions.client.state = "unavailable" if case == "unconfirmed" else "accepted"
            await decisions.refresh(operation["operation_id"], authorize=lambda: None)
            if case == "accepted":
                clock[0] += timedelta(seconds=601)
        if case == "open-attempt":
            monkeypatch.setattr(authentication, "can_open", lambda *args, **kwargs: True)
        if case == "closure":
            monkeypatch.setattr(authentication, "_closures", lambda *args: [object()])
        assert owner.waiting_for_sign_in(operation) is expected
        runtime = RuntimeSetupOwner.__new__(RuntimeSetupOwner)
        runtime.setup_owner, runtime.activation = owner, SimpleNamespace(ceremonies=ceremonies)
        assert runtime.waiting_for_sign_in(operation) is expected
        assert runtime.waiting_for_sign_in(operation | {"action": "activation.review"}) is expected
        assert not runtime.waiting_for_sign_in(operation | {"action": "account.confirm"})
    finally:
        await coordinator.stop()
        owner.registered = None
        await owner.stop()


@pytest.mark.asyncio
async def test_ineligible_eof_and_prefetch_do_not_start_another_read(scheduler_setup):
    from test_desktop_coordinator import command
    coordinator, owner, native, _ = scheduler_setup
    owner.facts["browser_access"] = "verified"
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    live, prefetched = [True], []
    owner.probe = lambda _: None
    owner.probe_interval = lambda _: 2 if live[0] else None

    async def ended(operation):
        live[0] = False
        return
        yield

    async def prefetch(operation):
        prefetched.append(True)

    owner.wait, owner.prefetch = ended, prefetch
    await coordinator._run_wait(operation)
    await coordinator.drain()
    coordinator._prefetch(operation)
    await asyncio.sleep(0.03)
    assert not owner.calls and not prefetched
    await coordinator.stop()


def test_unavailable_idle_classification_keeps_domain_recovery(scheduler_setup):
    from test_desktop_coordinator import command
    coordinator, owner, native, _ = scheduler_setup
    owner.facts["browser_access"] = "verified"
    coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    owner.automatic_recovery = lambda _: True

    def unavailable(operation):
        raise OSError("local read unavailable")

    owner.waiting_for_sign_in = unavailable
    assert coordinator._pass_facts()[0][1] is True


@pytest.mark.asyncio
async def test_failed_worker_retains_its_recovery_deadline(scheduler_setup):
    from test_desktop_coordinator import command
    coordinator, owner, native, _ = scheduler_setup
    owner.facts["browser_access"] = "verified"
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))

    async def failed(*args, **kwargs):
        raise OSError("settlement unavailable")

    coordinator._run = failed
    coordinator._start(operation, reconcile=True, actor=None)
    await coordinator.drain()
    await asyncio.sleep(0)
    assert operation["operation_id"] in coordinator._retry_at
    await coordinator.stop()
