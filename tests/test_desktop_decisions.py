import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.decisions import DesktopDecisions
from app.onboarding.authentication import DesktopAuthentication
from app.onboarding.coordinator import CommandRefused
from app.security.browser_ceremony import binding_digest
from app.security.desktop_decision import PROFILE
from test_desktop_ceremonies import authorized, setup as setup
from test_desktop_coordinator import command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class DecisionClient:
    transport = SimpleNamespace(close=lambda: None)

    def __init__(self):
        self.calls = []
        self.state = "confirmation-required"
        self.lose_submit_reply = False

    def read(self, request, *, before_send, expires_at):
        before_send()
        self.calls.append(("read", copy.deepcopy(request)))
        result = {"profile": PROFILE, "operation_id": request["operation_id"],
            "ceremony_id": request["ceremony_id"], "binding_digest": binding_digest(request["binding"]),
            "state": self.state}
        if self.state == "confirmation-required":
            result.update(decision_reference="A" * 43, decision_digest="0" * 64,
                decision={"action": "connect-account", "expires_at": expires_at,
                    "creator_id": request["binding"]["intended_creator_id"], "computer_name": "Test computer"})
        return result

    def submit(self, request, *, before_send, expires_at):
        before_send()
        self.calls.append(("submit", copy.deepcopy(request)))
        self.state = "accepted"
        if self.lose_submit_reply:
            raise OSError("Unconfirmed decision")
        return self.read(request, before_send=before_send, expires_at=expires_at)


@pytest.fixture
async def decision(setup):
    coordinator, ceremonies, ceremony_client, parent, binding, clock = setup
    await ceremonies.prepare(parent["operation_id"], binding, authorize=authorized)
    client = DecisionClient()
    decisions = DesktopDecisions(ceremonies.store, ceremonies, client)
    ceremonies.decisions = decisions
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    await decisions.refresh(parent["operation_id"], authorize=authorized)
    return coordinator, ceremonies, decisions, client, parent, native, clock, ceremony_client


def accept(decision):
    coordinator, _, decisions, _, parent, native, _, _ = decision
    coordinator.owner.scope["desktop_decision"] = decisions.presentation(parent["journey_id"], "account.connect")
    operation, fresh = coordinator._accept(native, command(coordinator.read(native), "account.confirm"))
    assert fresh
    return operation


@pytest.mark.asyncio
async def test_confirmation_keeps_a_wait_without_an_open_browser_task(decision):
    from app.security.initial_handoff import timestamp
    _, ceremonies, decisions, _, parent, _, clock, _ = decision
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, SimpleNamespace())
    assert authentication.probe_interval(parent["operation_id"]) is not None
    assert authentication.confirmation_deadline(parent["operation_id"]) == min(
        parent["expires_at"], timestamp(decisions.presentation(parent["journey_id"], "account.connect")
            ["projection"]["decision"]["expires_at"]).timestamp())
    clock[0] += timedelta(hours=1)
    assert authentication.confirmation_deadline(parent["operation_id"]) is None
    assert authentication.probe_interval(parent["operation_id"]) is None


@pytest.mark.asyncio
async def test_reads_and_restart_recovery_never_submit_unconfirmed_consent(decision):
    _, _, decisions, client, parent, _, _, _ = decision
    operation = accept(decision)
    for _ in range(3):
        await decisions.refresh(parent["operation_id"], authorize=authorized)
    result = await decisions.execute(operation, authorize=lambda: pytest.fail("No repeated consent"),
        read_guard=authorized, reconcile=True)
    assert result.state == "refused" and result.reason == "operation_unconfirmed"
    assert all(verb == "read" for verb, _ in client.calls)
    assert decisions.presentation(parent["journey_id"], "account.connect") is not None


@pytest.mark.asyncio
async def test_lost_accepted_reply_recovers_original_operation_without_resubmission(decision):
    _, ceremonies, decisions, client, parent, _, _, _ = decision
    operation = accept(decision)
    selected = json.loads(operation["owner_scope_json"])["desktop_decision"]
    client.lose_submit_reply = True
    with pytest.raises(OSError):
        await decisions.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    reopened = DesktopDecisions(ceremonies.store, ceremonies, client)
    result = await reopened.execute(operation, authorize=lambda: pytest.fail("No repeated consent"),
        read_guard=authorized, reconcile=True)
    assert result.state == "completed"
    assert reopened.status(parent["journey_id"], "account.connect")["state"] == "accepted"
    assert reopened.presentation(parent["journey_id"], "account.connect") is None
    submitted = [request for verb, request in client.calls if verb == "submit"]
    assert len(submitted) == 1
    assert submitted[0]["decision_reference"] == selected["projection"]["decision_reference"]
    assert submitted[0]["decision_digest"] == selected["projection"]["decision_digest"]
    assert {request["operation_id"] for _, request in client.calls} == {
        ceremonies.row(parent["operation_id"])["hosted_operation_id"]}


