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


class MissingKeyOnly:
    def __init__(self):
        self.prepared = 0

    def ensure_ready(self):
        self.prepared += 1

    def reopen_existing(self):
        pytest.fail("Browser access only creates a missing installation key")


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: now[0])
    admissions = ScopedAdmissions(store, stage="setup")
    tasks = BrowserTasks(admissions, extension_id="a" * 32)
    owner = ProvisioningSetupOwner(store, browser_tasks=tasks, ceremonies=None, initial=None, registered=None,
        key=MissingKeyOnly(), finalize=lambda **kwargs: pytest.fail("No finalization before account authority"), completion_ready=lambda: False)
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


async def launched_key(coordinator, tasks, operation_id):
    tasks.launched(operation_id)
    await coordinator.owner._key_task


@pytest.mark.asyncio
async def test_browser_access_creates_a_missing_key_once_its_launch_returns(setup):
    from app.security.installation_key import InstallationKeyAuthority
    from test_installation_key import EmulatedPlatformProvider

    coordinator, tasks, native, _ = setup
    store, provider = coordinator.owner.store, EmulatedPlatformProvider()
    coordinator.owner.key = InstallationKeyAuthority(store, provider)
    request, _, _ = await browser_access(setup)
    assert coordinator.receipt(native, request["operation_id"])["state"] == "waiting"
    await asyncio.sleep(0.05)
    assert store.installation_key_reference() is None and provider.create_calls == 0
    await launched_key(coordinator, tasks, request["operation_id"])
    assert store.installation_key_reference() is not None and provider.create_calls == 1
    await coordinator.owner.stop()


@pytest.mark.asyncio
async def test_stopping_before_the_launch_returns_prepares_no_key(setup):
    coordinator, _, _, _ = setup
    await browser_access(setup)
    await coordinator.owner.stop()
    assert coordinator.owner.key.prepared == 0


@pytest.mark.asyncio
async def test_only_the_browser_access_click_prepares_a_missing_key(setup):
    coordinator, tasks, _, _ = setup
    request, _, _ = await browser_access(setup)
    with coordinator.store.database.read() as connection:
        operation = dict(connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (request["operation_id"],)).fetchone())
    await launched_key(coordinator, tasks, request["operation_id"])
    coordinator.owner.key = MissingKeyOnly()
    assert (await coordinator.owner.execute(operation, reconcile=True)).state == "waiting"
    tasks.launched(request["operation_id"])
    await asyncio.sleep(0.05)
    await coordinator.owner.stop()
    assert coordinator.owner.key.prepared == 0


@pytest.mark.asyncio
async def test_browser_access_leaves_an_active_key_alone(setup):
    from app.security.installation_key import InstallationKeyAuthority
    from test_installation_key import EmulatedPlatformProvider

    coordinator, tasks, _, _ = setup
    active = InstallationKeyAuthority(coordinator.owner.store, EmulatedPlatformProvider()).ensure_ready()
    request, _, _ = await browser_access(setup)
    await launched_key(coordinator, tasks, request["operation_id"])
    await coordinator.owner.stop()
    assert coordinator.owner.key.prepared == 0
    assert coordinator.owner.store.installation_key_reference() == active


