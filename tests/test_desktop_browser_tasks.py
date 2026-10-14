import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.onboarding.admission import AdmissionRefused, ScopedAdmissions
from app.onboarding.browser_tasks import BrowserTasks, validate_observation
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.persistence.auth import SQLiteAuthenticationStore
from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
from app.security.hosted_grants import HostedGrantUnavailable
from test_desktop_coordinator import command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class ExistingKeyOnly:
    def reopen_existing(self):
        pytest.fail("Browser access does not touch an installation key")


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: now[0])
    admissions = ScopedAdmissions(store, stage="setup")
    tasks = BrowserTasks(admissions, extension_id="a" * 32)
    owner = ProvisioningSetupOwner(store, browser_tasks=tasks, ceremonies=None, initial=None, registered=None,
        key=ExistingKeyOnly(), finalize=lambda **kwargs: pytest.fail("No finalization before account authority"), completion_ready=lambda: False)
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    return coordinator, tasks, native, now


async def browser_access(setup):
    coordinator, tasks, native, _ = setup
    request = command(coordinator.read(native))
    await coordinator.command(native, request)
    await coordinator.drain()
    native = coordinator.admissions.require(native.identifier, role="desktop", generation=native.generation)
    task = tasks.current_native(native)[0]
    code = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task["task_id"])
    browser = coordinator.admissions.consume(code, role="browser_task")
    return request, task, browser


def observation(browser):
    return {"profile": "browser-task-observation.v1", "task_id": browser.task_id, "journey_id": browser.journey_id,
        "admission_generation": browser.generation, "extension_id": "a" * 32, "instance_id": str(uuid4()),
        "epoch": str(uuid4()), "revision": 1, "account_generation": 1, "consent_generation": 1,
        "draft_scope_id": str(uuid4()), "disclosure_bundle_id": "a" * 64, "state": "ready", "creator_account_id": "creator-test"}


@pytest.mark.asyncio
async def test_account_admission_freezes_a_separate_source_and_the_observed_creator(setup):
    coordinator, tasks, native, _ = setup
    request, _, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    operation, fresh = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    assert fresh
    scope = json.loads(operation["owner_scope_json"])
    assert scope["source_journey_id"] != operation["journey_id"]
    assert scope["creator_account_id"] == "creator-test" and scope["journey_kind"] == "initial-enrollment"
    task_id = tasks.prepare(operation, "browser.authentication")
    task = next(value for value in tasks.current_native(native) if value["task_id"] == task_id)
    attached = tasks.attachment(task)
    assert attached["creator_account_id"] == "creator-test"
    assert attached["participant"]["instance_id"] == scope["browser"]["instance_id"]
    coordinator.owner.authorize_initial_scope(scope["source_journey_id"], {"intended_creator_id": "creator-test"})
    with pytest.raises(HostedGrantUnavailable):
        coordinator.owner.authorize_initial_scope(scope["source_journey_id"], {"intended_creator_id": "another-creator"})
    assert coordinator.read(native)["journey_id"] == browser.journey_id


@pytest.mark.asyncio
async def test_expired_browser_access_can_restart_without_renewing_a_domain_operation(setup):
    coordinator, tasks, native, clock = setup
    original, _, browser = await browser_access(setup)
    clock[0] += timedelta(minutes=31)
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    await coordinator.reconcile(replacement, original["operation_id"])
    await coordinator.drain()
    assert coordinator.receipt(replacement, original["operation_id"])["reason"] == "operation_expired"
    assert coordinator.read(replacement)["allowed_actions"] == ["browser.access"]
    request = command(coordinator.read(replacement))
    await coordinator.command(replacement, request)
    await coordinator.drain()
    assert coordinator.receipt(replacement, request["operation_id"])["state"] == "waiting"
    assert len(tasks.current_native(replacement)) == 1
    with pytest.raises(AdmissionRefused):
        tasks.observe(browser, observation(browser))
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_restricted_owner_opens_one_bounded_browser_task_without_creating_cloud_authority(setup):
    coordinator, tasks, native, _ = setup
    request, task, browser = await browser_access(setup)
    assert coordinator.receipt(native, request["operation_id"])["state"] == "waiting"
    assert task["operation_id"] == request["operation_id"] and task["purpose"] == "browser.access"
    assert tasks.attachment(task)["creator_account_id"] is None
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 0
    tasks.observe(browser, observation(browser))
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    snapshot = coordinator.read(native)
    assert snapshot["next_task"] == "account-connection" and snapshot["allowed_actions"] == ["account.connect"]
    assert snapshot["facts"]["installation"] == "missing" and snapshot["facts"]["account"] == "missing"
    assert not snapshot["local_ready"] and not snapshot["full_ready"]