@pytest.mark.asyncio
async def test_changed_ceremony_revision_fences_stale_display_and_acceptance(decision):
    _, ceremonies, decisions, client, parent, _, _, ceremony_client = decision
    old_row, old_request = decisions._request(parent["operation_id"])
    old_view = client.read(old_request, before_send=authorized, expires_at=old_row["provider_expires_at"])
    operation = accept(decision)
    assert (await decisions.execute(operation, authorize=authorized, read_guard=authorized,
        reconcile=False)).state == "completed"
    decisions._save(old_row, old_view)
    assert decisions.status(parent["journey_id"], "account.connect")["state"] == "accepted"
    ceremony_client.revision = 2
    await ceremonies.reconcile(parent["operation_id"], authorize=authorized)
    client.state = "confirmation-required"
    await decisions.refresh(parent["operation_id"], authorize=authorized)
    fresh = decisions.presentation(parent["journey_id"], "account.connect")
    assert fresh is not None
    decisions._save(old_row, {**old_view, "state": "accepted"})
    assert decisions.presentation(parent["journey_id"], "account.connect") == fresh


@pytest.mark.asyncio
async def test_expired_display_cannot_submit_but_retains_original_recovery(decision):
    _, _, decisions, client, parent, _, clock, _ = decision
    operation = accept(decision)
    clock[0] += timedelta(hours=1)
    assert decisions.presentation(parent["journey_id"], "account.connect") is None
    assert decisions.status(parent["journey_id"], "account.connect")["state"] == "unavailable"
    result = await decisions.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    assert result.state == "refused" and result.reason == "operation_expired"
    assert not any(verb == "submit" for verb, _ in client.calls)
    client.state = "accepted"
    recovered = await decisions.execute(operation, authorize=lambda: pytest.fail("No repeated consent"),
        read_guard=authorized, reconcile=True)
    assert recovered.state == "completed"


def authentication_projection(row, state="admitted", *, open_browser=False):
    return {"profile": "urn:bridge-clean:browser-authentication-selection:v1",
        "operation_id": row["hosted_operation_id"], "ceremony_id": row["ceremony_id"],
        "binding_digest": binding_digest(json.loads(row["binding_json"])),
        "state": state, "expires_at": row["provider_expires_at"],
        "choices": dict.fromkeys(("google", "microsoft-personal", "telegram", "tiktok", "default"), True),
        "actions": {"select": False, "cancel": False, "open_browser": open_browser, "switch_account": False},
        "authentication_attempt": {"authentication_attempt_id": "55555555-5555-7555-8555-555555555555",
            "provider": "google", "mode": "sign-in"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("state,open_browser", [("selection-required", False), ("reauthentication-required", False),
    ("selected", True), ("expired", False), ("unavailable", False), ("admitted", True)])
async def test_unusable_or_recovering_authentication_hides_and_refuses_cached_consent(decision, state, open_browser):
    coordinator, ceremonies, decisions, _, parent, native, _, _ = decision
    row = ceremonies.row(parent["operation_id"])
    coordinator.owner.scope["desktop_decision"] = decisions.presentation(parent["journey_id"], "account.connect")
    coordinator.owner.scope["desktop_authentication"] = {"operation_id": parent["operation_id"],
        "browser_operation_id": None, **{key: value for key, value in authentication_projection(row, state,
            open_browser=open_browser).items() if key in {"state", "choices", "actions", "authentication_attempt"}}}
    snapshot = coordinator.read(native)
    assert snapshot["decision"] is None and "account.confirm" not in snapshot["allowed_actions"]
    with pytest.raises(CommandRefused) as refused:
        coordinator._accept(native, command(snapshot, "account.confirm"))
    assert refused.value.reason == "action_unavailable"


@pytest.mark.asyncio
async def test_authentication_revision_fences_late_display_even_after_same_projection_returns(decision):
    _, ceremonies, decisions, client, parent, _, _, _ = decision
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client)
    row = ceremonies.row(parent["operation_id"])
    admitted = authentication_projection(row)
    authentication._save(row, admitted)
    old_row, request = decisions._request(parent["operation_id"])
    delayed = client.read(request, before_send=authorized, expires_at=old_row["provider_expires_at"])
    authentication._save(row, authentication_projection(row, "selected", open_browser=True))
    authentication._save(row, admitted)
    decisions._save(old_row, delayed)
    authentication._save(row, admitted)
    assert decisions.presentation(parent["journey_id"], "account.connect") is None
    await decisions.refresh(parent["operation_id"], authorize=authorized)
    assert decisions.presentation(parent["journey_id"], "account.connect") is not None
    accepted = {key: value for key, value in delayed.items() if key not in {"decision", "decision_reference", "decision_digest"}}
    decisions._save(old_row, {**accepted, "state": "accepted"})
    authentication._save(row, authentication_projection(row, "retired"))
    assert decisions.status(parent["journey_id"], "account.connect")["state"] == "accepted"


@pytest.mark.asyncio
async def test_retired_switch_needs_fresh_decision_but_does_not_erase_live_customer_admission(decision):
    coordinator, ceremonies, decisions, client, parent, native, _, _ = decision
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client)
    row = ceremonies.row(parent["operation_id"])
    authentication._save(row, authentication_projection(row, "retired"))
    assert decisions.presentation(parent["journey_id"], "account.connect") is None
    await decisions.refresh(parent["operation_id"], authorize=authorized)
    coordinator.owner.scope["desktop_decision"] = decisions.presentation(parent["journey_id"], "account.connect")
    coordinator.owner.scope["desktop_authentication"] = authentication.presentation(parent["journey_id"], "account.connect")
    snapshot = coordinator.read(native)
    assert snapshot["decision"] is not None and "account.confirm" in snapshot["allowed_actions"]