@pytest.mark.asyncio
async def test_a_key_failure_does_not_affect_browser_access(setup):
    from app.security.installation_key import InstallationKeyUnavailable

    class RefusingKey(MissingKeyOnly):
        def ensure_ready(self):
            super().ensure_ready()
            raise InstallationKeyUnavailable("refused")

    coordinator, tasks, native, _ = setup
    coordinator.owner.key = RefusingKey()
    request, task, browser = await browser_access(setup)
    await launched_key(coordinator, tasks, request["operation_id"])
    await coordinator.owner.stop()
    assert coordinator.owner.key.prepared == 1
    assert coordinator.receipt(native, request["operation_id"])["state"] == "waiting"
    assert browser.task_id == task["task_id"]
    assert coordinator.owner.store.installation_key_reference() is None


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
    # Finish the earlier browser-access key preparation before installing the
    # synthetic enrollment key. Otherwise its delayed worker can remember the
    # old browser observation against the new key after a virtual-clock jump.
    await launched_key(coordinator, tasks, request["operation_id"])
    fixture = _HandoffFixture()
    original_prepare = fixture.prepare
    def prepare(request, **kwargs):
        return original_prepare(request, **kwargs) | {"reference": "A" * 43, "expires_at": (clock[0] + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    fixture.prepare = prepare
    initial = _enrollment_worker(coordinator.store, fixture)
    initial.bind_effect_gate(coordinator.owner._effect_gate, initial._current_journey)
    coordinator.owner.initial = initial
    coordinator.owner.registered = SimpleNamespace()
    class ReplacementClient:
        def __init__(self):
            self.calls = []
            self.mode = "prepared"
            self.expected_customer_id = None
            self.completion_id = None
        def replace(self, request, *, before_send, intended_creator_id):
            import hashlib
            from app.security.source_replacement import original_prepare
            from app.security.installation_continuation import canonical_request
            before_send()
            self.calls.append(request)
            if self.mode == "lost":
                raise HostedGrantUnavailable("Unavailable")
            if self.mode == "accepted":
                return {"profile": request["profile"], "predecessor_operation_id": request["predecessor_operation_id"],
                    "status": "accepted", "accepted_operation_id": "0198a1b2-c3d4-7300-8000-000000000077"}
            if self.mode == "committed":
                return {"profile": request["profile"], "predecessor_operation_id": request["predecessor_operation_id"],
                    "status": "committed", "completion_operation_id": self.completion_id or request["predecessor_operation_id"]}
            prepare = original_prepare("initial-handoff", request, successor=True)
            return {"profile": request["profile"], "predecessor_operation_id": request["predecessor_operation_id"],
                "status": "prepared", "successor_prepare_digest": hashlib.sha256(canonical_request(prepare)).hexdigest(),
                "successor": {"profile": prepare["profile"], "operation_id": prepare["operation_id"],
                    "reference": "A" * 43, "expires_at": (clock[0] + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")},
                "intended_creator_id": intended_creator_id} | ({"expected_customer_id": self.expected_customer_id} if self.expected_customer_id else {})
    coordinator.owner.source_replacements.clients["initial-enrollment"] = ReplacementClient()
    coordinator.owner.key = SimpleNamespace(reopen_existing=fixture.key.ensure_ready)
    coordinator.owner.store.installation_key_reference = lambda **_: fixture.key.ensure_ready()
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
    if elapsed == 0:
        assert recovery is None
        return
    assert recovery is not None and recovery["creator_account_id"] == scope["creator_account_id"]
    assert recovery["replacement"]["operation_id"] == before.operation_id
    with coordinator.store.database.transaction() as connection:
        admitted = coordinator.owner.admit(connection, operation["journey_id"], action="account.connect", scope=view.scope)
        root = connection.execute("SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?", (operation["journey_id"],)).fetchone()
        assert root["source_journey_id"] == scope["source_journey_id"]
    assert admitted.journey_id == operation["journey_id"]
    assert admitted.scope["source_journey_id"] != scope["source_journey_id"]
    assert admitted.scope["installation_id"] == before.installation_id
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


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [11, 31, 61])
@pytest.mark.parametrize("browser_outcome", ["waiting", "completed", "refused"])
async def test_restart_after_initial_sign_in_expiry_offers_browser_retry(setup, elapsed, browser_outcome):
    coordinator, _, _, operation, scope, clock, _ = await initial_ceremony_case(setup)
    key = coordinator.store.installation_key_reference()
    before = coordinator.owner.sources.resolve(scope["source_journey_id"])
    clock[0] += timedelta(minutes=elapsed)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    coordinator.start()
    await coordinator.drain()
    snapshot = coordinator.read(native)
    try:
        assert snapshot["reason"] == "none"
        assert snapshot["continuation_available"]
        assert snapshot["next_task"] == "browser-access"
        assert snapshot["allowed_actions"] == ["browser.access"]
        retry = command(snapshot, "browser.access")
        await coordinator.command(native, retry)
        await coordinator.drain()
        assert coordinator.read(native)["reason"] == "none"
        assert coordinator.read(native)["continuation_available"]
        if elapsed >= 30:
            with coordinator.store.database.read() as connection:
                assert connection.execute("SELECT 1 FROM desktop_setup_operations WHERE operation_id=?",
                    (operation["operation_id"],)).fetchone() is None
        if browser_outcome == "refused":
            clock[0] += timedelta(minutes=31)
            native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
            await coordinator.reconcile(native, retry["operation_id"])
            await coordinator.drain()
            assert coordinator.receipt(native, retry["operation_id"])["reason"] == "operation_expired"
            assert coordinator.read(native)["reason"] == "none"
            assert coordinator.read(native)["continuation_available"]
            retry = command(coordinator.read(native), "browser.access")
            await coordinator.command(native, retry)
            await coordinator.drain()
        assert coordinator.receipt(native, retry["operation_id"])["state"] == "waiting"
        tasks = coordinator.owner.browser_tasks.current_native(native)
        assert len(tasks) == 1 and tasks[0]["operation_id"] == retry["operation_id"]
        task = tasks[0]
        code = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task["task_id"])
        browser = coordinator.admissions.consume(code, role="browser_task")
        coordinator.owner.browser_tasks.observe(browser, observation(browser))
        if browser_outcome != "waiting":
            await coordinator.reconcile(native, retry["operation_id"])
            await coordinator.drain()
        ready = coordinator.read(native)
        assert ready["next_task"] == "account-connection"
        assert "account.connect" in ready["allowed_actions"]
        accepted, _ = coordinator._accept(native, command(ready, "account.connect"))
        selected = json.loads(accepted["owner_scope_json"])
        assert selected["source_journey_id"] != scope["source_journey_id"]
        assert selected["creator_account_id"] == scope["creator_account_id"]
        retained = coordinator.owner.sources.resolve(scope["source_journey_id"])
        assert retained.state == "expired" and retained.operation_id == before.operation_id
        assert retained.installation_id == before.installation_id
        assert coordinator.store.installation_key_reference() == key
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "malformed", "incomplete", "foreign-source", "mismatched-creator", "conflicting-creator"])
async def test_browser_retry_recovery_refuses_unproven_or_ambiguous_scopes(setup, change):
    coordinator, _, _, operation, _, clock, _ = await initial_ceremony_case(setup)
    clock[0] += timedelta(minutes=31)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    result = await coordinator.owner.execute(operation, reconcile=True)
    coordinator._settle(operation, result)
    retry, _ = coordinator._accept(native, command(coordinator.read(native), "browser.access"))
    scope = json.loads(retry["owner_scope_json"])
    if change == "missing":
        scope.pop("source_recovery")
    elif change == "malformed":
        scope["source_recovery"] = "unavailable"
    elif change == "incomplete":
        scope["source_recovery"].pop("classification_generation")
    elif change == "foreign-source":
        scope["source_journey_id"] = scope["source_recovery"]["source_journey_id"] = str(uuid4())
    elif change == "mismatched-creator":
        scope["source_recovery"]["creator_account_id"] = "another-creator"
    else:
        scope["creator_account_id"] = scope["source_recovery"]["creator_account_id"] = "another-creator"
    # Synthetic historical journal evidence exercises recovery selection without
    # relaxing production command admission or updating an immutable receipt.
    with coordinator.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_operations SELECT ?,journey_id,action,request_digest,generation,"
            "scope_digest,source_revision,?,'completed','none',created_at,expires_at FROM desktop_setup_operations WHERE operation_id=?",
            (str(uuid4()), json.dumps(scope), retry["operation_id"]))
        if change != "conflicting-creator":
            connection.execute("DELETE FROM desktop_setup_operations WHERE operation_id=?", (retry["operation_id"],))
    view = coordinator.owner.read(retry["journey_id"])
    assert view.scope["source_recovery"] is None
    assert view.reason == "authority_unavailable"


