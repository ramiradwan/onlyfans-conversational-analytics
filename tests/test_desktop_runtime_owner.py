import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.onboarding.admission import ScopedAdmissions
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.onboarding.runtime import RuntimeActor, RuntimeSetupOwner
from app.persistence.auth import (AuthorizedAccountBinding, ProvisioningCandidate, ProvisioningCandidateState,
    RevocationKey, RevocationScopeType)
from app.security.hosted_grants import HostedGrantUnavailable
from test_desktop_coordinator import command
from test_webauthn_routes import _seeded_store, _policy, _register_public_key, INSTANT, ACCOUNT_ID, INSTALLATION_ID

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.fixture
def setup(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    store.record_provisioning_candidate(ProvisioningCandidate("association-http", INSTALLATION_ID,
        "onboarding-http", "organization-http", ACCOUNT_ID, ProvisioningCandidateState.PENDING, INSTANT))
    assert store.approve_provisioning_candidate("association-http", resolved_at=INSTANT)
    store.record_authorized_account_binding(AuthorizedAccountBinding(ACCOUNT_ID, INSTALLATION_ID,
        "platform-http", "association-http", "a" * 64, INSTANT,
        ("installation_grant-http", "membership_snapshot-http", "creator_account_binding-http")))
    admissions = ScopedAdmissions(store, stage="runtime")
    tasks = BrowserTasks(admissions, extension_id="a" * 32)
    owner = RuntimeSetupOwner(store, browser_tasks=tasks, activation=SimpleNamespace(decision=lambda *a, **k: None, decision_status=lambda *a, **k: None, authentication=lambda *a, **k: None))
    journey = str(uuid4())
    with store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)", (journey, INSTANT.timestamp()))
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    session = admissions.consume(admissions.prepare_desktop(), role="desktop")
    return coordinator, owner, session, RuntimeActor(lambda: _policy(store))


def test_runtime_reads_use_local_authority_without_creating_an_operator_session(setup):
    coordinator, owner, native, _ = setup
    with owner.store.database.read() as connection:
        before = connection.total_changes
        view = owner.read(native.journey_id, connection=connection)
        assert connection.total_changes == before
    assert view.facts["account"] == view.facts["installation"] == view.facts["configuration"] == "verified"
    assert view.facts["enrollment"] == "missing"
    assert view.facts["activation"] == "missing"
    assert view.scope["actor"] is None and view.scope["organization_id"] == "organization-http"
    assert not view.permitted_actions
    assert coordinator.read(native)["next_task"] == "finish-setup"
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM bridge_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] == 0


@pytest.mark.parametrize("role", [None, "creator", "operator", "agent"])
def test_rich_readiness_preserves_creator_disclosure_and_computes_once(setup, monkeypatch, role):
    from dataclasses import replace
    from app.onboarding import runtime
    from app.security.analysis_authorization import AnalysisReadiness
    coordinator, owner, native, _ = setup
    policy = _policy(owner.store)
    local = owner._local_authority(policy)
    policy = replace(policy, identity=None if role is None else replace(policy.identity, role=role))
    calls = []
    monkeypatch.setattr(owner, "_local_authority", lambda *args, **kwargs: local)
    monkeypatch.setattr(runtime, "current_analysis_readiness", lambda *args: calls.append(args) or AnalysisReadiness("active", "blocked"))
    monkeypatch.setattr("app.security.activation_gate.runtime_is_activated", lambda: True)
    with owner.store.database.read() as connection:
        changes = connection.total_changes
        snapshot = coordinator.read(native, actor=RuntimeActor(lambda: policy))
        assert connection.total_changes == changes
    assert len(calls) == 1
    value = snapshot["analysis_readiness"]
    if role == "creator":
        assert value == {"availability": "current", "creator_account_id": ACCOUNT_ID, "value": {
            "schema": "ofca-analysis-readiness/v1", "commercial_authority": "active", "analysis_admission": "blocked"}}
    else:
        assert value == {"availability": "unauthorized", "creator_account_id": None, "value": None}


