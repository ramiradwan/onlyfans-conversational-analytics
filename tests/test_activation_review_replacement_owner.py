"""Encrypted-store exact commercial review replacement reservation/adoption."""
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.persistence.activation_review_replacement import ActivationReviewReplacements
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.source_preparations import SourcePreparations
from app.persistence.source_replacement_reservations import SourceReplacementReservations
from app.persistence import sqlite_api as sqlite3
from app.security.activation_review_replacement import prepare_request
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_continuation import canonical_request
from app.security.source_replacement import SourceReplacementRefused
from test_activation_reconciliation_owner import original, recover

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


def owner(case, store=None, origin="https://fixture.invalid"):
    return ActivationReviewReplacements(store or case.store, origin=origin)


def actor_scope(case):
    return json.loads(case.operation["owner_scope_json"])


def allow(**_):
    pass


def reserve(case, journal, connection, scope, *, authorize=allow, successor=None):
    if successor is None:
        successor = getattr(case, "successor_desktop", None) or str(uuid4())
    case.successor_desktop = successor
    return journal.reserve(connection, case.operation, scope, authorize=authorize,
        successor_desktop_operation_id=successor)


def admit(case, journal, connection, scope, reservation):
    journal.retire_original(connection, case.operation, scope, reservation, authorize=allow)
    old = case.operation
    selected = {**scope, "activation_replacement": journal.successor_intent(reservation)}
    now = case.clock[0].timestamp()
    connection.execute("INSERT INTO desktop_setup_operations VALUES (?,?,?,?,?,?,?,?,'accepted','none',?,?)",
        (reservation["successor_desktop_operation_id"], old["journey_id"], "activation.review", old["request_digest"],
         old["generation"], old["scope_digest"], old["source_revision"], canonical_request(selected).decode(), now, now+1800))
    return selected


async def terminal(case):
    case.state["status"] = "expired-uncommitted"
    assert (await recover(case, case.owner(case.store))).state == "outcome_unknown"


