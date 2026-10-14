import asyncio
from datetime import datetime, timedelta, timezone
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
        "choices": dict.fromkeys(("google", "microsoft-personal", "default"), True),
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
