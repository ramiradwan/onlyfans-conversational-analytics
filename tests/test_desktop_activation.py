import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.activation import DesktopActivation
from app.onboarding.admission import ScopedAdmissions
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.onboarding.runtime import RuntimeSetupOwner
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.provisioning.events import events
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.hosted_grants import HostedGrantUnavailable
from test_desktop_coordinator import Owner, command
from test_registered_continuation_context import owner as registered_owner
from test_runtime_activation_entry import installed

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.fixture
def setup(tmp_path):
    owner = registered_owner.__wrapped__(tmp_path)
    previous = installed(owner)
    admissions = ScopedAdmissions(owner.store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    projection = Owner()
    projection.facts = dict.fromkeys(projection.facts, "verified")
    projection.facts["activation"] = "missing"
    key = owner.key.reopen_existing()
    projection.scope = {"creator_account_id": "creator-1", "installation_id": "installation-1",
        "organization_id": "organization-1", "key": {"installation_key_id": key.installation_key_id,
            "installation_key_jkt": key.installation_key_jkt}}
    coordinator = DesktopOnboardingCoordinator(admissions, projection)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    calls = []

    def redeem(**value):
        calls.append(value)
        return CapabilityLicenseDeliveryReceipt("reference", "license", "issuance")

    activation = DesktopActivation(owner.store, key=owner.key, registered=None, browser_tasks=None,
        ceremonies=SimpleNamespace(row=lambda _: None),
        redemption=SimpleNamespace(resume_activation=redeem, prepare_activation=lambda **_: True))
    return activation, owner, operation, previous, calls


def authorized(connection=None):
    if connection is not None:
        assert connection.in_transaction


@pytest.mark.parametrize("extension_id", [None, "a" * 32])
def test_completion_binding_uses_release_extension_without_a_live_helper(setup, extension_id):
    activation, _, operation, _, _ = setup
    source = activation._reserve(operation, authorized)
    activation.registered = SimpleNamespace(_request=lambda _: {"profile": "synthetic"})
    activation.browser_tasks = SimpleNamespace(extension_id=extension_id,
        read=lambda _: pytest.fail("A paired ceremony does not require an old browser observation"))
    binding = activation._binding(operation, source)
    assert binding["completion"] == ({"mode": "system-browser"} if extension_id is None
        else {"mode": "extension-auth-flow", "extension_id": extension_id})


def test_source_is_reserved_once_and_never_replaced_after_a_lost_response(setup):
    activation, owner, operation, previous, _ = setup
    first = activation._reserve(operation, authorized)
    assert first["source_journey_id"] == previous
    owner.clock[0] += timedelta(minutes=31)
    second = activation._reserve(operation, authorized)
    assert second == first
    assert len(owner.store.authorized_account_bindings()) == 1


def test_installed_activation_does_not_depend_on_a_live_old_browser_presentation(setup):
    _, owner, _, previous, _ = setup
    owner.clock[0] += timedelta(days=3)
    # New, explicitly authenticated desktop intent uses the durable installed
    # target. The old presentation remains expired and cannot be renewed.
    row = owner.contexts.runtime_desktop_context(creator_account_id="creator-1",
        reopened_key=owner.key.reopen_existing(), authorize=authorized)
    assert row["journey_id"] != previous
    assert row["scope"]["creator_account_id"] == "creator-1"
    assert row["installation_id"] == "installation-1"


def test_source_reservation_refuses_a_revoked_local_binding(setup):
    activation, owner, operation, _, _ = setup
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE authorized_account_bindings SET revoked_at=?", (owner.clock[0].isoformat(),))
    with pytest.raises(OnboardingJourneyUnavailable):
        activation._reserve(operation, authorized)
    assert activation._source(operation["operation_id"]) is None


@pytest.mark.asyncio
async def test_restart_without_an_operator_cannot_start_a_new_ceremony(setup):
    activation, _, operation, _, calls = setup
    result = await activation.execute(operation, authorize=lambda: pytest.fail("No presentation authority at restart"), can_prepare=False)
    assert result.state == "outcome_unknown" and not calls
    assert activation._source(operation["operation_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "operation", "expired", "key"])
async def test_redemption_uses_exact_committed_result_and_existing_delivery_owner(setup, change):
    activation, owner, operation, _, calls = setup
    source = activation._reserve(operation, authorized)
    selected = json.loads(operation["owner_scope_json"])
    result = {"kind": "activation-redemption", "activation_operation_id": source["activation_operation_id"],
        "redemption": {"profile": "urn:bridge-clean:capability-license-redemption-continuation:v1",
            "continuation": "clr1." + "A" * 43, "expires_at": (owner.clock[0] + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
    if change == "operation":
        result["activation_operation_id"] = "00000000-0000-7000-8000-000000000009"
    elif change == "expired":
        result["redemption"]["expires_at"] = owner.clock[0].strftime("%Y-%m-%dT%H:%M:%S.000Z")
    elif change == "key":
        selected["key"]["installation_key_id"] = "different"
        operation["owner_scope_json"] = json.dumps(selected)
    if change in {"operation", "key"}:
        with pytest.raises(HostedGrantUnavailable):
            await activation._redeem(operation, result)
    else:
        outcome = await activation._redeem(operation, result)
        assert outcome.state == "waiting"
    if change in {None, "expired"}:
        assert calls == [{"activation_operation_id": source["activation_operation_id"],
            "binding": activation._delivery_binding(operation), "initial_redemption": result["redemption"]}]
    else:
        assert not calls
    with owner.store.database.read() as connection:
        assert "clr1." not in str([tuple(row) for row in connection.execute("SELECT * FROM desktop_activation_sources")])


@pytest.fixture
def pending_activation(setup):
    activation, owner, operation, _, deliveries = setup
    source = activation._reserve(operation, authorized)
    deadline = (owner.clock[0] + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row = {"domain_status": "awaiting-confirmation", "binding_json": json.dumps({"source_expires_at": deadline}),
        "provider_expires_at": deadline}
    state = {"decision": "unavailable", "open_browser": False, "response": "found"}
    reads, completed = [], []

    def protected(*args, **kwargs):
        pytest.fail("Read recovery cannot prepare, authenticate, confirm or open a browser")

    async def read(kind, operation_id, authorize):
        assert operation_id == operation["operation_id"]
        authorize()
        reads.append(kind)
        if kind == "ceremony":
            return {"status": state["response"], "snapshot": {"domain_status": row["domain_status"]}}
        if kind == "authentication":
            return {"state": "admitted", "actions": {"open_browser": state["open_browser"]}}
        return {"state": state["decision"]}

    activation.ceremonies = SimpleNamespace(row=lambda _: row,
        reconcile=lambda operation_id, *, authorize: read("ceremony", operation_id, authorize), prepare=protected,
        authentication=SimpleNamespace(refresh=lambda operation_id, *, authorize: read("authentication", operation_id, authorize),
            execute=protected),
        decisions=SimpleNamespace(refresh=lambda operation_id, *, authorize: read("decision", operation_id, authorize),
            execute=protected), browser_entry=protected)
    activation.registered = SimpleNamespace(prepare=protected)
    activation.redemption.prepare_activation = protected
    activation.browser_tasks = SimpleNamespace(prepare=protected, complete=completed.append)
    return activation, owner, operation, source, row, state, reads, completed, deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["retry-ready", "existing-offer"])
async def test_coordinator_keeps_unavailable_activation_and_observes_same_operation_readiness(pending_activation, later):
    activation, owner, operation, source, row, state, reads, completed, deliveries = pending_activation
    original = dict(operation), dict(row), dict(source)
    projection = Owner()
    finished = asyncio.Event()

    async def execute(accepted, *, reconcile, actor=None):
        assert reconcile and actor is None
        result = await activation.execute(accepted, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
        finished.set()
        return result

    projection.execute = execute
    projection.automatic_recovery = lambda accepted: RuntimeSetupOwner.automatic_recovery(
        SimpleNamespace(activation=activation), accepted)
    coordinator = DesktopOnboardingCoordinator(ScopedAdmissions(owner.store, stage="runtime"), projection)
    coordinator.start()
    try:
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        await coordinator.drain()
        pending = coordinator._pending()
        assert [(item["operation_id"], item["state"], item["reason"]) for item in pending] == [
            (operation["operation_id"], "waiting", "activation_unavailable")]
        assert not completed and not deliveries
        finished.clear()
        state["open_browser"] = later == "retry-ready"
        state["decision"] = "confirmation-required" if later == "existing-offer" else "unavailable"
        coordinator._retry_at[operation["operation_id"]] = 0
        events.publish()
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        await coordinator.drain()
        assert reads == ["ceremony", "authentication", "decision"] * 2
        assert completed == ([operation["operation_id"]] if later == "existing-offer" else [])
        assert not deliveries
        pending = coordinator._pending()
        assert len(pending) == 1 and pending[0]["state"] == "waiting"
        assert pending[0]["operation_id"] == operation["operation_id"]
        assert pending[0]["expires_at"] == operation["expires_at"]
        assert (operation, row, activation._source(operation["operation_id"])) == original
        with owner.store.database.read() as connection:
            assert connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM desktop_activation_sources").fetchone()[0] == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", ["operation", "source", "ceremony"])
async def test_unavailable_activation_after_original_deadline_is_unknown_and_still_readable(pending_activation, deadline):
    activation, owner, operation, source, row, _, reads, _, deliveries = pending_activation
    now = owner.clock[0].strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if deadline == "operation":
        operation["expires_at"] = owner.clock[0].timestamp()
    elif deadline == "source":
        row["binding_json"] = json.dumps({"source_expires_at": now})
    else:
        row["provider_expires_at"] = now
    original = dict(operation), dict(row), dict(source)
    for _ in range(2):
        result = await activation.execute(operation, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
        assert (result.state, result.reason) == ("outcome_unknown", "operation_unconfirmed")
        assert RuntimeSetupOwner.automatic_recovery(SimpleNamespace(activation=activation), operation)
    assert reads == ["ceremony", "authentication", "decision"] * 2
    assert not deliveries and (operation, row, activation._source(operation["operation_id"])) == original


@pytest.mark.asyncio
async def test_unavailable_read_does_not_settle_activation_as_noncommit(pending_activation):
    activation, _, operation, _, _, state, _, _, deliveries = pending_activation
    state["response"] = "unavailable"
    result = await activation.execute(operation, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
    assert (result.state, result.reason) == ("outcome_unknown", "operation_unconfirmed")
    assert not deliveries


@pytest.mark.asyncio
async def test_revoked_activation_is_terminal_without_preparation_or_further_reads(pending_activation):
    activation, _, operation, _, row, _, reads, _, deliveries = pending_activation
    row["domain_status"] = "revoked"
    result = await activation.execute(operation, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
    assert (result.state, result.reason) == ("refused", "operation_refused")
    assert reads == ["ceremony"] and not deliveries


@pytest.mark.asyncio
async def test_unavailable_activation_cannot_read_with_a_changed_selected_key(pending_activation):
    activation, _, operation, _, _, _, reads, _, deliveries = pending_activation
    selected = json.loads(operation["owner_scope_json"])
    selected["key"]["installation_key_id"] = "different"
    operation["owner_scope_json"] = json.dumps(selected)
    with pytest.raises(HostedGrantUnavailable, match="Activation key changed"):
        await activation.execute(operation, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
    assert not reads and not deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", ["committed", "accepted-decision"])
async def test_accepted_activation_delivery_recovers_after_presentation_expiry(pending_activation, accepted):
    activation, owner, operation, source, row, state, _, _, deliveries = pending_activation
    operation["expires_at"] = owner.clock[0].timestamp()
    if accepted == "committed":
        row["domain_status"] = "committed"
    else:
        state["decision"] = "accepted"
    result = await activation.execute(operation, authorize=lambda: pytest.fail("No new command authority"), can_prepare=False)
    assert result.state == "waiting"
    assert RuntimeSetupOwner.automatic_recovery(SimpleNamespace(activation=activation), operation)
    assert len(deliveries) == 1 and deliveries[0]["activation_operation_id"] == source["activation_operation_id"]
    assert deliveries[0]["binding"] == activation._delivery_binding(operation)
    assert activation._source(operation["operation_id"]) == source
