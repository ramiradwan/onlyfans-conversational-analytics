import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.onboarding.contract import PROFILE
from app.onboarding.coordinator import (
    FACTS, AcceptedContext, CommandRefused, DesktopOnboardingCoordinator, DomainView, OperationResult,
)
from app.persistence.auth import SQLiteAuthenticationStore

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class Owner:
    def __init__(self):
        self.facts = dict.fromkeys(FACTS, "missing")
        self.scope = {"account_generation": 1, "consent_generation": 1}
        self.calls = []
        self.admissions = []
        self.result = OperationResult("completed")
        self.started = asyncio.Event()
        self.reconciled = asyncio.Event()
        self.release = None
        self.reason = "none"

    def observe(self, operation):
        return None

    def read(self, journey_id, *, actor=None, connection=None):
        return DomainView(dict(self.facts), dict(self.scope), self.reason)

    def admit(self, connection, journey_id, *, action, scope, actor=None):
        journey = journey_id or str(uuid4())
        self.admissions.append(journey)
        return AcceptedContext(journey, dict(scope))

    async def execute(self, operation, *, reconcile, actor=None):
        self.calls.append((dict(operation), reconcile))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        if reconcile:
            self.reconciled.set()
        return self.result


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: now[0])
    admissions = ScopedAdmissions(store, stage="setup")
    session = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    return coordinator, owner, session, now


def command(snapshot, action="browser.access"):
    return {"profile": PROFILE, "kind": "command", "journey_id": snapshot["journey_id"],
            "operation_id": str(uuid4()), "expected": {name: snapshot[name] for name in (
                "generation", "revision", "scope_digest")}, "action": action}


def table_counts(coordinator):
    with coordinator.store.database.read() as connection:
        return {table: connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ("desktop_setup_journeys", "desktop_setup_operations", "desktop_browser_tasks", "onboarding_journeys")}


def test_reads_do_not_create_journeys_tasks_operations_or_owner_work(setup):
    coordinator, owner, session, _ = setup
    before = table_counts(coordinator)
    first = coordinator.read(session)
    assert first["next_task"] == "browser-access"
    assert first["allowed_actions"] == ["browser.access"]
    for _ in range(5):
        assert coordinator.read(session) == first
    assert table_counts(coordinator) == before
    assert not owner.calls and not owner.admissions


@pytest.mark.parametrize("activation", ["missing", "unknown", "verified"])
@pytest.mark.parametrize("browser", ["unknown", "missing", "paused", "verified"])
def test_completed_local_setup_is_independent_of_activation_and_browser_connection(setup, activation, browser):
    coordinator, owner, session, _ = setup
    owner.facts = dict.fromkeys(FACTS, "verified")
    owner.facts.update(activation=activation, analysis="unknown", browser_access=browser)
    snapshot = coordinator.read(session)
    assert snapshot["next_task"] == "complete" and snapshot["local_ready"]
    assert not snapshot["full_ready"]
    assert snapshot["allowed_actions"] == ([] if activation == "verified" else ["activation.review"])


@pytest.mark.parametrize("changes,next_task,actions", [
    ({"browser_access": "verified"}, "account-connection", ["account.connect"]),
    ({"browser_access": "paused"}, "account-connection", ["account.connect"]),
    ({"browser_access": "verified", "installation": "verified", "account": "verified"}, "finish-setup", ["setup.finalize"]),
    ({"browser_access": "verified", "installation": "verified", "account": "verified", "configuration": "verified"}, "finish-setup", []),
    ({"browser_access": "verified", "installation": "verified", "account": "verified", "configuration": "verified", "enrollment": "verified"}, "finish-setup", ["browser.pair"]),
])
def test_next_requirement_preserves_completed_domain_facts(setup, changes, next_task, actions):
    coordinator, owner, session, _ = setup
    owner.facts.update(changes)
    snapshot = coordinator.read(session)
    assert snapshot["next_task"] == next_task and snapshot["allowed_actions"] == actions


@pytest.mark.asyncio
async def test_duplicate_clicks_have_one_durable_operation_and_one_worker(setup):
    coordinator, owner, session, _ = setup
    owner.release = asyncio.Event()
    request = command(coordinator.read(session))
    first, duplicate = await asyncio.gather(coordinator.command(session, request), coordinator.command(session, request))
    assert first == duplicate and first["state"] == "accepted"
    await owner.started.wait()
    assert len(owner.calls) == len(owner.admissions) == 1
    assert table_counts(coordinator)["desktop_setup_operations"] == 1
    assert not coordinator.read(session)["allowed_actions"]
    owner.release.set()
    await coordinator.drain()
    assert coordinator.receipt(session, request["operation_id"])["state"] == "completed"


@pytest.mark.asyncio
async def test_only_a_fresh_acceptance_is_announced_and_an_announcement_failure_changes_nothing(setup):
    coordinator, owner, session, _ = setup
    announced = []

    def accepted(operation):
        announced.append(operation["operation_id"])
        raise RuntimeError("presentation unavailable")
    coordinator.accepted = accepted
    request = command(coordinator.read(session))
    first = await coordinator.command(session, request)
    # A duplicate reads the same operation's current receipt. Its worker may have advanced it meanwhile.
    again = await coordinator.command(session, request)
    assert again["operation_id"] == first["operation_id"] and again["state"] in {"accepted", "completed"}
    await coordinator.drain()
    assert announced == [request["operation_id"]]
    assert coordinator.receipt(session, request["operation_id"])["state"] == "completed"
    assert len(owner.calls) == 1

@pytest.mark.asyncio
async def test_a_command_is_accepted_off_the_event_loop_and_a_cancelled_request_still_starts_it(setup):
    import threading
    coordinator, owner, session, _ = setup
    loop_thread, on_loop = threading.get_ident(), []
    accept, entered, release = coordinator._accept, threading.Event(), threading.Event()

    def held(*args, **kwargs):
        on_loop.append(threading.get_ident() == loop_thread)
        entered.set()
        release.wait(5)
        return accept(*args, **kwargs)

    request = command(coordinator.read(session))
    coordinator._accept = held
    pending = asyncio.create_task(coordinator.command(session, request))
    assert await asyncio.to_thread(entered.wait, 5)
    # The HTTP request goes away while its accept is in flight.
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    while coordinator._accepting:
        await asyncio.sleep(0.01)
    await coordinator.drain()
    assert on_loop == [False]
    assert coordinator.receipt(session, request["operation_id"])["state"] == "completed"
    assert len(owner.calls) == 1


