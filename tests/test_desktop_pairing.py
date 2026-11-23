import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.endpoints.companion_session import ProtectedSocket
from app.core.config import settings
from app.onboarding.admission import AdmissionRefused, ScopedAdmissions, csrf_token
from app.onboarding.browser_routes import browser_router
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.onboarding.pairing import DesktopPairing
from app.onboarding.runtime import RuntimeActor, RuntimeSetupOwner
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.persistence.factory import create_canonical_repositories
from app.provisioning.events import events
from app.security.companion_pairing import CompanionPairingError, encode_pairing_id
from app.security.companion_pairing_proof import comparison_code
from app.security.companion_session_authority import CompanionSessionAuthority, CompanionSessionError
from app.security.hosted_grants import HostedGrantUnavailable
from app.transport.account_bound_manager import AccountBoundTransportManager
from test_companion_onboarding_progress import _bind, _config_applied
from test_companion_pairing_service import _agent_confirmation, contract as contract, grant_references as grant_references, local as local
from test_companion_session_authority import _hello
from test_desktop_coordinator import command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.fixture
def setup(local):
    admissions = ScopedAdmissions(local.store, stage="runtime")
    tasks = BrowserTasks(admissions, extension_id="a" * 32)
    pairing = DesktopPairing(local.store, browser_tasks=tasks, service=local.service)
    owner = RuntimeSetupOwner(local.store, browser_tasks=tasks, activation=None, pairing=pairing)
    with local.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)",
            (str(uuid4()), local.clock.now.timestamp()))
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    return SimpleNamespace(local=local, coordinator=coordinator, owner=owner, native=native,
        actor=RuntimeActor(lambda: local.store.build_runtime_policy(local.policy.identity)), tasks=tasks, pairing=pairing, admissions=admissions)


async def begin(setup):
    request = command(setup.coordinator.read(setup.native, actor=setup.actor), "browser.pair")
    await setup.coordinator.command(setup.native, request, actor=setup.actor)
    await setup.coordinator.drain()
    with setup.local.store.database.read() as connection:
        operation = dict(connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (request["operation_id"],)).fetchone())
    assert operation["state"] == "waiting"
    task = setup.tasks.current_native(setup.native)[0]
    code = setup.admissions.reissue_task_entry(setup.native.identifier, generation=setup.native.generation, task_id=task["task_id"])
    helper = setup.admissions.consume(code, role="browser_task")
    return operation, helper


def awaiting(setup):
    service = setup.local.service
    claimed = service.claim()
    service.offer(claimed, setup.local.request)
    window = service.persistence.window(claimed.pairing_id)
    service.confirm_agent(claimed.pairing_id, _agent_confirmation(setup.local, window))
    return window.pairing_id, {"comparison_code": comparison_code(window.pairing_digest)}


def apply_initial_configuration(setup, pairing_id):
    session = setup.local.service.persistence.session_authority(pairing_id).binding("configured-companion")
    assert setup.local.store.record_companion_configuration(session)
    assert not setup.local.store.record_companion_configuration(session)


@pytest.mark.asyncio
async def test_real_pairing_commits_only_the_independent_code_for_this_operator_task(setup, monkeypatch):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    assert setup.pairing.observe(operation) is None
    assert setup.local.service.persistence.companion_pin(pairing_id) is None
    payload = setup.tasks.attachment(setup.admissions.task(helper.identifier, generation=helper.generation))
    assert "comparison_code" not in json.dumps(payload)
    await setup.pairing.compare(helper, operation, code)
    pin = setup.local.service.persistence.companion_pin(pairing_id)
    assert pin.creator_account_id == setup.local.account
    # A committed comparison survives both the missing response and its request
    # authority being discarded during a Brain restart.
    restored = DesktopPairing(setup.local.store, browser_tasks=setup.tasks, service=setup.local.service)
    setup.owner.pairing = restored
    assert restored.observe(operation) is None
    assert not setup.coordinator.read(setup.native, actor=setup.actor)["local_ready"]
    await restored.compare(helper, operation, code)
    # The live transport can finish local setup even without a hosted progress reporter.
    store = setup.local.store
    monkeypatch.setattr(settings, "auth_database_path", str(store.database.path))
    # The signed contract vectors and both configuration resolvers share the fixture clock.
    monkeypatch.setattr("app.transport.manager.utc_now", store._now)
    monkeypatch.setattr("app.transport.account_bound_manager.datetime", SimpleNamespace(now=lambda _zone: store._now()))
    manager = AccountBoundTransportManager(create_canonical_repositories(), onboarding_progress=None)
    authority = CompanionSessionAuthority(store)
    snapshot = authority.prepare(pairing_id)
    session = authority.authorize(snapshot)
    with pytest.raises(CompanionSessionError):
        session.record_initial_configuration()
    _hello((setup.local, authority, snapshot, session))
    socket = ProtectedSocket(SimpleNamespace(authority=session, websocket=SimpleNamespace(client=None)))
    lease = await _bind(manager, socket, session.binding)
    required = manager.required_config_document(session.binding.creator_account_id)
    await manager.record_config_applied(lease, _config_applied(lease, required.config_revision, required.digest, 'degraded'))
    assert restored.observe(operation) is None
    await manager.record_config_applied(lease, _config_applied(lease, required.config_revision, required.digest))
    assert not session.record_initial_configuration()
    await setup.coordinator.reconcile(setup.native, operation["operation_id"])
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, operation["operation_id"])["state"] == "completed"
    snapshot = setup.coordinator.read(setup.native, actor=setup.actor)
    assert snapshot["local_ready"]
    with setup.local.store.database.read() as connection:
        row = connection.execute("SELECT companion_confirmation_method FROM agent_pairings WHERE pairing_id=?", (encode_pairing_id(pairing_id),)).fetchone()
        assert row["companion_confirmation_method"] == "browser_verified"