def prepared(case, request):
    successor = prepare_request(request, successor=True)
    return {"profile": request["profile"], "source_operation_id": request["source_operation_id"],
        "activation_operation_id": request["activation_operation_id"], "status": "prepared",
        "successor": {"profile": successor["profile"], "operation_id": successor["operation_id"],
            "reference": "A"*43, "expires_at": (case.clock[0]+timedelta(minutes=20)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "continuation_id": "0198a1b2-c3d4-7300-8000-000000000099"},
        "successor_prepare_digest": hashlib.sha256(canonical_request(successor)).hexdigest(),
        "successor_activation_operation_id": request["successor_activation_operation_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["committed","unresolved","refused"])
async def test_review_replacement_requires_definitive_noncommit(original, status):
    case = original
    case.state["status"] = status
    await recover(case, case.owner(case.store))
    journal = owner(case)
    with case.store.database.transaction() as connection:
        with pytest.raises(HostedGrantUnavailable):
            reserve(case, journal, connection, actor_scope(case))
        assert journal.row(connection, case.operation["operation_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["actor","origin","revoked","delivery","missing-evidence","guard"])
async def test_review_replacement_requires_exact_current_actor_and_authority(original, fault):
    case = original
    await terminal(case)
    scope = actor_scope(case)
    journal = owner(case)
    guard = allow
    with case.store.database.transaction() as connection:
        if fault == "actor":
            scope["actor"] = {**scope["actor"], "principal_id": "another"}
        elif fault == "origin":
            journal = owner(case, origin="https://other.invalid")
        elif fault == "revoked":
            connection.execute("UPDATE authorized_account_bindings SET revoked_at=?", (case.clock[0].isoformat(),))
        elif fault == "delivery":
            connection.execute("UPDATE capability_license_activation_deliveries SET activation_committed=1")
        elif fault == "missing-evidence":
            connection.execute("DELETE FROM onboarding_source_preparations")
        else:
            def guard(**_):
                raise HostedGrantUnavailable("Changed")
        with pytest.raises((HostedGrantUnavailable, ValueError)):
            reserve(case, journal, connection, scope, authorize=guard)
        assert journal.row(connection, case.operation["operation_id"]) is None


@pytest.mark.asyncio
async def test_review_replacement_exact_reservation_and_one_retry_survive_restart(original):
    case = original
    await terminal(case)
    journal = owner(case)
    scope = actor_scope(case)
    with case.store.database.transaction() as connection:
        proposal = journal.describe(connection, case.operation, scope, authorize=allow)
        assert proposal["activation_operation_id"] == case.activation_id
        assert journal.row(connection, case.operation["operation_id"]) is None
        reservation = reserve(case, journal, connection, scope)
        assert reserve(case, journal, connection, scope) == reservation
        with pytest.raises(HostedGrantUnavailable):
            journal.claim(connection, case.operation, scope, authorize=allow, reconcile=True)
        admit(case, journal, connection, scope, reservation)
        request, attempt = journal.claim(connection, case.operation, scope, authorize=allow)
        assert attempt == 1
        assert connection.execute("SELECT count(*) FROM onboarding_source_preparations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM desktop_activation_sources").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM capability_license_activation_deliveries").fetchone()[0] == 1
        with pytest.raises(OnboardingJourneyUnavailable):
            SourceReplacementReservations(case.store).reserve(connection, operation_id=proposal["source_operation_id"],
                expected_digest=proposal["source_prepare_digest"], creator_account_id=scope["creator_account_id"], authorize=allow)
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    journal = owner(case, reopened)
    with reopened.database.transaction() as connection:
        assert journal.claim(connection, case.operation, scope, authorize=allow) is None
        assert journal.claim(connection, case.operation, scope, authorize=allow, reconcile=True) == (request, 2)
        assert journal.claim(connection, case.operation, scope, authorize=allow, reconcile=True) is None
        with pytest.raises(HostedGrantUnavailable):
            journal.guard(connection, case.operation, scope, request, 1, authorize=allow)
        assert journal.guard(connection, case.operation, scope, request, 2, authorize=allow)[0]["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none","mismatched","adoption","authority"])
async def test_review_replacement_adopts_only_exact_successor_atomically(original, fault):
    case = original
    await terminal(case)
    journal = owner(case)
    scope = actor_scope(case)
    with case.store.database.transaction() as connection:
        reservation = reserve(case, journal, connection, scope)
        admit(case, journal, connection, scope, reservation)
        request, attempt = journal.claim(connection, case.operation, scope, authorize=allow)
    result = prepared(case, request)
    if fault == "mismatched":
        result["successor_activation_operation_id"] = request["activation_operation_id"]
    def guard(*, connection):
        if fault == "adoption" and connection.execute("SELECT 1 FROM desktop_activation_sources WHERE operation_id=?",
                (reservation["successor_desktop_operation_id"],)).fetchone():
            raise HostedGrantUnavailable("Adoption failed")
    if fault == "authority":
        with case.store.database.transaction() as connection:
            connection.execute("UPDATE authorized_account_bindings SET revoked_at=?", (case.clock[0].isoformat(),))
    def record():
        with case.store.database.transaction() as connection:
            return journal.record_prepared(connection, case.operation, scope, request, attempt, result, authorize=guard)
    if fault != "none":
        with pytest.raises((HostedGrantUnavailable, ValueError)):
            record()
        with case.store.database.read() as connection:
            assert journal.row(connection, case.operation["operation_id"])["status"] == "send-claimed"
            assert connection.execute("SELECT count(*) FROM onboarding_source_preparations").fetchone()[0] == 1
        return
    assert record()["status"] == "prepared"
    with case.store.database.transaction() as connection:
        row = InstallationContinuationStore(case.store).require_current(reservation["successor_journey_id"], connection=connection)
        assert row["state"] == "waiting" and row["scope"] == request["scope"]
        source_mapping = connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?",
            (reservation["successor_desktop_operation_id"],)).fetchone()
        assert source_mapping["source_journey_id"] == reservation["successor_journey_id"]
        assert source_mapping["activation_operation_id"] == reservation["successor_activation_operation_id"]
        source = SourcePreparations(case.store).get(request["source_operation_id"], connection=connection)
        successor = SourcePreparations(case.store).get(request["successor_source_operation_id"], connection=connection)
        assert successor["retain_until"] == source["retain_until"] and successor["created_at"] == source["created_at"]
        assert json.loads(successor["prepare_json"]) == prepare_request(request, successor=True)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE onboarding_journeys SET state='completed' WHERE operation_id=?", (request["source_operation_id"],))
        assert connection.execute("SELECT count(*) FROM capability_license_activation_deliveries").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["original_committed","review_unavailable","operation_conflict"])
async def test_review_replacement_refusal_never_adopts_or_retries(original, reason):
    case = original
    await terminal(case)
    journal = owner(case)
    scope = actor_scope(case)
    with case.store.database.transaction() as connection:
        reservation = reserve(case, journal, connection, scope)
        admit(case, journal, connection, scope, reservation)
        request, attempt = journal.claim(connection, case.operation, scope, authorize=allow)
        journal.record_refusal(connection, case.operation, scope, request, attempt, SourceReplacementRefused(reason), authorize=allow)
        assert journal.row(connection, case.operation["operation_id"])["refusal_reason"] == reason
        assert journal.claim(connection, case.operation, scope, authorize=allow, reconcile=True) is None
        assert connection.execute("SELECT count(*) FROM onboarding_source_preparations").fetchone()[0] == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [False, True])
async def test_desktop_review_replacement_explicit_command_prepares_exact_successor(original, monkeypatch, lost):
    from types import SimpleNamespace
    from app.onboarding.coordinator import DesktopOnboardingCoordinator, FACTS
    from app.onboarding.runtime import RuntimeSetupOwner, RuntimeActor
    from app.onboarding.readiness import DesktopAnalysisReadiness
    from app.security.runtime_policy import AuthContext
    from app.security.analysis_authorization import AnalysisReadiness
    from test_desktop_coordinator import command
    import app.onboarding.runtime as runtime_module

    case = original
    await terminal(case)
    activation = case.owner(case.store)
    scope = actor_scope(case)
    scope.update(source_journey_id=None)
    # Actual runtime authorization and admission; the fixture supplies the
    # local authority projection (its account binding is synthetic).
    runtime = RuntimeSetupOwner(case.store, browser_tasks=SimpleNamespace(read=lambda *_: None), activation=activation)
    identity = AuthContext(**scope["actor"])
    actor = RuntimeActor(lambda: case.store.build_runtime_policy(identity=identity))
    local = SimpleNamespace(**scope["actor"], grant_reference_ids=())
    readiness = AnalysisReadiness("required", "blocked")
    monkeypatch.setattr(runtime, "_local_authority", lambda policy, **_: local if policy.identity is not None else None)
    monkeypatch.setattr(case.store, "build_runtime_policy_from_grants",
        lambda *_: SimpleNamespace(identity_authority=SimpleNamespace(organization_id=scope["organization_id"])))
    monkeypatch.setattr(runtime_module, "current_analysis_readiness", lambda *_: readiness)
    monkeypatch.setattr(runtime_module, "local_setup_facts", lambda *_, **__: dict.fromkeys(FACTS, "verified") | {"activation": "missing"})
    monkeypatch.setattr(runtime_module, "creator_readiness", lambda policy, identity, value:
        DesktopAnalysisReadiness("current", identity.creator_account_id, value) if identity is not None else DesktopAnalysisReadiness())
    activation.ceremonies.authentication = None
    activation.ceremonies.decisions = None
    sent, prepared_ceremonies, delivery_ids = [], [], []
    class Client:
        def replace(self, request, *, before_send):
            before_send()
            sent.append(request)
            if lost and len(sent) == 1:
                raise HostedGrantUnavailable("lost")
            return prepared(case, request)
    activation.replacement_client = Client()
    coordinator = DesktopOnboardingCoordinator(case.admissions, runtime)
    # Renew presentation while retaining exact desktop workspace.
    native = case.admissions.consume(case.admissions.prepare_desktop(), role="desktop")
    with case.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_presentations SET journey_id=? WHERE generation=?",
            (case.operation["journey_id"], native.generation))
    before = coordinator.read(native, actor=actor)
    assert "activation.review" in before["allowed_actions"]
    assert not sent
    accepted, _ = coordinator._accept(native, command(before, "activation.review"), actor=actor)
    assert accepted["operation_id"] != case.operation["operation_id"]
    assert not sent
    with case.store.database.read() as connection:
        old = connection.execute("SELECT state,reason FROM desktop_setup_operations WHERE operation_id=?",
            (case.operation["operation_id"],)).fetchone()
        assert tuple(old) == ("refused", "operation_expired")
    assert "activation.review" not in coordinator.read(native, actor=actor)["allowed_actions"]
    assert (await runtime.execute(accepted, reconcile=True)).state == "outcome_unknown"
    assert not sent
    async def registered_prepare(journey_id, *, authorize):
        authorize()
        assert activation.contexts.require_current(journey_id)["reference"] == "A"*43
    activation.registered = SimpleNamespace(prepare=registered_prepare,
        _request=lambda row: {"profile": "urn:bridge-clean:installation-continuation:v2", "operation_id": row["operation_id"], "scope": row["scope"]})
    async def ceremony_prepare(operation_id, binding, *, authorize):
        authorize()
        prepared_ceremonies.append((operation_id, binding))
        return {"status": "found", "snapshot": {"domain_status": "pending"}}
    async def refresh(*args, **kwargs):
        return {"state": "confirmation-required"}
    activation.ceremonies.prepare = ceremony_prepare
    activation.ceremonies.authentication = None
    activation.ceremonies.decisions = SimpleNamespace(refresh=refresh)
    activation.ceremonies.decisions.presentation = lambda *_, **__: None
    activation.ceremonies.decisions.status = lambda *_, **__: None
    activation.browser_tasks = SimpleNamespace(extension_id=None, complete=lambda *_: None, prepare=case.forbid)
    activation.redemption.prepare_activation = lambda **kw: delivery_ids.append(kw["activation_operation_id"]) or True
    result = await runtime.execute(accepted, reconcile=False, actor=actor)
    assert result.state == "waiting"
    assert len(sent) == (2 if lost else 1)
    assert all(request == sent[0] for request in sent)
    assert delivery_ids == [sent[0]["successor_activation_operation_id"]]
    assert prepared_ceremonies[0][0] == accepted["operation_id"]
    assert prepared_ceremonies[0][1]["source"]["operation_id"] == sent[0]["successor_source_operation_id"]
    assert prepared_ceremonies[0][1]["activation_operation_id"] == delivery_ids[0]
    assert not case.deliveries