@pytest.mark.asyncio
async def test_lost_reply_recovers_receipt_without_repeating_mutation(setup):
    coordinator, owner, session, _ = setup
    request = command(coordinator.read(session))
    await coordinator.command(session, request)
    await coordinator.drain()
    reopened = DesktopOnboardingCoordinator(coordinator.admissions, owner)
    receipt = await reopened.command(session, request)
    assert receipt["state"] == "completed" and len(owner.calls) == 1


@pytest.mark.asyncio
async def test_worker_failure_is_unknown_and_restart_reconciles_original_operation(setup):
    coordinator, owner, session, _ = setup
    owner.result = OSError("lost committed result")
    request = command(coordinator.read(session))
    await coordinator.command(session, request)
    await coordinator.drain()
    assert coordinator.receipt(session, request["operation_id"])["state"] == "outcome_unknown"
    reopened = DesktopOnboardingCoordinator(coordinator.admissions, owner)
    current = reopened.read(session)
    assert not current["allowed_actions"]
    with pytest.raises(CommandRefused, match="command was refused"):
        await reopened.command(session, command(current))
    owner.result = OperationResult("completed")
    reopened.start()
    await reopened.drain()
    await reopened.stop()
    assert [(row["operation_id"], recover) for row, recover in owner.calls] == [
        (request["operation_id"], False), (request["operation_id"], True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,task,verified", [
    ("account.connect", "account-connection", {"browser_access"}),
    ("setup.finalize", "finish-setup", {"installation", "account"}),
    ("browser.pair", "finish-setup", {"installation", "account", "configuration", "enrollment"}),
])
async def test_replacement_presentation_keeps_the_accepted_task_without_inventing_authority(setup, action, task, verified):
    coordinator, owner, session, _ = setup
    owner.facts.update(dict.fromkeys(verified, "verified"))
    owner.result = OperationResult("outcome_unknown", "authority_unavailable")
    request = command(coordinator.read(session), action)
    await coordinator.command(session, request)
    await coordinator.drain()
    # The old browser observation is no longer admitted after reopening, and
    # domain facts may be unavailable. Neither loss changes the accepted task.
    owner.facts = dict.fromkeys(FACTS, "missing")
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    reopened = DesktopOnboardingCoordinator(coordinator.admissions, owner)
    current = reopened.read(replacement)
    assert current["next_task"] == task
    assert current["facts"] == owner.facts
    assert current["allowed_actions"] == []
    assert not current["local_ready"] and not current["full_ready"]
    assert [item["operation_id"] for item in current["pending_operations"]] == [request["operation_id"]]
    assert len(owner.calls) == len(owner.admissions) == 1