@pytest.mark.asyncio
async def test_exact_role_task_and_extension_are_required_for_observations(setup):
    coordinator, tasks, native, _ = setup
    _, _, browser = await browser_access(setup)
    value = observation(browser)
    with pytest.raises(AdmissionRefused):
        tasks.observe(native, value)
    for change in ({"extension_id": "b" * 32}, {"journey_id": str(uuid4())},
                   {"task_id": str(uuid4())}, {"admission_generation": str(uuid4())}):
        with pytest.raises(AdmissionRefused):
            tasks.observe(browser, value | change)
    assert tasks.read(browser.journey_id) is None


@pytest.mark.asyncio
async def test_reissue_retires_old_browser_and_retains_operation_scope_and_deadline(setup):
    coordinator, tasks, native, _ = setup
    request, task, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    code = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task["task_id"])
    assert tasks.read(browser.journey_id) is None
    with pytest.raises(AdmissionRefused):
        tasks.observe(browser, observation(browser))
    new_browser = coordinator.admissions.consume(code, role="browser_task")
    tasks.observe(new_browser, observation(new_browser))
    current = coordinator.admissions.task(new_browser.identifier, generation=new_browser.generation)
    assert current["operation_id"] == request["operation_id"]
    assert current["scope_digest"] == task["scope_digest"] and current["expires_at"] == task["expires_at"]


@pytest.mark.asyncio
async def test_stale_and_replayed_extension_state_cannot_retarget_a_live_admission(setup):
    _, tasks, _, _ = setup
    _, _, browser = await browser_access(setup)
    value = observation(browser)
    tasks.observe(browser, value)
    tasks.observe(browser, value)
    for change in ({"revision": 0}, {"instance_id": str(uuid4()), "revision": 2},
                   {"epoch": str(uuid4()), "revision": 2}, {"creator_account_id": "different"},
                   {"consent_generation": 0, "revision": 2}):
        with pytest.raises(AdmissionRefused):
            tasks.observe(browser, value | change)
    tasks.observe(browser, value | {"revision": 2, "account_generation": 2, "creator_account_id": "different"})
    assert tasks.read(browser.journey_id)["creator_account_id"] == "different"


@pytest.mark.asyncio
async def test_passive_reads_do_not_complete_access_and_owner_event_settles_it(setup):
    coordinator, tasks, native, _ = setup
    request, _, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    for _ in range(3):
        coordinator.read(native)
        assert coordinator.receipt(native, request["operation_id"])["state"] == "waiting"
    coordinator.start()
    async with asyncio.timeout(2):
        while coordinator.receipt(native, request["operation_id"])["state"] != "completed":
            await asyncio.sleep(0)
    await coordinator.stop()


@pytest.mark.asyncio
async def test_browser_expiry_hides_observation_without_renewal(setup):
    coordinator, tasks, native, now = setup
    _, _, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    now[0] += timedelta(minutes=31)
    assert tasks.read(browser.journey_id) is None
    with pytest.raises(AdmissionRefused):
        tasks.observe(browser, observation(browser))


def test_observation_cannot_claim_installation_pairing_or_capability_authority():
    from types import SimpleNamespace
    value = observation(SimpleNamespace(task_id=str(uuid4()), journey_id=str(uuid4()), generation=str(uuid4())))
    for change in ({"approved": True}, {"local_ready": True}, {"license": "secret"}, {"comparison_code": "123456"},
                   {"state": "ready", "creator_account_id": None}, {"state": "needs-consent"},
                   {"revision": True}):
        with pytest.raises(AdmissionRefused):
            validate_observation(value | change)