@pytest.mark.asyncio
async def test_restart_requires_fresh_operator_then_recovers_the_same_pairing_window(setup):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    original = setup.local.service.persistence.window(pairing_id)
    restored = DesktopPairing(setup.local.store, browser_tasks=setup.tasks, service=setup.local.service)
    setup.owner.pairing = restored
    await setup.coordinator.reconcile(setup.native, operation["operation_id"])
    await setup.coordinator.drain()
    with pytest.raises(AdmissionRefused):
        await restored.compare(helper, operation, code)
    await setup.coordinator.reconcile(setup.native, operation["operation_id"], actor=setup.actor)
    await setup.coordinator.drain()
    assert setup.local.service.persistence.window(pairing_id) == original
    await restored.compare(helper, operation, code)
    assert restored.observe(operation) is None
    apply_initial_configuration(setup, pairing_id)
    assert restored.observe(operation).state == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_at_deadline", [False, True])
async def test_interrupted_extension_can_retry_without_disconnecting_a_confirmed_pin(setup, echo_at_deadline):
    operation, helper = await begin(setup)
    original_id, code = awaiting(setup)
    await setup.pairing.compare(helper, operation, code)
    assert setup.pairing.observe(operation) is None
    deadline = setup.owner.confirmation_deadline(operation)
    assert deadline == setup.local.clock.now.timestamp() + 300
    setup.local.clock.now += timedelta(seconds=300)
    if echo_at_deadline:
        apply_initial_configuration(setup, original_id)
        assert setup.pairing.observe(operation).reason == "operation_expired"
        return
    await setup.coordinator.reconcile(setup.native, operation["operation_id"], actor=setup.actor)
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, operation["operation_id"])["reason"] == "operation_expired"
    assert not setup.coordinator.read(setup.native, actor=setup.actor)["local_ready"]
    retry, next_helper = await begin(setup)
    next_id, next_code = awaiting(setup)
    assert next_id != original_id
    await setup.pairing.compare(next_helper, retry, next_code)
    apply_initial_configuration(setup, next_id)
    assert setup.pairing.observe(retry).state == "completed"


@pytest.mark.asyncio
async def test_lost_open_reply_recovers_once_without_renewing_keys_or_expiry(setup, monkeypatch):
    original_open = setup.local.service.open
    def lost_reply(*args, **kwargs):
        original_open(*args, **kwargs)
        raise OSError("synthetic lost reply")
    monkeypatch.setattr(setup.local.service, "open", lost_reply)
    request = command(setup.coordinator.read(setup.native, actor=setup.actor), "browser.pair")
    await setup.coordinator.command(setup.native, request, actor=setup.actor)
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, request["operation_id"])["state"] == "outcome_unknown"
    row = setup.pairing._row(request["operation_id"])
    original = setup.local.service.persistence.window(row["pairing_id"])
    setup.local.clock.now += timedelta(seconds=30)
    monkeypatch.setattr(setup.local.service, "open", original_open)
    await setup.coordinator.reconcile(setup.native, request["operation_id"], actor=setup.actor)
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, request["operation_id"])["state"] == "waiting"
    assert setup.local.service.persistence.window(row["pairing_id"]) == original
    assert setup.tasks.current_native(setup.native)[0]["expires_at"] == row["expires_at"]