@pytest.mark.asyncio
async def test_transient_outage_recovers_accepted_operation_without_a_page_action(setup, monkeypatch):
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.01)
    coordinator, owner, session, _ = setup
    owner.automatic_recovery = lambda operation: operation["action"] == "account.connect"
    owner.facts["browser_access"] = "verified"
    owner.result = OSError("network unavailable")
    request = command(coordinator.read(session), "account.connect")
    coordinator.start()
    try:
        await coordinator.command(session, request)
        await owner.started.wait()
        assert coordinator.receipt(session, request["operation_id"])["state"] == "outcome_unknown"
        owner.result = OperationResult("completed")
        async with asyncio.timeout(2):
            while coordinator.receipt(session, request["operation_id"])["state"] != "completed":
                await asyncio.sleep(0.01)
        recovered_calls = len(owner.calls)
        assert recovered_calls >= 2
        assert [row["operation_id"] for row, _ in owner.calls] == [request["operation_id"]] * recovered_calls
        assert [recover for _, recover in owner.calls] == [False] + [True] * (recovered_calls - 1)
        assert len(owner.admissions) == 1
        await asyncio.sleep(0.03)
        assert len(owner.calls) == recovered_calls
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_recovery_scheduler_does_not_repeat_actions_without_domain_permission(setup, monkeypatch):
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.01)
    coordinator, owner, session, _ = setup
    owner.result = OperationResult("outcome_unknown", "operation_unconfirmed")
    owner.automatic_recovery = lambda _: False
    coordinator.start()
    try:
        await asyncio.sleep(0.02)
        assert not owner.calls
        await coordinator.command(session, command(coordinator.read(session)))
        await coordinator.drain()
        await asyncio.sleep(0.04)
        assert len(owner.calls) == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_replacing_presentation_fences_ui_but_not_already_accepted_work(setup):
    coordinator, owner, session, _ = setup
    owner.release = asyncio.Event()
    request = command(coordinator.read(session))
    await coordinator.command(session, request)
    await owner.started.wait()
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    with pytest.raises(AdmissionRefused):
        coordinator.read(session)
    assert coordinator.read(replacement)["pending_operations"][0]["operation_id"] == request["operation_id"]
    owner.release.set()
    await coordinator.drain()
    assert coordinator.receipt(replacement, request["operation_id"])["state"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account_generation", "consent_generation", "facts", "reason"])
async def test_stale_snapshot_refuses_before_any_owner_mutation(setup, change):
    coordinator, owner, session, _ = setup
    request = command(coordinator.read(session))
    if change in owner.scope:
        owner.scope[change] += 1
    elif change == "facts":
        owner.facts["browser_access"] = "verified"
    else:
        owner.reason = "account_mismatch"
    with pytest.raises(CommandRefused) as error:
        await coordinator.command(session, request)
    assert error.value.reason == "setup_changed"
    assert not owner.calls and not owner.admissions and not any(table_counts(coordinator).values())


@pytest.mark.asyncio
async def test_operation_id_cannot_change_action_or_journey(setup):
    coordinator, _, session, _ = setup
    request = command(coordinator.read(session))
    await coordinator.command(session, request)
    await coordinator.drain()
    for changed in ({"action": "account.connect"}, {"journey_id": str(uuid4())}):
        with pytest.raises(CommandRefused) as error:
            await coordinator.command(session, request | changed)
        assert error.value.reason == "operation_conflict"


@pytest.mark.asyncio
async def test_expiry_does_not_erase_unresolved_receipt_or_create_replacement(setup):
    coordinator, owner, session, now = setup
    owner.result = OperationResult("waiting")
    request = command(coordinator.read(session))
    await coordinator.command(session, request)
    await coordinator.drain()
    now[0] += timedelta(minutes=31)
    with pytest.raises(AdmissionRefused):
        coordinator.read(session)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    snapshot = coordinator.read(replacement)
    assert snapshot["pending_operations"][0]["state"] == "waiting"
    assert not snapshot["allowed_actions"]
    assert table_counts(coordinator)["desktop_setup_operations"] == 1


def test_closed_command_contract_rejects_authority_or_arbitrary_actions(setup):
    from app.onboarding.contract import validate
    coordinator, _, session, _ = setup
    request = command(coordinator.read(session))
    for changed in ({"approved": True}, {"url": "https://elsewhere.test"}, {"action": "accept-consent"},
                    {"expected": request["expected"] | {"revision": True}}):
        with pytest.raises(ValueError):
            validate(request | changed, kind="command")


def authentication_operations(setup, parent_action="account.connect", action="auth.select", relation=None):
    coordinator, owner, session, _ = setup
    owner.facts["browser_access"] = "missing" if parent_action == "browser.access" else "verified"
    if parent_action == "activation.review":
        owner.facts = dict.fromkeys(FACTS, "verified")
        owner.facts["activation"] = "missing"
    parent, _ = coordinator._accept(session, command(coordinator.read(session), parent_action))
    if relation == "wrong-journey":
        # Add a separate accepted journal; never alter an operation's immutable
        # scope to manufacture this foreign-parent recovery fixture.
        parent = {**parent, "operation_id": str(uuid4()), "journey_id": str(uuid4())}
        with coordinator.store.database.transaction() as connection:
            connection.execute("INSERT INTO desktop_setup_journeys(journey_id,revision,created_at) VALUES (?,0,0)",
                (parent["journey_id"],))
            connection.execute("INSERT INTO desktop_setup_operations(" + ",".join(parent) + ") VALUES (" +
                ",".join("?" for _ in parent) + ")", tuple(parent.values()))
    child_id = str(uuid4())
    owner.scope["desktop_authentication"] = {
        "operation_id": child_id if relation == "self" else parent["operation_id"], "browser_operation_id": None,
        "state": "selection-required" if action == "auth.select" else "admitted",
        "choices": dict.fromkeys(("google", "microsoft-personal", "telegram", "tiktok", "default"), True),
        "actions": {"select": action == "auth.select", "cancel": action == "auth.cancel",
            "switch_account": action == "auth.switch", "open_browser": False},
        "authentication_attempt": None if action == "auth.select" else {
            "authentication_attempt_id": "01900000-0000-7000-8000-000000000001",
            "provider": "google", "mode": "sign-in"},
    }
    request = command(coordinator.read(session), action)
    request["operation_id"] = child_id
    if action != "auth.cancel":
        request["provider"] = "google"
    child, _ = coordinator._accept(session, request)
    coordinator._settle(parent, OperationResult("waiting"))
    coordinator._settle(child, OperationResult("waiting"))
    return parent, child


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_action", ["account.connect", "activation.review"])
@pytest.mark.parametrize("action", ["auth.select", "auth.cancel", "auth.switch"])
async def test_authentication_child_promptly_reads_only_its_original_parent(setup, parent_action, action):
    coordinator, owner, session, _ = setup
    parent, child = authentication_operations(setup, parent_action, action)
    before = table_counts(coordinator)
    calls = []
    owner.automatic_recovery = lambda operation: operation["operation_id"] == parent["operation_id"]

    async def execute(operation, *, reconcile, actor=None):
        calls.append((operation["operation_id"], reconcile, actor))
        return OperationResult("completed" if operation["operation_id"] == child["operation_id"] else "waiting")

    owner.execute = execute
    coordinator._retry_at[parent["operation_id"]] = asyncio.get_running_loop().time() + 60
    actor = object()
    await coordinator.reconcile(session, child["operation_id"], actor=actor)
    await coordinator.drain()
    assert calls == [(child["operation_id"], True, actor), (parent["operation_id"], True, None)]
    assert coordinator.receipt(session, child["operation_id"])["state"] == "completed"
    assert coordinator.receipt(session, parent["operation_id"])["state"] == "waiting"
    assert table_counts(coordinator) == before
    assert len(owner.admissions) == 2
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", ["completed", "refused", "wrong-journey", "wrong-action",
    "missing", "self", "not-recoverable", "recovery-error", "closed", "unconfirmed", "transport-error"])
