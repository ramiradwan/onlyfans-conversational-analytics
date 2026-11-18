"""Original activation read and delivery delegation across encrypted-store restarts."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.activation import DesktopActivation
from app.onboarding.admission import ScopedAdmissions
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.persistence.auth import ClaimSubmission, InstallationKeyReservation, ProvisioningCandidate, ProvisioningCandidateState, SQLiteAuthenticationStore
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.source_preparations import SourcePreparations
from app.security.activation_reconciliation import ActivationReconciliationRefused
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_continuation import canonical_request
from test_desktop_coordinator import Owner, command
from test_source_replacement_client import ROOT, NOW, REGISTERED, Key

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.fixture
def original(tmp_path):
    vector = json.loads((ROOT / "complimentary-activation-reconciliation-v1/cases.json").read_text("utf-8"))["proof_cases"][0]
    key = Key(REGISTERED, vector)
    scope = vector["request"]["scope"]
    clock = [NOW]
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    ref = key.reference
    store.reserve_installation_key(InstallationKeyReservation(ref.provider_name, ref.provider_key_name, ref.algorithm, ref.created_at))
    store.activate_installation_key(ref)
    store.record_claim_submission(ClaimSubmission("claim.test", scope["source_onboarding_transaction_id"],
        scope["organization_id"], scope["installation_id"], NOW))
    store.resolve_claim_submission("claim.test", outcome=None, resolved_at=NOW)
    store.record_provisioning_candidate(ProvisioningCandidate(
        association_request_id=scope["association_request_id"], installation_id=scope["installation_id"],
        onboarding_transaction_id=scope["source_onboarding_transaction_id"], organization_id=scope["organization_id"],
        creator_account_id=scope["creator_account_id"], state=ProvisioningCandidateState.PENDING, requested_at=NOW))
    store.approve_provisioning_candidate(scope["association_request_id"], resolved_at=NOW)
    contexts = InstallationContinuationStore(store)
    with store.database.transaction() as connection:
        journey = contexts.admit_desktop_context(connection, journey_id=None, reopen_key=key.reopen_existing)
    original_source = contexts.begin_prepare(journey)
    deadline = (NOW + timedelta(minutes=10)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    contexts.bind_result(journey, continuation_id=vector["request"]["source_continuation_id"],
        reference="A"*43, expires_at=deadline)
    with store.database.transaction() as connection:
        connection.execute("INSERT INTO authorized_account_bindings VALUES (?,?,?,?,?,?,?)",
            (scope["creator_account_id"], scope["installation_id"], scope["creator_account_id"],
             scope["association_request_id"], "a"*64, NOW.isoformat(), None))
    admissions = ScopedAdmissions(store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    projection = Owner()
    projection.facts = dict.fromkeys(projection.facts, "verified")
    projection.facts["activation"] = "missing"
    projection.scope = {**scope, "key": {"installation_key_id": ref.installation_key_id,
        "installation_key_jkt": ref.installation_key_jkt}}
    coordinator = DesktopOnboardingCoordinator(admissions, projection)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    activation_id = vector["request"]["activation_operation_id"]
    selected = json.loads(operation["owner_scope_json"])
    delivery = {name: selected[name] for name in ("organization_id", "installation_id", "creator_account_id")} | selected["key"]
    with store.database.transaction() as connection:
        evidence = SourcePreparations(store).get(original_source["operation_id"], connection=connection)
        binding = {"purpose": "activate-complimentary", "activation_operation_id": activation_id,
            "source": {"kind": "registered", "profile": json.loads(evidence["prepare_json"])["profile"],
                "operation_id": evidence["operation_id"], "request_digest": evidence["prepare_digest"],
                "reference": "A"*43, "continuation_id": vector["request"]["source_continuation_id"]},
            "intended_creator_id": scope["creator_account_id"], "source_expires_at": deadline,
            **delivery}
        binding.pop("creator_account_id")
        connection.execute("INSERT INTO desktop_activation_sources VALUES (?,?,?)",
            (operation["operation_id"], journey, activation_id))
        connection.execute("INSERT INTO desktop_browser_ceremonies "
            "(operation_id,hosted_operation_id,binding_json,domain_status) VALUES (?,?,?,'pending')",
            (operation["operation_id"], "0198a1b2-c3d4-7300-8000-000000000088", json.dumps(binding)))
        connection.execute("INSERT INTO capability_license_activation_deliveries "
            "(activation_operation_id,provider_origin,binding_json) VALUES (?,?,?)",
            (activation_id, "https://fixture.invalid/", canonical_request(delivery).decode()))
    clock[0] += timedelta(minutes=11)
    calls, deliveries = [], []
    state = {"status": "committed"}
    def forbid(*args, **kwargs):
        pytest.fail("Original outcome recovery cannot prepare, confirm or open a browser")
    def redeem(**kwargs):
        deliveries.append(kwargs)
        return CapabilityLicenseDeliveryReceipt("reference", "license", "issuance")
    class Client:
        def read(self, request, *, before_send):
            before_send()
            calls.append(request)
            if state["status"] == "transport":
                raise HostedGrantUnavailable("lost")
            if state["status"] == "refused":
                raise ActivationReconciliationRefused("authorization_denied")
            return {**{name: request[name] for name in ("profile", "source_operation_id", "activation_operation_id")},
                "status": state["status"], **({"activation_operation_id": "0198a1b2-c3d4-7300-8000-000000000099"} if state.get("mismatch") else {})}
    def owner(current):
        def row(identity):
            with current.database.read() as connection:
                result = connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (identity,)).fetchone()
                return None if result is None else dict(result)
        return DesktopActivation(current, key=SimpleNamespace(reopen_existing=current.installation_key_reference),
            registered=SimpleNamespace(prepare=forbid), ceremonies=SimpleNamespace(row=row, reconcile=forbid, prepare=forbid),
            browser_tasks=SimpleNamespace(prepare=forbid), redemption=SimpleNamespace(prepare_activation=forbid, resume_activation=redeem),
            reconciliation_client=Client(), reconciliation_origin=state.get("origin", "https://fixture.invalid/"))
    return SimpleNamespace(store=store, owner=owner, operation=operation, path=path, clock=clock, state=state,
        calls=calls, deliveries=deliveries, source=original_source, journey=journey, activation_id=activation_id,
        delivery=delivery, forbid=forbid)


async def recover(case, activation):
    return await activation.execute(case.operation, authorize=case.forbid, can_prepare=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["committed", "expired-uncommitted", "refused"])
async def test_terminal_original_outcome_never_creates_a_new_activation(original, status):
    case = original
    case.state["status"] = status
    activation = case.owner(case.store)
    result = await recover(case, activation)
    assert result.state == ("waiting" if status == "committed" else "outcome_unknown")
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    activation = case.owner(reopened)
    await recover(case, activation)
    assert len(case.calls) == 1
    assert bool(case.deliveries) == (status == "committed")
    for delivery in case.deliveries:
        assert delivery == {"activation_operation_id": case.activation_id, "binding": case.delivery, "initial_redemption": None}
    assert activation.outcome_is_known(case.operation["operation_id"]) == (status == "committed")
    assert activation.automatic_recovery(case.operation) == (status == "committed")
    assert activation.reconciliation_pending(case.operation["journey_id"])
    with reopened.database.read() as connection:
        row = activation.reconciliation.row(connection, case.operation["operation_id"])
        assert row["status"] == status and row["attempts"] == 1
        assert connection.execute("SELECT count(*) FROM desktop_activation_sources").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["transport", "unresolved"])
async def test_read_budget_survives_restart_and_exhaustion_is_passive(original, status):
    case = original
    case.state["status"] = status
    activation = case.owner(case.store)
    assert (await recover(case, activation)).state == "outcome_unknown"
    assert (await recover(case, activation)).state == "outcome_unknown"
    assert len(case.calls) == 1
    for _ in range(2):
        case.clock[0] += timedelta(minutes=1)
        activation = case.owner(SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0]))
        assert (await recover(case, activation)).state == "outcome_unknown"
    case.clock[0] += timedelta(minutes=2)
    assert (await recover(case, activation)).state == "outcome_unknown"
    assert len(case.calls) == 3 and all(request == case.calls[0] for request in case.calls)
    assert not case.deliveries and not activation.automatic_recovery(case.operation)
    with case.store.database.read() as connection:
        row = activation.reconciliation.row(connection, case.operation["operation_id"])
        assert row["status"] is None and row["attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "missing-evidence", "wrong-origin", "wrong-result"])
async def test_missing_or_changed_original_never_redeems(original, change):
    case = original
    with case.store.database.transaction() as connection:
        if change == "revoked":
            connection.execute("UPDATE authorized_account_bindings SET revoked_at=?", (case.clock[0].isoformat(),))
        elif change == "missing-evidence":
            connection.execute("DELETE FROM onboarding_source_preparations")
        elif change == "wrong-origin":
            case.state["origin"] = "https://other.invalid/"
        else:
            case.state["mismatch"] = True
    activation = case.owner(case.store)
    result = await recover(case, activation)
    assert result.state == "outcome_unknown" and not case.deliveries
    assert len(case.calls) == (1 if change == "wrong-result" else 0)
    with case.store.database.read() as connection:
        row = activation.reconciliation.row(connection, case.operation["operation_id"])
        assert row is None or row["status"] is None

@pytest.mark.asyncio
async def test_registered_source_refusal_delegates_only_exact_existing_activation(original):
    case = original
    activation = case.owner(case.store)
    with case.store.database.read() as connection:
        request, _ = activation.reconciliation.request(connection, case.operation)
    proposal = {"source_journey_id": case.journey, "creator_account_id": request["scope"]["creator_account_id"],
        "association_request_id": request["scope"]["association_request_id"],
        "replacement": {"operation_id": request["source_operation_id"],
            "expected_digest": request["source_prepare_digest"], "installation_id": request["scope"]["installation_id"]}}
    await activation.reconcile_registered_source({**proposal, "creator_account_id": "another"})
    await activation.reconcile_registered_source({**proposal, "replacement": {**proposal["replacement"], "expected_digest": "0"*64}})
    assert not case.calls and not case.deliveries
    await activation.reconcile_registered_source(proposal)
    assert case.calls == [request] and len(case.deliveries) == 1


@pytest.mark.asyncio
async def test_existing_delivery_recovery_takes_priority_without_source_evidence(original):
    case = original
    with case.store.database.transaction() as connection:
        connection.execute("UPDATE capability_license_activation_deliveries SET activation_committed=1")
        connection.execute("DELETE FROM onboarding_source_preparations")
    activation = case.owner(case.store)
    assert (await recover(case, activation)).state == "waiting"
    assert not case.calls and len(case.deliveries) == 1
    assert activation.automatic_recovery(case.operation)

    assert activation.outcome_is_known(case.operation["operation_id"])