@pytest.mark.asyncio
async def test_mismatched_code_ends_the_exact_attempt_without_a_retry_window(setup):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    wrong = "000000" if code["comparison_code"] != "000000" else "111111"
    with pytest.raises(CompanionPairingError, match="pairing_proof_refused"):
        await setup.pairing.compare(helper, operation, {"comparison_code": wrong})
    assert setup.local.service.persistence.companion_pin(pairing_id) is None
    assert setup.pairing.observe(operation).state == "refused"
    await setup.coordinator.reconcile(setup.native, operation["operation_id"], actor=setup.actor)
    await setup.coordinator.drain()
    with setup.local.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM companion_pairing_windows").fetchone()[0] == 1
    assert setup.local.service.persistence.window(pairing_id).state.value == "declined"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "account", "deadline", "shape"])
async def test_stale_or_revoked_pairing_comparisons_cannot_commit(setup, change):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    if change == "task":
        setup.admissions.reissue_task_entry(setup.native.identifier, generation=setup.native.generation, task_id=helper.task_id)
    elif change == "account":
        setup.local.store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, setup.local.account))
    elif change == "deadline":
        setup.local.clock.now += timedelta(seconds=301)
    else:
        code["pairing_id"] = "a browser cannot choose the pairing window"
    with pytest.raises((AdmissionRefused, HostedGrantUnavailable, CompanionPairingError)):
        await setup.pairing.compare(helper, operation, code)
    if change == "deadline":
        assert setup.pairing.observe(operation).reason == "operation_expired"
    assert setup.local.service.persistence.companion_pin(pairing_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_replacement_fences_the_comparison_inside_its_mutation_transaction(setup, monkeypatch, matches):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    persistence = setup.local.service.persistence
    name = "confirm_and_admit" if matches else "cancel_authorized_window"
    original = getattr(persistence, name)
    def replaced(*args, **kwargs):
        setup.admissions.reissue_task_entry(setup.native.identifier, generation=setup.native.generation, task_id=helper.task_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(persistence, name, replaced)
    if not matches:
        code["comparison_code"] = "000000" if code["comparison_code"] != "000000" else "111111"
    with pytest.raises(AdmissionRefused):
        await setup.pairing.compare(helper, operation, code)
    assert persistence.companion_pin(pairing_id) is None
    assert persistence.window(pairing_id).state.value == "awaiting_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_early_port_code_waits_for_the_agent_proof_commit_and_stops_on_shutdown(setup, stop):
    operation, helper = await begin(setup)
    service = setup.local.service
    claimed = service.claim()
    service.offer(claimed, setup.local.request)
    offered = service.persistence.window(claimed.pairing_id)
    value = {"comparison_code": comparison_code(offered.pairing_digest)}
    waiting = asyncio.create_task(setup.pairing.compare(helper, operation, value))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert service.persistence.companion_pin(claimed.pairing_id) is None
    with pytest.raises(AdmissionRefused, match="comparison_pending"):
        await setup.pairing.compare(helper, operation, value)
    if stop:
        await setup.pairing.stop()
        with pytest.raises(AdmissionRefused):
            await waiting
        assert service.persistence.companion_pin(claimed.pairing_id) is None
    else:
        service.confirm_agent(claimed.pairing_id, _agent_confirmation(setup.local, offered))
        events.publish()
        await waiting
        assert service.persistence.companion_pin(claimed.pairing_id) is not None
    assert not setup.pairing._comparisons


@pytest.mark.asyncio
async def test_browser_route_requires_admission_and_csrf_and_returns_no_brain_code(setup):
    operation, helper = await begin(setup)
    pairing_id, code = awaiting(setup)
    app = FastAPI()
    app.include_router(browser_router(setup.coordinator, setup.tasks, None, pairing=setup.pairing))
    headers = {"Origin": "http://bridge.localhost:17871", "X-Desktop-Generation": helper.generation,
        "X-Desktop-CSRF": csrf_token(helper.identifier), "Cookie": "__Host-browser_task=" + helper.identifier}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge.localhost:17871") as client:
        rejected = await client.post("/api/v1/browser-tasks/pairing", json=code, headers={**headers, "X-Desktop-CSRF": "wrong"})
        assert rejected.status_code == 403
        assert setup.local.service.persistence.companion_pin(pairing_id) is None
        response = await client.post("/api/v1/browser-tasks/pairing", json=code, headers=headers)
    assert response.status_code == 202 and response.json() == {"state": "checking"}
    assert response.headers["Cache-Control"] == "no-store"
    assert code["comparison_code"] not in response.text
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, operation["operation_id"])["state"] == "waiting"
    apply_initial_configuration(setup, pairing_id)
    await setup.coordinator.reconcile(setup.native, operation["operation_id"])
    await setup.coordinator.drain()
    assert setup.coordinator.receipt(setup.native, operation["operation_id"])["state"] == "completed"