async def test_authentication_followup_leaves_finished_or_unrelated_parent_untouched(setup, ineligible):
    coordinator, owner, session, _ = setup
    parent, child = authentication_operations(setup,
        parent_action="browser.access" if ineligible == "wrong-action" else "account.connect", relation=ineligible)
    owner.automatic_recovery = lambda _: ineligible != "not-recoverable"
    if ineligible == "recovery-error":
        def unavailable(_):
            raise OSError("Authority unavailable")
        owner.automatic_recovery = unavailable
    with coordinator.store.database.transaction() as connection:
        if ineligible in {"completed", "refused"}:
            connection.execute("UPDATE desktop_setup_operations SET state=? WHERE operation_id=?",
                (ineligible, parent["operation_id"]))
        elif ineligible == "missing":
            connection.execute("DELETE FROM desktop_setup_operations WHERE operation_id=?", (parent["operation_id"],))
        before = [dict(row) for row in connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id<>?", (child["operation_id"],))]
    owner.result = (OSError("Lost reply") if ineligible == "transport-error" else
        OperationResult("outcome_unknown", "operation_unconfirmed") if ineligible == "unconfirmed" else OperationResult("completed"))
    await coordinator.reconcile(session, child["operation_id"])
    if ineligible == "closed":
        await coordinator.stop()
    else:
        await coordinator.drain()
    assert [row["operation_id"] for row, _ in owner.calls] == [child["operation_id"]]
    with coordinator.store.database.read() as connection:
        after = [dict(row) for row in connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id<>?", (child["operation_id"],))]
    assert after == before
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["refresh", "close", "parent-completed"])
async def test_authentication_refresh_waits_for_inflight_parent_and_respects_close(setup, ending):
    coordinator, owner, session, _ = setup
    parent, child = authentication_operations(setup)
    owner.automatic_recovery = lambda _: True
    started, release, admitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    parent_reads = []

    async def execute(operation, *, reconcile, actor=None):
        if operation["operation_id"] == child["operation_id"]:
            admitted.set()
            return OperationResult("completed")
        parent_reads.append((admitted.is_set(), reconcile, actor))
        if len(parent_reads) == 1:
            started.set()
            await release.wait()
        return OperationResult("completed" if ending == "parent-completed" else "waiting")

    owner.execute = execute
    await coordinator.reconcile(session, parent["operation_id"])
    await started.wait()
    await coordinator.reconcile(session, child["operation_id"])
    await admitted.wait()
    assert parent_reads == [(False, True, None)]
    draining = asyncio.create_task(coordinator.stop() if ending == "close" else coordinator.drain())
    await asyncio.sleep(0)
    assert not draining.done()
    release.set()
    await asyncio.wait_for(draining, 1)
    assert parent_reads == ([(False, True, None), (True, True, None)] if ending == "refresh" else [(False, True, None)])
    assert not coordinator._workers
    await coordinator.stop()


async def _probed_connect(setup, probe, *, interval=0.01, recoverable=True):
    coordinator, owner, session, _ = setup
    owner.facts["browser_access"] = "verified"
    owner.result = OperationResult("waiting")
    owner.automatic_recovery = lambda _: recoverable
    owner.probe_interval = lambda _: interval
    owner.probe = probe
    request = command(coordinator.read(session), "account.connect")
    coordinator.start()
    await coordinator.command(session, request)
    await coordinator.drain()
    return request


@pytest.mark.asyncio
async def test_open_sign_in_is_probed_and_reconciled_as_soon_as_its_outcome_changes(setup):
    coordinator, owner, session, _ = setup
    probes, changed = [], asyncio.Event()

    async def probe(operation):
        probes.append(operation["operation_id"])
        return changed.is_set()

    request = await _probed_connect(setup, probe)
    try:
        async with asyncio.timeout(2):
            while len(probes) < 3:
                await asyncio.sleep(0.005)
        # An unchanged probe never repeats the full owner reconciliation.
        assert [reconcile for _, reconcile in owner.calls] == [False]
        owner.result = OperationResult("completed")
        changed.set()
        async with asyncio.timeout(1):
            while coordinator.receipt(session, request["operation_id"])["state"] != "completed":
                await asyncio.sleep(0.005)
        assert [reconcile for _, reconcile in owner.calls] == [False, True]
        assert set(probes) == {request["operation_id"]}
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_failed_probe_falls_back_to_one_full_reconciliation_and_backs_off(setup, monkeypatch):
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.5)
    coordinator, owner, _, _ = setup
    probes = []

    async def probe(operation):
        probes.append(operation["operation_id"])
        raise OSError("authority unavailable")

    await _probed_connect(setup, probe)
    try:
        async with asyncio.timeout(1):
            while len(owner.calls) < 2:
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.1)
        assert len(probes) == 1
        assert [reconcile for _, reconcile in owner.calls] == [False, True]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", ["not-recoverable", "no-open-sign-in"])
async def test_probes_run_only_for_recoverable_work_with_an_open_sign_in(setup, ineligible):
    coordinator, owner, _, _ = setup
    probes = []

    async def probe(operation):
        probes.append(operation["operation_id"])
        return True

    await _probed_connect(setup, probe, interval=None if ineligible == "no-open-sign-in" else 0.01,
        recoverable=ineligible != "not-recoverable")
    try:
        await asyncio.sleep(0.1)
        assert not probes
        assert [reconcile for _, reconcile in owner.calls] == [False]
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_cancels_an_inflight_probe_without_starting_owner_work(setup):
    coordinator, owner, _, _ = setup
    started, release = asyncio.Event(), asyncio.Event()

    async def probe(operation):
        started.set()
        await release.wait()
        return True

    await _probed_connect(setup, probe)
    async with asyncio.timeout(1):
        await started.wait()
    await asyncio.wait_for(coordinator.stop(), 1)
    release.set()
    await asyncio.sleep(0.02)
    assert [reconcile for _, reconcile in owner.calls] == [False]
    assert not coordinator._workers

class Hints:
    """A scripted held wait: each connection yields its hints, then stays open or fails."""

    def __init__(self, *connections):
        self.connections = list(connections)
        self.opened = []
        self.hold = asyncio.Event()

    def __call__(self, operation):
        self.opened.append(asyncio.get_running_loop().time())
        script = self.connections.pop(0) if self.connections else ("hold",)
        return self._run(script)

    async def _run(self, script):
        for step in script:
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, asyncio.Event):
                await step.wait()
                continue
            if step == "hold":
                await self.hold.wait()
                return
            yield step


def _hint(revision, presentation="active", status="awaiting-authentication"):
    return (revision, presentation, status)


async def _waited_connect(setup, wait, *, probe=None):
    probes = []

    async def counted(operation):
        probes.append(operation["operation_id"])
        return False

    request = await _probed_connect(setup, probe or counted)
    setup[1].wait = wait
    return request, probes