@pytest.mark.asyncio
async def test_an_account_connect_opens_the_store_six_times_before_its_ceremony_send(setup, monkeypatch):
    coordinator, initial, _, operation, _, _, _ = await initial_ceremony_case(setup)
    with coordinator.store.database.transaction() as connection:
        connection.execute("DELETE FROM desktop_browser_ceremonies")
    owner = coordinator.owner
    database, connect, opened = coordinator.store.database, coordinator.store.database.connect, []
    guards, guard = [], owner._guard

    def counted():
        opened.append(1)
        return connect()

    def checked(*args, **kwargs):
        guards.append(1)
        return guard(*args, **kwargs)

    class Sent(Exception):
        pass

    async def send(*args, **kwargs):
        raise Sent()

    monkeypatch.setattr(database, "connect", counted)
    monkeypatch.setattr(owner, "_guard", checked)
    monkeypatch.setattr(owner.ceremonies._effects, "run", send)
    with pytest.raises(Sent):
        await owner.execute(operation, reconcile=False)
    # Every guard check still runs.
    assert len(guards) == 7
    # Shared reads before each await, then the handoff journey read and the ceremony reservation.
    assert len(opened) == 6

@pytest.mark.asyncio
async def test_a_setup_snapshot_read_opens_the_store_once(setup, monkeypatch):
    coordinator, _, _, _, _, _, native = await initial_ceremony_case(setup)
    database, connect, opened = coordinator.store.database, coordinator.store.database.connect, []
    expected = coordinator.read(native)

    def counted():
        opened.append(1)
        return connect()

    monkeypatch.setattr(database, "connect", counted)
    assert coordinator.read(native) == expected
    assert len(opened) == 1

async def replacement_case(setup):
    from app.onboarding.authentication import DesktopAuthentication
    from test_desktop_authentication import AuthenticationClient
    from test_desktop_ceremonies import ReplacementClient
    from test_onboarding_enrollment_runtime import _HandoffFixture
    # Production retains the browser observation under the installation key.
    # Establish the same key before observing browser access in this fixture.
    setup[0].store.installation_key_reference = lambda **_: _HandoffFixture().key.ensure_ready()
    coordinator, initial, client, parent, scope, clock, native = await initial_ceremony_case(setup)
    owner = coordinator.owner
    ceremonies = owner.ceremonies
    client.mode, client.state, client.revision = "live", "retired", 2
    await ceremonies.reconcile(parent["operation_id"], authorize=lambda: owner._read_guard(parent))
    authentication = DesktopAuthentication(coordinator.store, ceremonies, AuthenticationClient())
    ceremonies.authentication = authentication
    ceremonies.replacement_client = ReplacementClient(client, clock=lambda: clock[0])
    return coordinator, initial, ceremonies, parent, scope, clock, native