def test_readiness_enrichment_cannot_change_existing_command_guards(setup, monkeypatch):
    from app.onboarding.coordinator import DomainView
    from app.onboarding.readiness import DesktopAnalysisReadiness
    from app.security.analysis_authorization import AnalysisReadiness
    coordinator, owner, native, actor = setup
    view = owner.read(native.journey_id, actor=actor)
    snapshots = []
    for readiness in [DesktopAnalysisReadiness(), DesktopAnalysisReadiness("current", ACCOUNT_ID, AnalysisReadiness("active", "blocked"))]:
        monkeypatch.setattr(owner, "read", lambda *args, readiness=readiness, **kwargs: DomainView(
            view.facts, view.scope, view.reason, view.permitted_actions, readiness))
        snapshots.append(coordinator.read(native, actor=actor))
    assert snapshots[0]["analysis_readiness"] != snapshots[1]["analysis_readiness"]
    assert {name: snapshots[0][name] for name in ("generation", "revision", "scope_digest", "allowed_actions")} == {
        name: snapshots[1][name] for name in ("generation", "revision", "scope_digest", "allowed_actions")}


@pytest.mark.asyncio
async def test_readiness_stream_observes_expiry_without_a_commit(setup, monkeypatch):
    from datetime import timedelta
    from starlette.requests import ClientDisconnect
    from app.onboarding.routes import DesktopEventResponse
    from app.provisioning.state import next_authority_expiry
    coordinator, owner, native, actor = setup
    clock = [INSTANT + timedelta(hours=1) - timedelta(milliseconds=20)]
    monkeypatch.setattr(owner.store, "_clock", lambda: clock[0])
    monkeypatch.setattr("app.security.activation_gate.runtime_is_activated", lambda: True)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(
        replacing_generation=native.generation), role="desktop")
    frames, active = [], set()
    response = DesktopEventResponse(coordinator, native, actor=lambda: actor, active=active,
        deadline=lambda: next_authority_expiry(owner.store) or 1800)

    async def send(message):
        if message["type"] == "http.response.body" and message["body"].startswith(b"event: setup"):
            frames.append(json.loads(message["body"].decode().split("data: ", 1)[1]))
            if len(frames) == 1:
                asyncio.get_running_loop().call_later(.005,
                    lambda: clock.__setitem__(0, INSTANT + timedelta(hours=1)))
            else:
                raise OSError("disconnected")

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 1)
    assert frames[0]["analysis_readiness"]["availability"] == "current"
    assert frames[1]["analysis_readiness"] == {"availability": "unauthorized", "creator_account_id": None, "value": None}
    assert not active
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] == 0


def test_new_runtime_mutations_require_operator_and_keep_credentials_out_of_scope(setup):
    coordinator, owner, native, actor = setup
    authorized = owner.read(native.journey_id, actor=actor)
    assert authorized.permitted_actions == {"activation.review", "activation.confirm", "browser.access", "auth.select", "auth.cancel", "auth.switch"}
    assert authorized.scope["actor"]["creator_account_id"] == ACCOUNT_ID
    encoded = json.dumps(authorized.scope)
    assert "bootstrap-session-http" not in encoded and "session_id" not in encoded
    with owner.store.database.transaction() as connection:
        with pytest.raises(HostedGrantUnavailable):
            owner.admit(connection, native.journey_id, action="activation.review", scope=authorized.scope)
        assert owner.admit(connection, native.journey_id, action="activation.review", scope=authorized.scope, actor=actor).journey_id == native.journey_id


def test_runtime_guard_rechecks_current_authority_under_the_admission_transaction(setup):
    coordinator, owner, native, actor = setup
    view = owner.read(native.journey_id, actor=actor)
    operation = {"action": "activation.review", "journey_id": native.journey_id,
        "owner_scope_json": json.dumps(view.scope)}
    with owner.store.database.transaction() as connection:
        assert owner.authorize(operation, actor, connection=connection).identity is not None
    owner.store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_ID))
    with pytest.raises(HostedGrantUnavailable):
        owner.authorize(operation, actor)