async def _until(condition, timeout=2):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_wait_frame_starts_reconcile_and_never_settles_alone(setup):
    coordinator, owner, session, _ = setup
    request, _ = await _waited_connect(setup, Hints((_hint(1, "retired", "committed"), "hold")))
    try:
        await _until(lambda: [reconcile for _, reconcile in owner.calls] == [False, True])
        await _until(lambda: not coordinator._workers)
        # The frame said committed; the owner's proof read still says waiting.
        assert coordinator.receipt(session, request["operation_id"])["state"] == "waiting"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_healthy_wait_suppresses_probe_and_retry(setup, monkeypatch):
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.05)
    coordinator, owner, session, _ = setup
    hints = Hints((_hint(1), "hold"))
    request, probes = await _waited_connect(setup, hints)
    try:
        await _until(lambda: request["operation_id"] in coordinator._held)
        await asyncio.wait_for(owner.reconciled.wait(), 2)
        assert [reconcile for _, reconcile in owner.calls] == [False, True]
        settled = len(owner.calls)
        probes.clear()
        await asyncio.sleep(0.3)
        assert probes == []
        assert len(owner.calls) == settled
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", ["healthy", "frame", "expiry", "failure"])
async def test_idle_confirmation_wakes_only_on_frame_expiry_or_failed_wait(setup, monkeypatch, wake):
    import time
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.03)
    monkeypatch.setattr("app.onboarding.coordinator.WAIT_BACKOFF_CAP_SECONDS", 0.3)
    coordinator, owner, _, clock = setup
    changed = asyncio.Event()
    script = (changed, OSError("stream lost")) if wake == "failure" else (changed, _hint(2), "hold")
    hints = Hints((_hint(1), *script))
    prefetches, probes = [], []

    async def prefetch(operation):
        prefetches.append(True)
        return 0.01

    owner.prefetch = prefetch

    async def probe(operation):
        probes.append(True)
        raise OSError("wait unavailable")

    await _waited_connect(setup, hints, probe=probe)
    await _until(lambda: coordinator._held and not coordinator._workers)
    # Measure from the held wait.
    probes.clear()
    started, initial = time.monotonic(), clock[0]
    coordinator.store._clock = lambda: initial + timedelta(seconds=time.monotonic() - started)
    expiry = initial.timestamp() + (0.25 if wake == "expiry" else 10)
    owner.confirmation_deadline = lambda _: expiry if coordinator.admissions._now() < expiry else None
    owner.probe_interval = lambda _: 0.01 if coordinator.admissions._now() < expiry else None
    passes, original = [], coordinator._pass_facts

    def counted():
        passes.append(True)
        return original()

    coordinator._pass_facts = counted
    try:
        await asyncio.sleep(0.06)
        before, reads, prefetched = len(passes), len(owner.calls), len(prefetches)
        await asyncio.sleep(0.12)
        assert len(passes) == before
        assert len(owner.calls) == reads
        assert len(prefetches) == prefetched
        if wake == "healthy":
            return
        if wake in {"frame", "failure"}:
            changed.set()
        await _until(lambda: len(owner.calls) == reads + 1)
        await _until(lambda: not coordinator._workers)
        await asyncio.sleep(0.01)
        assert len(owner.calls) == reads + 1
        assert not probes
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_lost_wait_falls_back_to_probe_then_reconnects(setup, monkeypatch):
    monkeypatch.setattr("app.onboarding.coordinator.WAIT_BACKOFF_CAP_SECONDS", 0.3)
    coordinator, owner, session, _ = setup
    lose, reconnected_frame = asyncio.Event(), asyncio.Event()
    # A reconnect may open before the watcher gets CPU time. Keep its first
    # frame pending to exercise the fallback probe while the wait is unheld.
    hints = Hints((_hint(1), lose, OSError("stream lost")),
                  (reconnected_frame, _hint(2, "retired", "committed"), "hold"))
    request, probes = await _waited_connect(setup, hints)
    try:
        await _until(lambda: request["operation_id"] in coordinator._held)
        await _until(lambda: not coordinator._workers)
        before = [reconcile for _, reconcile in owner.calls].count(True)
        probes.clear()
        lose.set()
        # The probe resumes while the wait is down, then the reconnected wait reconciles.
        await _until(lambda: probes, timeout=1)
        assert request["operation_id"] not in coordinator._held
        assert set(probes) == {request["operation_id"]}
        await _until(lambda: len(hints.opened) >= 2, timeout=2)
        reconnected_frame.set()
        await _until(lambda: [reconcile for _, reconcile in owner.calls].count(True) > before, timeout=2)
        assert request["operation_id"] in coordinator._held
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_reconnect_honours_retry_after(setup):
    from app.security.browser_ceremony_wait import CeremonyWaitUnavailable
    coordinator, owner, session, _ = setup
    hints = Hints((CeremonyWaitUnavailable("busy", retry_after=0.3),), (_hint(1), "hold"))
    request, _ = await _waited_connect(setup, hints)
    try:
        await _until(lambda: len(hints.opened) >= 2, timeout=2)
        assert hints.opened[1] - hints.opened[0] >= 0.29
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_refused_wait_reads_the_proof_once_per_run_of_failures(setup, monkeypatch):
    from app.security.browser_ceremony_wait import CeremonyWaitUnavailable
    monkeypatch.setattr("app.onboarding.coordinator.WAIT_BACKOFF_CAP_SECONDS", 0.02)
    coordinator, owner, session, _ = setup
    refused = CeremonyWaitUnavailable("refused")
    second, framed, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    hints = Hints((refused,), (second, refused), (framed, _hint(1), lost, OSError("stream lost")),
                  (refused,), (refused,), ("hold",))
    await _waited_connect(setup, hints)

    def reads():
        return [reconcile for _, reconcile in owner.calls].count(True)

    try:
        await _until(lambda: reads() == 1 and not coordinator._workers)
        second.set()
        await _until(lambda: len(hints.opened) >= 3)
        await coordinator.drain()
        assert reads() == 1
        framed.set()
        await _until(lambda: reads() == 2 and not coordinator._workers)
        lost.set()
        await _until(lambda: len(hints.opened) >= 6)
        await coordinator.drain()
        assert reads() == 3
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_repeated_hints_are_ignored(setup):
    coordinator, owner, session, _ = setup
    hints = Hints((_hint(2), _hint(2), "hold"))
    request, _ = await _waited_connect(setup, hints)
    try:
        await _until(lambda: request["operation_id"] in coordinator._held)
        await _until(lambda: not coordinator._workers)
        await asyncio.sleep(0.05)
        assert [reconcile for _, reconcile in owner.calls].count(True) == 1
    finally:
        await coordinator.stop()


@pytest.mark.parametrize("case", json.loads((Path(__file__).resolve().parents[1]
    / "contracts/browser-ceremony-wait-v1/frame-cases.json").read_bytes()), ids=lambda case: case["case_id"])