@pytest.mark.asyncio
async def test_expired_ceremony_recovery_requires_explicit_scoped_action_and_does_not_launch(setup):
    from app.onboarding.coordinator import CommandRefused
    from app.onboarding.contract import validate
    coordinator, initial, ceremonies, parent, scope, clock, native = await replacement_case(setup)
    owner = coordinator.owner
    original = ceremonies.row(parent["operation_id"])
    source = owner.sources.resolve(scope["source_journey_id"])
    before = len(ceremonies.client.calls)
    snapshot = coordinator.read(native)
    assert snapshot["authentication"]["state"] == "replacement-required"
    assert snapshot["allowed_actions"] == ["auth.select"]
    assert snapshot["authentication"]["actions"] == dict(select=True, cancel=False, open_browser=False, switch_account=False)
    assert not any(snapshot["authentication"]["choices"].values())
    assert coordinator.read(native) == snapshot
    assert len(ceremonies.client.calls) == before
    with pytest.raises(CommandRefused):
        coordinator._accept(native, {**command(snapshot, "auth.select"), "provider": "google"})
    with pytest.raises(ValueError):
        validate({**command(snapshot, "auth.select"), "provider": "google", "replace_ceremony": True}, kind="command")
    with pytest.raises(ValueError):
        validate({**command(snapshot, "auth.switch"), "replace_ceremony": True}, kind="command")
    clicked = {**command(snapshot, "auth.select"), "replace_ceremony": True}
    child, fresh = coordinator._accept(native, clicked)
    assert fresh
    assert coordinator._accept(native, clicked) == (child, False)
    with pytest.raises(CommandRefused):
        coordinator._accept(native, {**clicked, "operation_id": str(uuid4())})
    pending = coordinator.read(native)
    assert pending["authentication"]["state"] == "replacement-pending"
    assert "auth.select" not in pending["allowed_actions"]
    with pytest.raises(AdmissionRefused):
        coordinator.admissions.prepare_accepted_task(child["operation_id"], purpose="browser.authentication",
            deadline=child["expires_at"])
    coordinator._start(child, reconcile=False, actor=None)
    await coordinator.drain()
    assert coordinator.receipt(native, child["operation_id"])["state"] == "completed"
    successor = ceremonies.row(parent["operation_id"])
    assert successor["ceremony_id"] != original["ceremony_id"]
    assert successor["provider_expires_at"] == original["provider_expires_at"]
    assert owner.sources.resolve(scope["source_journey_id"]) == source
    assert len(ceremonies.replacement_client.calls) == 1
    assert ceremonies.authentication.client.calls
    assert all(verb == "read" for verb, _ in ceremonies.authentication.client.calls)
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT 1 FROM desktop_browser_tasks WHERE operation_id=?", (child["operation_id"],)).fetchone() is None
    assert (await owner.execute(child, reconcile=True)).state == "completed"
    assert len(ceremonies.replacement_client.calls) == 1
    # The coordinator automatically refreshed the original parent. No extra read command.
    current = coordinator.read(native)
    assert current["authentication"]["state"] == "selection-required"
    assert "auth.select" in current["allowed_actions"]
    assert current["authentication"]["operation_id"] == parent["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before-reservation", "lost-response", "changed-predecessor", "changed-account"])
async def test_explicit_ceremony_recovery_reconciles_or_refuses_without_resending(setup, fault):
    from app.onboarding.coordinator import CommandRefused, OperationResult
    coordinator, initial, ceremonies, parent, scope, clock, native = await replacement_case(setup)
    owner = coordinator.owner
    snapshot = coordinator.read(native)
    assert snapshot["authentication"]["state"] == "replacement-required"
    clicked = {**command(snapshot, "auth.select"), "replace_ceremony": True}
    if fault == "changed-predecessor":
        ceremonies.client.revision += 1
        await ceremonies.reconcile(parent["operation_id"], authorize=lambda: owner._read_guard(parent))
        with pytest.raises(CommandRefused):
            coordinator._accept(native, clicked)
        assert ceremonies.replacement_client.calls == []
        return
    child, fresh = coordinator._accept(native, clicked)
    assert fresh
    if fault == "before-reservation":
        assert (await owner.execute(child, reconcile=True)).state == "refused"
        assert ceremonies.replacement_client.calls == []
        return
    if fault == "changed-account":
        selected = owner.read
        def changed(*args, **kwargs):
            from app.onboarding.coordinator import DomainView
            view = selected(*args, **kwargs)
            return DomainView(view.facts, {**view.scope, "creator_account_id": "different-account"}, view.reason)
        owner.read = changed
        assert (await owner.execute(child, reconcile=False)).state == "refused"
        assert ceremonies.replacement_client.calls == []
        return
    ceremonies.replacement_client.lost = True
    with pytest.raises(OSError):
        await owner.execute(child, reconcile=False)
    coordinator._settle(child, OperationResult("outcome_unknown", "operation_unconfirmed"))
    assert coordinator.read(native)["authentication"]["state"] == "replacement-pending"
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(replacing_generation=native.generation),
        role="desktop")
    assert coordinator.read(replacement)["authentication"]["state"] == "replacement-pending"
    result = await owner.execute(child, reconcile=True)
    assert result.state == "completed"
    coordinator._settle(child, result)
    assert len(ceremonies.replacement_client.calls) == 1
    assert ceremonies.authentication.client.calls == []
    assert coordinator.receipt(replacement, child["operation_id"])["state"] == "completed"