@pytest.mark.asyncio
async def test_held_challenges_are_spent_by_the_next_ceremony_and_decision_reads(decision):
    from app.security.hosted_grants import HostedGrantUnavailable
    _, ceremonies, decisions, client, parent, _, _, ceremony_client = decision
    held, spent = [], []

    def prefetch(kind):
        def prefetch_read(request, *, before_send):
            before_send()
            held.append((kind, copy.deepcopy(request)))
            return SimpleNamespace(kind=kind)
        return prefetch_read

    def spending(kind, read):
        def spend(request, *, prefetched=None, **options):
            spent.append((kind, getattr(prefetched, "kind", None)))
            return read(request, **options)
        return spend

    ceremony_client.prefetch_read, client.prefetch_read = prefetch("ceremony"), prefetch("decision")
    ceremony_client.read, client.read = spending("ceremony", ceremony_client.read), spending("decision", client.read)
    guards = []
    operation_id = parent["operation_id"]
    assert ceremonies.hold_read(operation_id, authorize=lambda: guards.append(True))
    assert decisions.hold_read(operation_id, authorize=lambda: guards.append(True))
    assert not ceremonies.hold_read(operation_id, authorize=lambda: guards.append(True))
    await ceremonies.reconcile(operation_id, authorize=authorized)
    await decisions.refresh(operation_id, authorize=authorized)
    await ceremonies.reconcile(operation_id, authorize=authorized)
    assert sorted(kind for kind, _ in held) == ["ceremony", "decision"] and len(guards) == 2
    row = ceremonies.row(operation_id)
    assert dict(held)["ceremony"] == ceremonies.request(row)
    assert dict(held)["decision"]["ceremony_id"] == row["ceremony_id"]
    assert spent == [("ceremony", "ceremony"), ("decision", "decision"), ("ceremony", None)]

    def failing(request, *, before_send):
        raise HostedGrantUnavailable("Challenge unavailable")

    ceremony_client.prefetch_read = failing
    assert ceremonies.hold_read(operation_id, authorize=authorized)
    await ceremonies.reconcile(operation_id, authorize=authorized)
    assert spent[-1] == ("ceremony", None)


@pytest.mark.asyncio
async def test_a_hint_holds_its_parent_read_challenges_before_its_own_read(decision):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, _, client, parent, _, _, ceremony_client = decision
    order = []
    ceremony_client.prefetch_read = lambda request, *, before_send: order.append("ceremony")
    client.prefetch_read = lambda request, *, before_send: order.append("decision")

    async def reconcile_task(operation_id, *, authorize):
        await asyncio.sleep(0.05)
        order.append("hinted")
        return True

    ceremonies.authentication = SimpleNamespace(parent_operation=lambda operation_id, **_: parent["operation_id"],
                                                reconcile_task=reconcile_task)
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store, owner.ceremonies = coordinator.store, ceremonies
    owner._guard = lambda *_, **__: None
    owner._read_guard = lambda *_: None
    assert await owner.reconcile_hint({"operation_id": "child"}) is True
    assert sorted(order[:2]) == ["ceremony", "decision"] and order[2] == "hinted"