async def test_every_published_wait_frame_starts_proof_reconciliation(setup, case):
    coordinator, owner, session, _ = setup
    frames = case["expected"]["frames"]
    opened, hold = asyncio.Event(), asyncio.Event()

    def reconciliations():
        return sum(reconcile for _, reconcile in owner.calls)

    async def wait(operation):
        opened.set()
        for frame in frames:
            before = reconciliations()
            yield tuple(frame)
            await _until(lambda: reconciliations() > before)
            await _until(lambda: not coordinator._workers)
        await hold.wait()

    request, _ = await _waited_connect(setup, wait)
    try:
        await _until(opened.is_set)
        await _until(lambda: reconciliations() == len(frames))
        assert coordinator.receipt(session, request["operation_id"])["state"] == "waiting"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_cancels_wait_without_owner_work(setup):
    coordinator, owner, session, _ = setup
    hints = Hints(("hold",))
    request, _ = await _waited_connect(setup, hints)
    await _until(lambda: len(hints.opened) >= 1)
    calls = len(owner.calls)
    await asyncio.wait_for(coordinator.stop(), 1)
    await asyncio.sleep(0.02)
    assert len(owner.calls) == calls
    assert not coordinator._waits


@pytest.mark.asyncio
async def test_hint_worker_coalesces_bursts_and_retains_later_revision(setup):
    coordinator, owner, session, _ = setup
    owner.result = OperationResult("waiting")
    await coordinator.command(session, command(coordinator.read(session)))
    await coordinator.drain()
    operation = coordinator._pending()[0]
    entered, release = asyncio.Event(), asyncio.Event()
    hints = []

    async def reconcile_hint(value):
        hints.append(value)
        if len(hints) == 1:
            entered.set()
            await release.wait()
        return False

    owner.reconcile_hint = reconcile_hint
    coordinator._reconcile_soon(operation)
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(20):
        coordinator._reconcile_soon(operation)
    release.set()
    await coordinator.drain()
    assert len(hints) == 2
    assert len(owner.calls) == 1
    await coordinator.stop()


@pytest.mark.asyncio
async def test_non_hint_reconcile_during_active_worker_runs_full_followup(setup):
    coordinator, owner, session, _ = setup
    owner.result = OperationResult("waiting")
    await coordinator.command(session, command(coordinator.read(session)))
    await coordinator.drain()
    operation = coordinator._pending()[0]
    entered, release = asyncio.Event(), asyncio.Event()
    hints = []

    async def reconcile_hint(value):
        hints.append(value["operation_id"])
        if len(hints) == 1:
            entered.set()
            await release.wait()
        return False

    owner.reconcile_hint = reconcile_hint
    owner.result = OperationResult("refused")
    coordinator._reconcile_soon(operation)
    await asyncio.wait_for(entered.wait(), 1)
    coordinator._reconcile_soon(operation)
    await coordinator.reconcile(session, operation["operation_id"])
    coordinator._reconcile_soon(operation)
    release.set()
    await coordinator.drain()
    assert len(hints) == 1
    assert len(owner.calls) == 2
    assert owner.calls[-1][1] is True
    assert coordinator.receipt(session, operation["operation_id"])["state"] == "refused"
    await coordinator.stop()


def test_sign_in_phases_are_closed_delta_only_info_records(caplog):
    from app.onboarding import timing
    import logging
    caplog.set_level(logging.INFO, logger=timing.__name__)
    with timing.phase("authentication-read", "challenge", clock=iter([3.0, 3.125]).__next__):
        pass
    assert caplog.messages == ["event=sign_in_phase operation=authentication-read phase=challenge status=ok elapsed_ms=125"]
    with pytest.raises(ValueError):
        with timing.phase("private-value", "challenge"):
            pass
    assert len(caplog.messages) == 1


def test_sign_in_phases_append_to_a_bounded_local_file(tmp_path, monkeypatch):
    import re
    from app.onboarding import timing
    monkeypatch.setattr(timing, "_sink", None)
    timing.observe("key", "sign", "ok", 1)
    timing.record_to(tmp_path / "absent")
    timing.observe("key", "sign", "ok", 1)
    assert not (tmp_path / "absent").exists()
    timing.record_to(tmp_path)
    timing.reset()
    timing.mark("operation", "frame", clock=lambda: 1.0)
    timing.completed("operation", clock=lambda: 1.5)
    timing.close("operation", "helper", clock=lambda: 1.75)
    directory = tmp_path / "diagnostics"
    first = (directory / "sign-in-timing.log").read_text("ascii").splitlines()
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z "
    assert re.fullmatch(stamp + "sign_in_phase operation=helper phase=task-send status=ok elapsed_ms=250", first[0])
    assert re.fullmatch(stamp + "sign_in_close_timing route=helper frame_to_completed_ms=500 completed_to_sent_ms=250",
                        first[1])
    for _ in range(3000):
        timing.observe("authentication-read", "challenge-receive", "ok", 125.4)
    names = sorted(path.name for path in directory.iterdir())
    assert names == ["sign-in-timing.1.log", "sign-in-timing.log"]
    for name in names:
        size = (directory / name).stat().st_size
        assert 0 < size <= timing.SINK_LIMIT_BYTES
        last = (directory / name).read_text("ascii").splitlines()[-1]
        assert re.fullmatch(stamp + "sign_in_phase operation=authentication-read phase=challenge-receive "
                            "status=ok elapsed_ms=125", last)
    timing.reset()