@pytest.mark.asyncio
async def test_expired_initial_completion_uses_its_original_recovery_without_a_new_command(setup):
    from types import SimpleNamespace
    from test_onboarding_enrollment_runtime import _HandoffFixture, _enrollment_worker
    coordinator, tasks, native, clock = setup
    request, _, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    scope = json.loads(operation["owner_scope_json"])
    client = _HandoffFixture()
    initial = _enrollment_worker(coordinator.store, client)
    coordinator.owner.initial = initial
    coordinator.owner.key = SimpleNamespace(reopen_existing=client.key.ensure_ready)
    resumed = []
    initial.resume = resumed.append
    initial.journeys.update(scope["source_journey_id"], state="unknown", operation_id=initial.journeys.new_operation(),
        scope_json=json.dumps({"intended_creator_id": scope["creator_account_id"],
            "installation_key_jkt": client.key.ensure_ready().installation_key_jkt}),
        prepare_json=json.dumps({"profile": "urn:bridge-clean:initial-installation-handoff:v2"}))
    before = initial.journeys.require_current(scope["source_journey_id"])
    clock[0] += timedelta(minutes=31)
    result = await coordinator.owner.execute(operation, reconcile=True)
    assert result.state == "outcome_unknown"
    assert len(resumed) == 1 and resumed[0] != scope["source_journey_id"]
    recovered = initial.journeys.require_current(resumed[0])
    assert recovered["operation_id"] == before["operation_id"]
    assert recovered["installation_id"] == before["installation_id"]
    assert recovered["scope_json"] == before["scope_json"]
    with coordinator.store.database.read() as connection:
        saved = connection.execute("SELECT owner_scope_json FROM desktop_setup_operations WHERE operation_id=?", (operation["operation_id"],)).fetchone()
        assert saved["owner_scope_json"] == operation["owner_scope_json"]
        assert connection.execute("SELECT count(*) FROM provisioning_browser_sessions").fetchone()[0] == 0
    assert client.preparations == []

async def initial_ceremony_case(setup, *, active_wait=False):
    from types import SimpleNamespace
    from app.onboarding.ceremonies import BrowserCeremonies
    from app.security.browser_ceremony import BrowserCeremonyClient
    from test_desktop_ceremonies import Client
    from test_onboarding_enrollment_runtime import _HandoffFixture, _enrollment_worker
    coordinator, tasks, native, clock = setup
    request, _, browser = await browser_access(setup)
    tasks.observe(browser, observation(browser))
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    scope = json.loads(operation["owner_scope_json"])
    fixture = _HandoffFixture()
    original_prepare = fixture.prepare
    def prepare(request, **kwargs):
        return original_prepare(request, **kwargs) | {"reference": "A" * 43, "expires_at": (clock[0] + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    fixture.prepare = prepare
    initial = _enrollment_worker(coordinator.store, fixture)
    initial.bind_effect_gate(coordinator.owner._effect_gate, initial._current_journey)
    coordinator.owner.initial = initial
    coordinator.owner.registered = SimpleNamespace()
    coordinator.owner.key = SimpleNamespace(reopen_existing=fixture.key.ensure_ready)
    if not active_wait:
        initial.resume = lambda _: None
    await initial.prepare(scope["source_journey_id"])
    if active_wait:
        async with asyncio.timeout(2):
            while not fixture.waits or initial.accepted_work():
                await asyncio.sleep(0)
    class TerminalClient(Client):
        mode = "terminal"
        def read(self, request, **kwargs):
            if self.mode == "refused":
                raise HostedGrantUnavailable("Unavailable")
            result = super().read(request, **kwargs)
            if self.mode == "missing":
                result = {name: result[name] for name in ("profile", "operation_id")} | {"status": "unavailable"}
            elif self.mode == "terminal":
                result["snapshot"]["domain_status"] = "unavailable"
            self.validator.validate_result("read", request, result, expires_at=kwargs.get("expires_at"))
            return result
    client = TerminalClient()
    client.validator = BrowserCeremonyClient(None, None, clock=lambda: clock[0])
    ceremonies = BrowserCeremonies(coordinator.store, client, hosted_origin="https://provider.example")
    coordinator.owner.ceremonies = ceremonies
    binding = coordinator.owner._binding(operation)
    await ceremonies.prepare(operation["operation_id"], binding, authorize=lambda connection=None: coordinator.owner._guard(operation, connection=connection))
    client.state = "expired"
    return coordinator, initial, client, operation, scope, clock, native


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [0, 11, 31, 61])
async def test_exact_initial_terminal_read_allows_explicit_same_desktop_continuation(setup, elapsed):
    coordinator, initial, client, operation, scope, clock, native = await initial_ceremony_case(setup)
    before = coordinator.owner.sources.resolve(scope["source_journey_id"])
    assert before.kind == "initial-enrollment" and before.binding is None
    assert before.preparation.installation_key_id == initial.client.key.ensure_ready().installation_key_id
    clock[0] += timedelta(minutes=elapsed)
    result = await coordinator.owner.execute(operation, reconcile=True)
    assert (result.state, result.reason) == ("refused", "operation_expired")
    coordinator._settle(operation, result)
    if elapsed >= 30:
        native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(replacing_generation=native.generation), role="desktop")
    view = coordinator.owner.read(operation["journey_id"])
    recovery = view.scope["source_recovery"]
    assert recovery is not None and recovery["creator_account_id"] == scope["creator_account_id"]
    with coordinator.store.database.transaction() as connection:
        admitted = coordinator.owner.admit(connection, operation["journey_id"], action="account.connect", scope=view.scope)
    assert admitted.journey_id == operation["journey_id"]
    successor = initial.journeys.require_current(admitted.scope["source_journey_id"])
    assert successor["journey_id"] != scope["source_journey_id"] and successor["state"] == "new"
    retained = coordinator.owner.sources.resolve(scope["source_journey_id"])
    assert retained.state == "expired" and retained.operation_id == before.operation_id
    assert retained.expires_at == before.expires_at
    assert retained.installation_id == before.installation_id and retained.binding == before.binding
    assert len([call for call in client.calls if call[0] == "prepare"]) == 1
    assert len(initial.client.preparations) == 1