@pytest.mark.parametrize("domain_status", [None, "pending", "committed", "revoked"])
def test_installed_license_does_not_clear_an_uncertain_commercial_operation(setup, monkeypatch, domain_status):
    from app.onboarding.activation import DesktopActivation
    from app.onboarding.coordinator import DomainView

    _, owner, native, actor = setup
    selected = owner.read(native.journey_id, actor=actor)
    activation = DesktopActivation(owner.store, key=None, registered=None, browser_tasks=None,
        redemption=None, ceremonies=SimpleNamespace(row=lambda _: (
            None if domain_status is None else {"domain_status": domain_status})))
    owner.activation = activation
    monkeypatch.setattr(owner, "read", lambda _: DomainView(
        {**selected.facts, "activation": "verified"}, selected.scope))
    operation = {"operation_id": "accepted-commercial-operation", "action": "activation.review",
        "journey_id": native.journey_id, "owner_scope_json": json.dumps(selected.scope)}
    result = owner.observe(operation)
    if domain_status in {None, "committed"}:
        assert result.state == "completed"
    else:
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["browser.access", "account.connect", "setup.finalize"])
async def test_configured_restart_recovers_the_original_setup_owner_without_operator_admission(setup, action):
    from app.onboarding.coordinator import OperationResult

    _, owner, native, _ = setup
    recovered = []

    async def execute(operation, *, reconcile):
        recovered.append((operation, reconcile))
        return OperationResult("waiting")

    owner.setup_owner = SimpleNamespace(observe=lambda _: None, execute=execute)
    operation = {"operation_id": "accepted-before-restart", "action": action, "journey_id": native.journey_id, "owner_scope_json": "{}"}
    result = await owner.execute(operation, reconcile=True)
    assert result.state == "waiting"
    assert recovered == [(operation, True)]
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM bridge_sessions").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_account", [False, True])
async def test_runtime_browser_access_uses_the_exact_operator_task_and_creator(setup, matching_account):
    from test_desktop_browser_tasks import observation

    coordinator, owner, native, actor = setup
    _register_public_key(owner.store, ec.generate_private_key(ec.SECP256R1()))
    snapshot = coordinator.read(native, actor=actor)
    assert "browser.access" in snapshot["allowed_actions"]
    assert "browser.access" not in coordinator.read(native)["allowed_actions"]
    request = command(snapshot)
    await coordinator.command(native, request, actor=actor)
    await coordinator.drain()
    native = coordinator.admissions.require(native.identifier, role="desktop", generation=native.generation)
    task = owner.browser_tasks.current_native(native)[0]
    code = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task["task_id"])
    browser = coordinator.admissions.consume(code, role="browser_task")
    owner.browser_tasks.observe(browser, {**observation(browser), "creator_account_id": ACCOUNT_ID if matching_account else "another-account"})
    await coordinator.reconcile(native, request["operation_id"])
    await coordinator.drain()
    assert coordinator.receipt(native, request["operation_id"])["state"] == ("completed" if matching_account else "refused")
    if matching_account:
        # A new explicit repair cannot complete from the previous task's status.
        second = command(coordinator.read(native, actor=actor))
        await coordinator.command(native, second, actor=actor)
        await coordinator.drain()
        assert coordinator.receipt(native, second["operation_id"])["state"] == "waiting"
        with owner.store.database.read() as connection:
            operation = dict(connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?", (second["operation_id"],)).fetchone())
        assert owner.observe(operation) is None
        assert (await owner.execute(operation, reconcile=True)).state == "outcome_unknown"
        owner.store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_ID))
        with pytest.raises(HostedGrantUnavailable):
            await owner.execute(operation, reconcile=True, actor=actor)


@pytest.mark.asyncio
async def test_watcher_survives_an_unavailable_fact_and_reconciles_later(tmp_path):
    from test_desktop_coordinator import setup as core_setup
    from app.onboarding.coordinator import OperationResult
    from app.provisioning.events import events

    coordinator, owner, native, _ = core_setup.__wrapped__(tmp_path)
    owner.result = OperationResult("waiting")
    request = command(coordinator.read(native))
    await coordinator.command(native, request)
    await coordinator.drain()
    reads = asyncio.Event()

    def unavailable(operation):
        reads.set()
        raise OSError("Owner read is unavailable")

    owner.observe = unavailable
    coordinator.start()
    await asyncio.wait_for(reads.wait(), 1)
    assert not coordinator._watcher.done()
    owner.observe = lambda operation: OperationResult("completed")
    events.publish()
    for _ in range(100):
        if coordinator.receipt(native, request["operation_id"])["state"] == "completed":
            break
        await asyncio.sleep(0)
    assert coordinator.receipt(native, request["operation_id"])["state"] == "completed"
    await coordinator.stop()