@pytest.mark.asyncio
async def test_wait_opens_during_dispatch_but_prefetch_waits_for_its_first_frame(setup):
    from app.provisioning.events import events
    coordinator, owner, session, _ = setup
    opened, prefetched, release, frame = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    ready = []

    async def execute(operation, **kwargs):
        ready.append(True)
        events.publish()
        await release.wait()
        return OperationResult("waiting")

    async def wait(operation):
        opened.set()
        await frame.wait()
        yield _hint(1)
        await release.wait()

    async def prefetch(operation):
        prefetched.set()

    owner.execute, owner.wait, owner.prefetch = execute, wait, prefetch
    owner.automatic_recovery = lambda operation: bool(ready)
    owner.probe_interval = lambda operation: 2 if ready else None
    owner.probe = lambda operation: None
    coordinator.start()
    try:
        await coordinator.command(session, command(coordinator.read(session)))
        await asyncio.wait_for(opened.wait(), 1)
        await asyncio.sleep(0.05)
        assert not prefetched.is_set()
        frame.set()
        await asyncio.wait_for(prefetched.wait(), 1)
        assert coordinator._workers
    finally:
        release.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_prefetched_challenge_is_renewed_while_the_sign_in_stays_open(setup):
    coordinator, owner, session, _ = setup
    open_task, renewals, release, delay = [True], [], asyncio.Event(), [0.02]

    async def execute(operation, **kwargs):
        await release.wait()
        return OperationResult("waiting")

    async def wait(operation):
        yield _hint(1)
        await release.wait()

    async def prefetch(operation):
        renewals.append(delay[0])
        return delay[0]

    owner.execute, owner.wait, owner.prefetch = execute, wait, prefetch
    owner.automatic_recovery = lambda operation: True
    owner.probe_interval = lambda operation: 2 if open_task[0] else None
    owner.probe = lambda operation: None
    coordinator.start()
    try:
        await coordinator.command(session, command(coordinator.read(session)))
        for _ in range(100):
            if len(renewals) >= 4:
                break
            await asyncio.sleep(0.01)
        assert len(renewals) >= 4
        delay[0] = 30
        for _ in range(100):
            if renewals[-1] == 30:
                break
            await asyncio.sleep(0.01)
        operation_id = next(iter(coordinator._prefetches))
        # A consumed challenge is replaced at once rather than at the next renewal.
        before = len(renewals)
        coordinator._prefetch({"operation_id": operation_id})
        for _ in range(50):
            if len(renewals) > before:
                break
            await asyncio.sleep(0.01)
        assert len(renewals) > before
        open_task[0] = False
        renewed = len(renewals)
        coordinator._prefetch({"operation_id": operation_id})
        for _ in range(50):
            if not coordinator._prefetches:
                break
            await asyncio.sleep(0.01)
        assert not coordinator._prefetches and len(renewals) == renewed
    finally:
        release.set()
        await coordinator.stop()

@pytest.mark.asyncio
async def test_a_later_frame_with_the_outcome_a_hinted_read_closed_on_reads_nothing(setup):
    coordinator, owner, session, _ = setup
    owner.result = OperationResult("waiting")
    await coordinator.command(session, command(coordinator.read(session)))
    await coordinator.drain()
    operation = coordinator._pending()[0]
    hints = []

    async def reconcile_hint(value):
        hints.append(value["operation_id"])
        return True if len(hints) == 1 else None

    owner.reconcile_hint = reconcile_hint
    owner.calls.clear()
    coordinator._reconcile_soon(operation, frame=(1, "active", "awaiting-confirmation"))
    await coordinator.drain()
    assert (len(hints), len(owner.calls)) == (1, 1)
    # The ceremony retires on the same outcome. The closed task has nothing left to read.
    coordinator._reconcile_soon(operation, frame=(2, "retired", "awaiting-confirmation"))
    await coordinator.drain()
    assert (len(hints), len(owner.calls)) == (1, 1)
    # A new outcome is read in full.
    coordinator._reconcile_soon(operation, frame=(3, "retired", "committed"))
    await coordinator.drain()
    assert (len(hints), len(owner.calls)) == (2, 2)
    # A read without a frame is never skipped.
    coordinator._reconcile_soon(operation)
    await coordinator.drain()
    assert (len(hints), len(owner.calls)) == (3, 3)
    await coordinator.stop()

@pytest.mark.asyncio
async def test_a_watch_pass_reads_the_store_off_the_event_loop_and_keeps_a_wake_from_that_read(setup):
    import threading
    from app.provisioning.events import events
    coordinator, owner, session, _ = setup
    owner.result = OperationResult("waiting")
    await coordinator.command(session, command(coordinator.read(session)))
    await coordinator.drain()
    database, loop_thread = coordinator.store.database, threading.get_ident()
    connect, on_loop, observed = database.connect, [], []
    state = {"commit": False, "done": False}

    def counted():
        if threading.get_ident() == loop_thread:
            on_loop.append(1)
        return connect()

    def observe(operation):
        with database.read() as connection:
            connection.execute("SELECT 1").fetchone()
        observed.append(threading.get_ident())
        if state["commit"]:
            # A commit lands while this pass reads. Its wake must reach the next pass.
            state["commit"], state["done"] = False, True
            events.publish()
            return None
        return OperationResult("completed") if state["done"] else None

    owner.observe = observe
    settled = asyncio.Event()
    owner.settled = lambda operation, result: settled.set()
    coordinator.start()
    try:
        async with asyncio.timeout(2):
            while not observed:
                await asyncio.sleep(0.005)
        database.connect = counted
        state["commit"] = True
        events.publish()
        async with asyncio.timeout(2):
            await settled.wait()
        database.connect = connect
        assert loop_thread not in observed
        # Only the settle's own write runs on the loop. Every pass read ran in a worker thread.
        assert len(on_loop) <= 1
    finally:
        database.connect = connect
        await coordinator.stop()


def ready_for_finalization(coordinator, owner, session):
    owner.automatic_finalization = True
    owner.facts.update(installation="verified", account="verified", browser_access="verified")
    owner.scope.update(source_journey_id=str(uuid4()), journey_kind="initial-enrollment",
        installation_id="installation", association_request_id="association", creator_account_id="creator",
        key={"installation_key_id": "key", "installation_key_jkt": "thumbprint"})
    journey = str(uuid4())
    with coordinator.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,created_at) VALUES (?,?)",
            (journey, coordinator.admissions._now()))
        connection.execute("UPDATE desktop_presentations SET journey_id=? WHERE generation=?",
            (journey, session.generation))
    return journey