@pytest.mark.asyncio
async def test_typed_source_keeps_initial_terminal_proof_after_retirement(setup):
    coordinator, initial, client, operation, scope, clock, _ = await initial_ceremony_case(setup)
    before = coordinator.owner.sources.resolve(scope["source_journey_id"])
    clock[0] += timedelta(minutes=31)
    with initial.store.database.transaction() as connection:
        initial.journeys._cleanup(connection, clock[0])
    result = await coordinator.owner.execute(operation, reconcile=True)
    assert (result.state, result.reason) == ("refused", "operation_expired")
    retained = coordinator.owner.sources.resolve(scope["source_journey_id"])
    assert retained.terminal_read.domain_status == "unavailable"
    assert retained.operation_id == before.operation_id and retained.expires_at == before.expires_at
    assert len([call for call in client.calls if call[0] == "prepare"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "refused", "pending"])
async def test_unconfirmed_initial_read_never_settles_or_replaces_original_source(setup, mode):
    coordinator, initial, client, operation, scope, clock, _ = await initial_ceremony_case(setup)
    clock[0] += timedelta(minutes=31)
    client.mode = mode
    try:
        result = await coordinator.owner.execute(operation, reconcile=True)
    except HostedGrantUnavailable:
        assert mode == "refused"
    else:
        assert result.state == "outcome_unknown"
    current = coordinator.owner.sources.resolve(scope["source_journey_id"])
    assert current.state == "waiting"
    assert current.terminal_read is None
    assert coordinator.owner.read(operation["journey_id"]).scope["source_recovery"] is None
    assert len(initial.client.preparations) == 1


@pytest.mark.asyncio
async def test_terminal_initial_read_stops_only_the_idle_original_wait(setup):
    coordinator, initial, _, operation, scope, clock, _ = await initial_ceremony_case(setup, active_wait=True)
    task = initial._tasks[scope["source_journey_id"]]
    assert not task.done()
    clock[0] += timedelta(minutes=11)
    result = await coordinator.owner.execute(operation, reconcile=True)
    assert result.reason == "operation_expired" and task.cancelled()
    assert not coordinator.owner._work()
    await initial.stop()