@pytest.mark.asyncio
async def test_expired_local_presentation_does_not_shorten_live_ceremony_recovery_source(setup):
    coordinator, _, native, clock = setup
    clock[0] += timedelta(seconds=1720)
    coordinator, _, ceremonies, parent, scope, clock, native = await replacement_case(setup)
    source = coordinator.owner.sources.resolve(scope["source_journey_id"])
    clock[0] += timedelta(seconds=81)
    assert parent["expires_at"] < clock[0].timestamp() < source.expires_at.timestamp()
    replacement = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(replacing_generation=native.generation),
        role="desktop")
    snapshot = coordinator.read(replacement)
    assert snapshot["authentication"]["state"] == "replacement-required"
    clicked = {**command(snapshot, "auth.select"), "replace_ceremony": True}
    child, _ = coordinator._accept(replacement, clicked)
    assert (await coordinator.owner.execute(child, reconcile=False)).state == "completed"
    assert len(ceremonies.replacement_client.calls) == 1
    assert coordinator.owner.sources.resolve(scope["source_journey_id"]) == source


@pytest.mark.asyncio
async def test_closed_ceremony_recovery_refusal_is_not_rollback_or_permission_to_resend(setup):
    from app.onboarding.coordinator import OperationResult
    coordinator, _, ceremonies, parent, _, _, native = await replacement_case(setup)
    owner = coordinator.owner
    child, _ = coordinator._accept(native, {**command(coordinator.read(native), "auth.select"), "replace_ceremony": True})
    ceremonies.replacement_client.refused = True
    result = await owner.execute(child, reconcile=False)
    assert result == OperationResult("outcome_unknown", "operation_unconfirmed")
    coordinator._settle(child, result)
    assert (await owner.execute(child, reconcile=True)) == result
    assert coordinator.read(native)["authentication"]["state"] == "replacement-pending"
    assert "auth.select" not in coordinator.read(native)["allowed_actions"]
    assert len(ceremonies.replacement_client.calls) == 1
    # An independently verified original account outcome wins over replacement.
    coordinator._settle(parent, OperationResult("completed"))
    assert (await owner.execute(child, reconcile=True)).state == "refused"
    assert len(ceremonies.replacement_client.calls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_prepared_source_replacement_adopts_only_verified_successor(setup, lost, monkeypatch):
    coordinator, initial, _, old, scope, clock, native = await initial_ceremony_case(setup)
    clock[0] += timedelta(minutes=11)
    terminal = await coordinator.owner.execute(old, reconcile=True)
    coordinator._settle(old, terminal)
    # The recovery owner owns preparation and pointer adoption; isolate the
    # subsequent ordinary ceremony (covered by the ceremony suite).
    connected = []
    async def connect(operation, *, reconcile):
        from app.onboarding.coordinator import OperationResult
        accepted = json.loads(operation["owner_scope_json"])
        current = initial.journeys.require_current(accepted["source_journey_id"])
        assert current["state"] == "waiting"
        assert current["installation_id"] == accepted["installation_id"]
        connected.append(current["journey_id"])
        return OperationResult("waiting")
    monkeypatch.setattr(coordinator.owner, "_connect", connect)
    ready = coordinator.read(native)
    assert ready["continuation_available"]
    operation, _ = coordinator._accept(native, command(ready, "account.connect"))
    replacement = coordinator.owner.source_replacements.clients["initial-enrollment"]
    if lost:
        replacement.mode = "lost"
        outcome = await coordinator.owner.execute(operation, reconcile=False)
        assert outcome.state == "outcome_unknown"
        assert coordinator.owner._source(operation["journey_id"]) == scope["source_journey_id"]
        assert coordinator.read(native)["authentication"]["state"] == "replacement-pending"
        replacement.mode = "prepared"
    outcome = await coordinator.owner.execute(operation, reconcile=lost)
    assert outcome.state == "waiting"
    accepted = json.loads(operation["owner_scope_json"])
    assert coordinator.owner._source(operation["journey_id"]) == accepted["source_journey_id"]
    assert connected == [accepted["source_journey_id"]]
    assert len(initial.client.preparations) == 1
    assert len(replacement.calls) == (2 if lost else 1)
    assert all(call == replacement.calls[0] for call in replacement.calls)

@pytest.mark.asyncio
@pytest.mark.parametrize("expected_customer_id", [None, "customer-original"])
async def test_verified_initial_source_replacement_prepares_new_ceremony_without_fresh_handoff(setup, expected_customer_id):
    coordinator, initial, client, old, scope, clock, native = await initial_ceremony_case(setup)
    clock[0] += timedelta(minutes=11)
    terminal = await coordinator.owner.execute(old, reconcile=True)
    coordinator._settle(old, terminal)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    coordinator.owner.source_replacements.clients["initial-enrollment"].expected_customer_id = expected_customer_id
    # The hosted fixture normally uses a constant ceremony ID. A different
    # source gets a distinct ceremony ID, independently of the source ID.
    original_prepare = client.prepare
    def prepare(request, **kwargs):
        result = original_prepare(request, **kwargs)
        return {**result, "ceremony_id": "0198a1b2-c3d4-7300-8000-000000000099"}
    client.prepare = prepare
    client.mode = "active"
    client.state = "active"
    result = await coordinator.owner.execute(operation, reconcile=False)
    assert result.state == "waiting"
    assert len(initial.client.preparations) == 1
    assert len([call for call in client.calls if call[0] == "prepare"]) == 2
    selected = json.loads(operation["owner_scope_json"])
    assert coordinator.owner._source(operation["journey_id"]) == selected["source_journey_id"]
    ceremony = coordinator.owner.ceremonies.row(operation["operation_id"])
    binding = json.loads(ceremony["binding_json"])
    assert binding["source"]["operation_id"] == selected["successor_operation_id"]
    assert binding.get("expected_customer_id") == expected_customer_id
    assert ("expected_customer_id" in binding) == (expected_customer_id is not None)

async def original_initial_case(setup, *, mode="committed", retained_scope=True, retired=False):
    from types import SimpleNamespace
    from app.security.hosted_grants import BootstrapRecovery, BootstrapRecoveryRefused
    from app.security.initial_handoff import InitialHandoffRefused
    from app.persistence.auth import VerifiedGrantReference
    from hashlib import sha256

    coordinator, initial, client, old, selection, clock, native = await initial_ceremony_case(setup)
    store = coordinator.store
    source_id = selection["source_journey_id"]
    source = initial.journeys.require_current(source_id)
    key = initial.client.key.ensure_ready()
    original_id = source["operation_id"]
    scope = {"onboarding_transaction_id": "original-transaction", "organization_id": "original-org",
        "intended_creator_id": selection["creator_account_id"],
        "association_request_id": "0198a1b2-c3d4-7300-8000-000000000078",
        "installation_id": source["installation_id"], "installation_key_jkt": key.installation_key_jkt,
        "action_digests": {"issue-installation-claim": "a"*64, "approve-creator-association": "b"*64},
        "authorization_revision": 1}
    enrolled = clock[0] + timedelta(minutes=1)
    receipt = {"operation_id": original_id, "installation_id": scope["installation_id"],
        "organization_id": scope["organization_id"], "onboarding_transaction_id": scope["onboarding_transaction_id"],
        "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt,
        "claim_id": "0198a1b2-c3d4-7300-8000-000000000079",
        "consumed_at": enrolled.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    if retained_scope:
        with store.database.transaction() as connection:
            connection.execute("UPDATE onboarding_journeys SET scope_json=? WHERE journey_id=?", (json.dumps(scope), source_id))
    clock[0] += timedelta(minutes=11)
    terminal = await coordinator.owner.execute(old, reconcile=True)
    assert terminal.reason == "operation_expired"
    coordinator._settle(old, terminal)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    replacement = coordinator.owner.source_replacements.clients["initial-enrollment"]
    replacement.mode = mode
    receipt_calls, bootstrap_calls, resumed = [], [], []
    control = SimpleNamespace(receipt_available=mode=="committed", bootstrap_refused=False, transient=retired, invalid_grant=False, bootstrap_transient=False)
    initial.client.key.reopen_existing = lambda: key
    store.installation_key_reference = lambda **_: key
    initial.client.key.ensure_ready = lambda: pytest.fail("Original reconciliation cannot create a key")
    def read_receipt(request, *, before_send, key):
        before_send()
        receipt_calls.append(request)
        if control.transient:
            raise HostedGrantUnavailable("Transient read failure")
        if not control.receipt_available:
            raise InitialHandoffRefused("handoff_refused")
        return dict(receipt)
    initial.client.receipt = read_receipt
    def recover_bootstrap(*, claim_id, recovery_request_id, before_send, existing_key, expected_enrolled_at, adopt_verified):
        before_send()
        bootstrap_calls.append(recovery_request_id)
        assert recovery_request_id == original_id
        assert claim_id == receipt["claim_id"] and expected_enrolled_at == enrolled
        if control.bootstrap_refused:
            raise BootstrapRecoveryRefused("bootstrap_authority_withdrawn")
        if control.bootstrap_transient:
            raise HostedGrantUnavailable("Bootstrap response unavailable")
        refs = tuple(VerifiedGrantReference(reference_id=kind, grant_identifier=kind, grant_type=kind,
            grant_digest=sha256(kind.encode()).hexdigest(), issuer="issuer", subject="subject",
            installation_id=scope["installation_id"], creator_account_id=None,
            valid_from=clock[0]-timedelta(seconds=1), expires_at=clock[0]+timedelta(hours=1),
            verified_at=clock[0], organization_id=scope["organization_id"],
            installation_key_id=key.installation_key_id, installation_key_jkt=key.installation_key_jkt)
            for kind in ("installation_grant", "membership_snapshot"))
        if control.invalid_grant:
            from dataclasses import replace
            refs = (refs[0], replace(refs[1], organization_id="different-org"))
        adopt_verified(refs, enrolled_at=enrolled, recovered_at=clock[0])
        return BootstrapRecovery(tuple(g.reference_id for g in refs), enrolled, clock[0], ())
    initial.grants.recover_bootstrap_v3 = recover_bootstrap
    initial.resume = resumed.append
    return SimpleNamespace(coordinator=coordinator, owner=coordinator.owner, store=store, initial=initial,
        client=client, source_id=source_id, original_id=original_id, operation=operation, clock=clock,
        scope=scope, receipt=receipt, replacement=replacement, receipt_calls=receipt_calls,
        bootstrap_calls=bootstrap_calls, resumed=resumed, control=control, native=native)


def original_journal(case):
    with case.store.database.read() as connection:
        return connection.execute("SELECT * FROM onboarding_initial_replacement_recoveries WHERE predecessor_operation_id=?",
            (case.original_id,)).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,retired", [("accepted",False),("committed",False),("committed",True)])
async def test_initial_replacement_recovers_only_original_enrollment(setup, mode, retired):
    case = await original_initial_case(setup, mode=mode, retired=retired)
    first = await case.owner.execute(case.operation, reconcile=False)
    if retired:
        assert first.state == "outcome_unknown"
        case.clock[0] += timedelta(minutes=21)
        with case.store.database.transaction() as connection:
            case.initial.journeys._cleanup(connection, case.clock[0])
        case.control.transient = False
        first = await case.owner.execute(case.operation, reconcile=True)
    if mode == "accepted":
        assert first.state == "outcome_unknown"
        assert not case.bootstrap_calls and not case.store.unresolved_claim_submissions()
        case.control.receipt_available = True
        case.clock[0] += timedelta(seconds=15)
        first = await case.owner.execute(case.operation, reconcile=True)
    assert first.state == "waiting"
    assert original_journal(case)["completed"] == 1
    assert case.bootstrap_calls == [case.original_id]
    assert all(req["operation_id"] == case.original_id for req in case.receipt_calls)
    assert case.resumed == [case.source_id]
    assert case.owner._source(case.operation["journey_id"]) == case.source_id
    row = case.initial.journeys.require_current(case.source_id)
    assert row["state"] == "completed" and row["operation_id"] == case.original_id
    assert row["handoff_reference"] is None
    assert json.loads(row["scope_json"]) == case.scope
    assert len(case.replacement.calls) == 1 and len(case.initial.client.preparations) == 1
    assert len([call for call in case.client.calls if call[0]=="prepare"]) == 1
    assert len(case.store.consumed_claim_submissions()) == 1
    assert case.store.provisioning_candidate(case.scope["association_request_id"]).state.value == "pending"
    with case.store.database.read() as connection:
        retained,receipt,_,claim=case.initial.lost_enrollment.exact_receipt(connection,operation_id=case.original_id)
        assert receipt==case.receipt and claim["state"]=="consumed"
        assert retained["bootstrap_refusal"] is None
    assert case.owner.observe(case.operation) is None
    await case.owner.execute(case.operation, reconcile=True)
    assert len(case.bootstrap_calls) == 1 and len(case.replacement.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing-scope","wrong-organization","wrong-operation","wrong-completion","bootstrap-refused"])
async def test_initial_replacement_does_not_invent_authority(setup, fault):
    case = await original_initial_case(setup, retained_scope=fault!="missing-scope")
    if fault == "wrong-organization":
        case.receipt["organization_id"] = "different-org"
    elif fault == "wrong-operation":
        case.receipt["operation_id"] = "0198a1b2-c3d4-7300-8000-000000000090"
    elif fault == "wrong-completion":
        case.replacement.completion_id = "0198a1b2-c3d4-7300-8000-000000000090"
    elif fault == "bootstrap-refused":
        case.control.bootstrap_refused = True
    result = await case.owner.execute(case.operation, reconcile=False)
    assert result.state == "outcome_unknown"
    journal = original_journal(case)
    assert not case.resumed and not case.store.consumed_claim_submissions()
    assert case.store.provisioning_candidate(case.scope["association_request_id"]) is None
    if fault == "wrong-completion":
        assert journal is None and not case.receipt_calls
    else:
        assert journal["completed"] == 0
        assert bool(journal["receipt_json"]) == (fault in {"missing-scope","bootstrap-refused"})
        assert bool(journal["blocked"]) == (fault == "bootstrap-refused")
    assert len(case.bootstrap_calls) == (1 if fault == "bootstrap-refused" else 0)
    with case.store.database.read() as connection:
        retained=connection.execute("SELECT * FROM lost_enrollment_evidence").fetchone()
        if fault=="bootstrap-refused":
            assert retained["bootstrap_refusal"]=="bootstrap_authority_withdrawn"
        else:
            assert retained is None
    case.clock[0] += timedelta(minutes=1)
    if fault in {"missing-scope","bootstrap-refused","wrong-completion"}:
        assert not case.owner.automatic_recovery(case.operation)
        await case.owner.execute(case.operation, reconcile=True)
        assert len(case.bootstrap_calls) == (1 if fault == "bootstrap-refused" else 0)
    assert len(case.replacement.calls) == 1 and len(case.initial.client.preparations) == 1
    assert case.owner._source(case.operation["journey_id"]) == case.source_id


@pytest.mark.asyncio
async def test_initial_accepted_receipt_read_budget_survives_owner_restart(setup):
    from test_onboarding_enrollment_runtime import _enrollment_worker
    case = await original_initial_case(setup, mode="accepted")
    assert (await case.owner.execute(case.operation, reconcile=False)).state == "outcome_unknown"
    restarted = _enrollment_worker(case.store, case.initial.client)
    restarted.grants = case.initial.grants
    restarted.resume = case.resumed.append
    restarted.bind_effect_gate(case.owner._effect_gate, restarted._current_journey)
    case.owner.initial = restarted
    await case.owner.execute(case.operation, reconcile=True)
    assert len(case.receipt_calls) == 1
    for delay in (15,30,60):
        case.clock[0] += timedelta(seconds=delay)
        await case.owner.execute(case.operation, reconcile=True)
    assert len(case.receipt_calls) == 3 and original_journal(case)["attempts"] == 3
    assert not case.owner.automatic_recovery(case.operation)
    assert not case.bootstrap_calls and not case.resumed
    assert len(case.replacement.calls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["invalid-grant","lost-bootstrap"])
async def test_original_bootstrap_adoption_is_atomic_and_retries_retained_receipt(setup, fault):
    case = await original_initial_case(setup)
    case.control.invalid_grant = fault == "invalid-grant"
    case.control.bootstrap_transient = fault == "lost-bootstrap"
    result = await case.owner.execute(case.operation, reconcile=False)
    assert result.state == "outcome_unknown"
    assert original_journal(case)["receipt_json"]
    assert not original_journal(case)["completed"]
    assert case.store.claim_submission(case.receipt["claim_id"]).state.value == "submitted"
    assert not case.store.consumed_claim_submissions()
    assert case.store.provisioning_candidate(case.scope["association_request_id"]) is None
    with case.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0] == 0
    case.control.invalid_grant = case.control.bootstrap_transient = False
    case.clock[0] += timedelta(seconds=15)
    result = await case.owner.execute(case.operation, reconcile=True)
    assert result.state == "waiting" and original_journal(case)["completed"] == 1
    assert len(case.receipt_calls) == 1 and case.bootstrap_calls == [case.original_id]*2
    assert len(case.replacement.calls) == 1

@pytest.mark.asyncio
async def test_original_lost_bootstrap_receipt_survives_source_cleanup(setup):
    from app.persistence.lost_enrollment_evidence import BOOTSTRAP_WINDOW
    from app.persistence.source_preparations import SourcePreparations
    from app.security.initial_handoff import timestamp
    case=await original_initial_case(setup)
    case.control.bootstrap_transient=True
    assert (await case.owner.execute(case.operation,reconcile=False)).state=="outcome_unknown"
    with case.store.database.read() as connection:
        assert SourcePreparations(case.store).get(case.original_id,connection=connection) is not None
        assert case.initial.lost_enrollment.exact_receipt(connection)[1]==case.receipt
    case.clock[0]=timestamp(case.receipt["consumed_at"])+BOOTSTRAP_WINDOW
    with case.store.database.transaction() as connection:
        SourcePreparations.cleanup(connection,case.clock[0])
        assert SourcePreparations(case.store).get(case.original_id,connection=connection) is None
        candidate=case.initial.lost_enrollment.eligible(connection)
        assert candidate.claim_id==case.receipt["claim_id"] and candidate.operation_id==case.original_id
        assert candidate.scope["evidence"]=={"kind":"lost-enrollment","claim_id":case.receipt["claim_id"],
            "creator_scope":{"kind":"none"}}
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==0
    assert case.bootstrap_calls==[case.original_id]
    assert not case.store.consumed_claim_submissions()