@pytest.mark.asyncio
async def test_automatically_finalizes_verified_account_once_without_renderer_action(setup):
    from app.provisioning.events import events
    coordinator, owner, session, _ = setup
    ready_for_finalization(coordinator, owner, session)
    owner.release = asyncio.Event()
    snapshot = coordinator.read(session)
    assert "setup.finalize" in snapshot["allowed_actions"]
    assert not owner.calls and not owner.admissions
    coordinator.start()
    try:
        async with asyncio.timeout(10):
            await owner.started.wait()
        operation = owner.calls[0][0]
        assert operation["action"] == "setup.finalize"
        assert coordinator.receipt(session, operation["operation_id"])["state"] == "accepted"
        for _ in range(3):
            events.publish()
            await asyncio.sleep(0)
        assert len(owner.calls) == len(owner.admissions) == 1
        owner.facts["configuration"] = "verified"
        owner.release.set()
        await coordinator.drain()
        assert coordinator.receipt(session, operation["operation_id"])["state"] == "completed"
        assert not coordinator.read(session)["pending_operations"]
        assert len(owner.calls) == 1
    finally:
        owner.release.set()
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["no-account", "no-key", "recovery", "expired", "pending", "confirmation", "runtime"])
async def test_automatic_finalization_never_creates_new_authority(setup, block):
    coordinator, owner, session, now = setup
    ready_for_finalization(coordinator, owner, session)
    if block == "no-account":
        owner.facts["account"] = "missing"
    elif block == "no-key":
        owner.scope["key"] = None
    elif block == "recovery":
        owner.scope["source_recovery"] = {"source": "unconfirmed"}
    elif block == "expired":
        now[0] += timedelta(hours=1)
    elif block == "pending":
        owner.facts["account"] = "missing"
        request = command(coordinator.read(session), "account.connect")
        coordinator._accept(session, request)
        owner.facts["account"] = "verified"
    elif block == "confirmation":
        owner.scope["desktop_decision_status"] = {"operation_id": str(uuid4()), "state": "confirmation-required"}
    elif block == "runtime":
        coordinator = DesktopOnboardingCoordinator(ScopedAdmissions(coordinator.store, stage="runtime"), owner)
    before = table_counts(coordinator)
    coordinator._continue_finalization()
    assert table_counts(coordinator) == before
    assert not owner.calls


@pytest.mark.asyncio
async def test_finalization_refusal_survives_reopening_without_a_new_operation(setup):
    coordinator, owner, session, now = setup
    ready_for_finalization(coordinator, owner, session)
    owner.result = OperationResult("refused", "authority_unavailable")
    coordinator._continue_finalization()
    await coordinator.drain()
    operation = owner.calls[0][0]
    assert coordinator.receipt(session, operation["operation_id"])["state"] == "refused"
    assert coordinator.read(session)["reason"] == "authority_unavailable"
    assert "setup.finalize" not in coordinator.read(session)["allowed_actions"]
    now[0] += timedelta(hours=1)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    reopened = DesktopOnboardingCoordinator(coordinator.admissions, owner)
    reopened._continue_finalization()
    assert reopened.read(replacement)["reason"] == "authority_unavailable"
    assert len(owner.calls) == len(owner.admissions) == 1


@pytest.mark.asyncio
async def test_restart_reconciles_original_automatic_finalization(setup):
    coordinator, owner, session, _ = setup
    ready_for_finalization(coordinator, owner, session)
    owner.result = OSError("lost result")
    coordinator._continue_finalization()
    await coordinator.drain()
    first = owner.calls[0][0]["operation_id"]
    assert coordinator.receipt(session, first)["state"] == "outcome_unknown"
    owner.result = OperationResult("completed")
    reopened = DesktopOnboardingCoordinator(coordinator.admissions, owner)
    reopened.start()
    try:
        await reopened.drain()
        assert reopened.receipt(session, first)["state"] == "completed"
        assert [(op["operation_id"], reconcile) for op, reconcile in owner.calls] == [(first, False), (first, True)]
        assert len(owner.admissions) == 1
    finally:
        await reopened.stop()


@pytest.mark.asyncio
async def test_drain_completes_when_caller_wakes_directly_from_worker_settlement(setup):
    coordinator, owner, session, _ = setup
    ready_for_finalization(coordinator, owner, session)
    settled = asyncio.Event()
    owner.settled = lambda operation, result: settled.set()
    coordinator.start()
    try:
        async with asyncio.timeout(5):
            await settled.wait()
            await coordinator.drain()
        assert not coordinator._workers
        assert len(owner.calls) == 1
        assert coordinator.receipt(session, owner.calls[0][0]["operation_id"])["state"] == "completed"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_replacement_required_projects_only_verified_predecessor_choices_and_keeps_admission_closed(setup):
    coordinator, owner, session, _ = setup
    owner.facts["browser_access"] = "verified"
    parent, _ = coordinator._accept(session, command(coordinator.read(session), "account.connect"))
    parent_id = parent["operation_id"]
    predecessor = {"parent_operation_id": parent_id, "predecessor": {"predecessor_operation_id": "0198a1b2-c3d4-7300-8000-000000000001"}}
    verified_choices = {"google": True, "microsoft-personal": False, "telegram": True, "tiktok": False, "default": True}
    all_false = dict.fromkeys(verified_choices, False)
    owner.scope["ceremony_replacement"] = predecessor
    owner.scope["desktop_authentication"] = {"operation_id": parent_id, "browser_operation_id": None,
        "state": "expired", "choices": verified_choices,
        "actions": dict.fromkeys(("select", "cancel", "open_browser", "switch_account"), False),
        "authentication_attempt": None}
    snapshot = coordinator.read(session)
    assert snapshot["authentication"]["state"] == "replacement-required"
    assert snapshot["authentication"]["choices"] == verified_choices
    assert snapshot["allowed_actions"] == ["auth.select"]
    with pytest.raises(CommandRefused) as refused:
        coordinator._accept(session, {**command(snapshot, "auth.select"), "provider": "google"})
    assert refused.value.reason == "action_unavailable"
    accepted, fresh = coordinator._accept(session, {**command(snapshot, "auth.select"), "replace_ceremony": True})
    assert fresh and json.loads(accepted["owner_scope_json"])["ceremony_replacement"] == predecessor
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='completed' WHERE operation_id=?", (accepted["operation_id"],))
    for stale in (
        None,
        {**owner.scope["desktop_authentication"], "operation_id": str(uuid4())},
        {**owner.scope["desktop_authentication"], "state": "unavailable"},
    ):
        owner.scope["desktop_authentication"] = stale
        assert coordinator.read(session)["authentication"]["choices"] == all_false
