"""Exact source evidence and one irreversible send claim, in real SQLCipher."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.persistence.auth import (InstallationKeyReservation, SQLiteAuthenticationStore, ClaimSubmission,
    ProvisioningCandidate, ProvisioningCandidateState)
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.persistence.source_preparations import SourcePreparations, RETENTION
from app.persistence.source_replacement_reservations import SourceReplacementReservations
from app.security.source_replacement import INITIAL, REGISTERED, original_prepare, SourceReplacementRefused
from app.security.installation_continuation import canonical_request
from app.persistence.installation_continuation import InstallationContinuationStore, _generation, _scope, _json
from app.persistence import sqlite_api as sqlite3
import hashlib
from test_source_replacement_client import fixture, NOW


pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.fixture
def retained(tmp_path, fixture):
    kind, _, _, key, vector, _ = fixture
    clock = [NOW]
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    reference = key.reference
    store.reserve_installation_key(InstallationKeyReservation(reference.provider_name, reference.provider_key_name,
        reference.algorithm, reference.created_at))
    store.activate_installation_key(reference)
    request = original_prepare(kind, vector["request"])
    destination = request["destination"] if kind == INITIAL else request["scope"]
    source_kind = "initial-enrollment" if kind == INITIAL else "registered-continuation"
    identity = str(uuid4())
    journey = {"journey_id": identity, "kind": source_kind, "installation_id": destination["installation_id"],
        "operation_id": request["operation_id"], "created_at": NOW.isoformat()}
    preparations = SourcePreparations(store)
    if kind == REGISTERED:
        store.record_claim_submission(ClaimSubmission("claim.test", destination["source_onboarding_transaction_id"],
            destination["organization_id"], destination["installation_id"], NOW))
        store.resolve_claim_submission("claim.test", outcome=None, resolved_at=NOW)
        store.record_provisioning_candidate(ProvisioningCandidate(
            association_request_id=destination["association_request_id"], installation_id=destination["installation_id"],
            onboarding_transaction_id=destination["source_onboarding_transaction_id"], organization_id=destination["organization_id"],
            creator_account_id=destination["creator_account_id"], state=ProvisioningCandidateState.PENDING, requested_at=NOW))
    with store.database.transaction() as connection:
        connection.execute("INSERT INTO onboarding_journeys (journey_id,kind,installation_id,operation_id,"
            "created_at,expires_at,state) VALUES (?,?,?,?,?,?,'waiting')",
            (identity, source_kind, destination["installation_id"], request["operation_id"], NOW.isoformat(),
                (NOW + timedelta(minutes=30)).isoformat()))
        if kind == REGISTERED:
            target = InstallationContinuationStore(store).selection.select_in_transaction(connection)
            assert _scope(target) == destination
            connection.execute("INSERT INTO installation_continuation_contexts "
                "(journey_id,operation_profile,association_request_id,selection_json,scope_json,"
                "continuation_id,provider_expires_at) VALUES (?,?,?,?,?,?,?)",
                (identity, request["profile"], target.candidate.association_request_id, _generation(target),
                    _json(destination), vector["request"]["predecessor_continuation_id"], (NOW+timedelta(minutes=10)).isoformat()))
        saved = preparations.retain(connection, journey, request)
        preparations.record_prepared(connection, journey, expires_at=(NOW + timedelta(minutes=10)).isoformat(),
            continuation_id=vector["request"].get("predecessor_continuation_id"))
    clock[0] += timedelta(minutes=11)
    return store, path, clock, journey, request, saved, vector


def arguments(saved, *, guard=lambda **_: None, creator="creator.test"):
    return {"operation_id": saved["operation_id"], "expected_digest": saved["prepare_digest"],
        "creator_account_id": creator, "authorize": guard}


def test_reserve_and_claim_exact_once_across_restart(retained):
    store, path, clock, journey, request, saved, vector = retained
    with store.database.transaction() as connection:
        reservations = SourceReplacementReservations(store)
        first = reservations.reserve(connection, **arguments(saved))
        again = reservations.reserve(connection, **arguments(saved))
        assert again == first
        assert first["state"] == "reserved"
    reopened = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    with reopened.database.transaction() as connection:
        result = SourceReplacementReservations(reopened).claim_send(connection, **arguments(saved))
        assert result["successor_operation_id"] == first["successor_operation_id"]
        assert result["predecessor_prepare_digest"] == vector["request"]["predecessor_prepare_digest"]
        assert result["predecessor_operation_id"] == request["operation_id"]
        assert json.loads(first["request_json"]) == result
    with reopened.database.transaction() as connection:
        reservations = SourceReplacementReservations(reopened)
        assert reservations.reserve(connection, **arguments(saved))["state"] == "send-claimed"
        with pytest.raises(OnboardingJourneyUnavailable):
            reservations.claim_send(connection, **arguments(saved))
        assert connection.execute("SELECT count(*) FROM onboarding_source_replacement_reservations").fetchone()[0] == 1


def test_claim_guard_failure_rolls_back_claim(retained):
    store, _, _, _, _, saved, _ = retained
    reservations = SourceReplacementReservations(store)
    with store.database.transaction() as connection:
        reservations.reserve(connection, **arguments(saved))
    calls = []
    def guard(**_):
        calls.append(True)
        if len(calls) > 1:
            raise OnboardingJourneyUnavailable("journey_unavailable")
    with pytest.raises(OnboardingJourneyUnavailable):
        with store.database.transaction() as connection:
            reservations.claim_send(connection, **arguments(saved, guard=guard))
    with store.database.transaction() as connection:
        assert reservations.reserve(connection, **arguments(saved))["state"] == "reserved"


def test_live_or_unconfirmed_source_cannot_reserve(retained):
    store, _, clock, _, _, saved, _ = retained
    clock[0] = NOW + timedelta(minutes=9)
    with store.database.transaction() as connection:
        with pytest.raises(OnboardingJourneyUnavailable):
            SourceReplacementReservations(store).reserve(connection, **arguments(saved))
    clock[0] = NOW + timedelta(minutes=11)
    with store.database.transaction() as connection:
        connection.execute("DELETE FROM onboarding_source_prepared_receipts")
        with pytest.raises(OnboardingJourneyUnavailable):
            SourceReplacementReservations(store).reserve(connection, **arguments(saved))
        assert connection.execute("SELECT count(*) FROM onboarding_source_replacement_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("state", ["completed", "revoked"])
def test_terminal_original_takes_precedence(retained, state):
    store, _, _, journey, _, saved, _ = retained
    with store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET state=? WHERE journey_id=?", (state, journey["journey_id"]))
        with pytest.raises(OnboardingJourneyUnavailable):
            SourceReplacementReservations(store).reserve(connection, **arguments(saved))


def test_exact_request_retained_after_journey_cleanup_and_never_extended(retained):
    store, _, clock, journey, request, saved, _ = retained
    preparations = SourcePreparations(store)
    with store.database.transaction() as connection:
        assert preparations.classify(journey["journey_id"], connection=connection) == "retained-prepare"
        assert preparations.retain(connection, journey, request) == saved
    clock[0] = NOW + timedelta(minutes=31)
    with store.database.transaction() as connection:
        OnboardingJourneyStore(store)._cleanup(connection, clock[0])
        assert preparations.get(saved["operation_id"], connection=connection) == saved
        assert preparations.classify(journey["journey_id"], connection=connection) == "retained-prepare"
        assert connection.execute("SELECT 1 FROM onboarding_journeys WHERE journey_id=?", (journey["journey_id"],)).fetchone() is None
        SourceReplacementReservations(store).reserve(connection, **arguments(saved))
    clock[0] = NOW + RETENTION
    with store.database.transaction() as connection:
        preparations.cleanup(connection, clock[0])
        assert preparations.get(saved["operation_id"], connection=connection) is None
        assert preparations.classify(journey["journey_id"], connection=connection) == "indeterminate"
        assert connection.execute("SELECT count(*) FROM onboarding_source_replacement_reservations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM onboarding_source_prepared_receipts").fetchone()[0] == 0


def test_new_initial_is_distinct_from_missing_or_unknown_evidence(tmp_path):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    journey = OnboardingJourneyStore(store).open()["journey_id"]
    with store.database.transaction() as connection:
        source = SourcePreparations(store)
        assert source.classify(journey, connection=connection) == "never-prepared"
        assert source.classify(str(uuid4()), connection=connection) == "indeterminate"
        connection.execute("UPDATE onboarding_journeys SET state='prepare-unknown' WHERE journey_id=?", (journey,))
        assert source.classify(journey, connection=connection) == "indeterminate"


def claimed(retained):
    store, _, clock, journey, request, saved, vector = retained
    reservations = SourceReplacementReservations(store)
    with store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET state='expired' WHERE journey_id=?", (journey["journey_id"],))
        reservations.reserve(connection, **arguments(saved))
        replacement = reservations.claim_send(connection, **arguments(saved))
    return reservations, replacement


def prepared_result(replacement, clock):
    kind = INITIAL if "destination" in replacement else REGISTERED
    successor = original_prepare(kind, replacement, successor=True)
    response = {"profile": replacement["profile"], "predecessor_operation_id": replacement["predecessor_operation_id"],
        "status": "prepared", "successor_prepare_digest": hashlib.sha256(canonical_request(successor)).hexdigest(),
        "successor": {"profile": successor["profile"], "operation_id": successor["operation_id"],
            "reference": "A"*43, "expires_at": (clock[0] + timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00","Z")}}
    if kind == INITIAL:
        response["intended_creator_id"] = "creator.test"
    else:
        response["successor"]["continuation_id"] = "0198a1b2-c3d4-7300-8000-000000000099"
    return response


def test_prepared_successor_is_exact_and_inherits_root_retention(retained):
    store, _, clock, journey, request, saved, _ = retained
    reservations, replacement = claimed(retained)
    identity = str(uuid4())
    with store.database.transaction() as connection:
        result = reservations.record_result(connection, **arguments(saved), successor_journey_id=identity,
            result=prepared_result(replacement, clock))
        assert result["status"] == "prepared" and result["successor_journey_id"] == identity
        successor = OnboardingJourneyStore(store).require_current(identity, connection=connection)
        assert successor["state"] == "waiting" and successor["installation_id"] == journey["installation_id"]
        evidence = reservations.preparations.get(successor["operation_id"], connection=connection)
        assert evidence["created_at"] == saved["created_at"]
        assert evidence["retain_until"] == saved["retain_until"]
        assert evidence["journey_id"] == identity
        if journey["kind"] == "registered-continuation":
            current = InstallationContinuationStore(store).require_current(identity, connection=connection)
            assert current["scope"] == request["scope"]
            assert current["continuation_id"] == "0198a1b2-c3d4-7300-8000-000000000099"
        else:
            assert successor["scope_json"] is None
            assert json.loads(successor["prepare_json"])["destination"] == request["destination"]
        with pytest.raises(OnboardingJourneyUnavailable):
            OnboardingJourneyStore(store).require_current(journey["journey_id"], connection=connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE onboarding_journeys SET state='completed' WHERE journey_id=?", (journey["journey_id"],))
        with pytest.raises(OnboardingJourneyUnavailable):
            reservations.record_result(connection, **arguments(saved), successor_journey_id=str(uuid4()),
                result=prepared_result(replacement, clock))
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM onboarding_source_replacement_outcomes").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["digest", "scope", "guard", "revoked"])
def test_invalid_or_stale_replacement_cannot_adopt(retained, change):
    store, _, clock, journey, _, saved, _ = retained
    reservations, replacement = claimed(retained)
    result = prepared_result(replacement, clock)
    def guard(**_):
        if change == "guard":
            raise OnboardingJourneyUnavailable("journey_unavailable")
    if change == "digest":
        result["successor_prepare_digest"] = "0"*64
    elif change == "scope":
        if journey["kind"] == "initial-enrollment":
            result["intended_creator_id"] = "another"
        else:
            result["successor"]["continuation_id"] = replacement["predecessor_continuation_id"]
    elif change == "revoked":
        with store.database.transaction() as connection:
            connection.execute("UPDATE onboarding_journeys SET state='revoked' WHERE journey_id=?", (journey["journey_id"],))
    from app.security.hosted_grants import HostedGrantUnavailable
    with pytest.raises((OnboardingJourneyUnavailable, HostedGrantUnavailable)):
        with store.database.transaction() as connection:
            reservations.record_result(connection, **arguments(saved, guard=guard), successor_journey_id=str(uuid4()), result=result)
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 1
        assert reservations.outcome(connection, saved["operation_id"]) is None


def test_original_outcomes_never_materialize_successors(retained):
    store, _, clock, journey, _, saved, _ = retained
    reservations, replacement = claimed(retained)
    with store.database.transaction() as connection:
        if journey["kind"] == "initial-enrollment":
            original_id = "0198a1b2-c3d4-7300-8000-000000000003"
            result = {"profile": replacement["profile"], "predecessor_operation_id": replacement["predecessor_operation_id"],
                "status": "committed", "completion_operation_id": original_id}
            outcome = reservations.record_result(connection, **arguments(saved), successor_journey_id=str(uuid4()), result=result)
            assert outcome["status"] == "committed" and outcome["original_domain_operation_id"] == original_id
        else:
            outcome = reservations.record_commercial_refusal(connection, **arguments(saved),
                refusal=SourceReplacementRefused("commercial_reconciliation_required"))
            assert outcome["status"] == "commercial-reconciliation-required"
        assert outcome["successor_journey_id"] is None
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM onboarding_source_preparations").fetchone()[0] == 1

@pytest.mark.parametrize("loss", [False, True])
def test_owner_adapter_resumes_exact_prepared_source(retained, loss):
    from app.provisioning.setup_sources import SetupSourceLifecycle, PreparedSourceRecovery
    from app.security.hosted_grants import HostedGrantUnavailable
    store, path, clock, journey, _, saved, _ = retained
    calls, adopted = [], []
    class Client:
        def replace(self, request, *, before_send, intended_creator_id):
            before_send()
            calls.append(request)
            if loss and len(calls) == 1:
                raise HostedGrantUnavailable("lost")
            return prepared_result(request, clock)
    def adapter(owner):
        return PreparedSourceRecovery(SetupSourceLifecycle(owner, OnboardingJourneyStore(owner), InstallationContinuationStore(owner)), {journey["kind"]: Client()})
    with store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET state='expired' WHERE journey_id=?", (journey["journey_id"],))
        recovery = adapter(store)
        proposal = recovery.proposal(connection, journey["journey_id"], "creator.test")
        recovery.reserve(connection, proposal, authorize=lambda **_: None)
    identity = str(uuid4())
    def adopt(*, connection, successor_journey_id):
        assert successor_journey_id == identity
        assert OnboardingJourneyStore(store).require_current(identity, connection=connection)["state"] == "waiting"
        adopted.append(successor_journey_id)
    def execute(recovery, reconcile):
        return recovery.execute(proposal, successor_journey_id=identity, authorize=lambda **_: None,
            adopt=adopt, reconcile=reconcile)
    assert execute(recovery, True) is None
    outcome = execute(recovery, False)
    if loss:
        assert outcome is None and not adopted
        reopened = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
        recovery = adapter(reopened)
        outcome = execute(recovery, True)
    assert outcome["status"] == "prepared"
    assert adopted == [identity]
    assert execute(recovery, True) == outcome
    assert len(calls) == (2 if loss else 1) and all(call == calls[0] for call in calls)

def test_second_exact_reconciliation_is_not_admitted(retained):
    store, path, clock, _, _, saved, _ = retained
    reservations, replacement = claimed(retained)
    with store.database.transaction() as connection:
        assert reservations.claim_reconciliation(connection, **arguments(saved)) == replacement
    reopened = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    with reopened.database.transaction() as connection:
        reservations = SourceReplacementReservations(reopened)
        assert reservations.claim_reconciliation(connection, **arguments(saved)) is None
        with pytest.raises(OnboardingJourneyUnavailable):
            reservations.claim_send(connection, **arguments(saved))
        assert connection.execute("SELECT count(*) FROM onboarding_source_replacement_reservations").fetchone()[0] == 1

@pytest.mark.parametrize("new_customer", [None, "customer-different", "customer-original"])
@pytest.mark.parametrize("fixture", [INITIAL], indirect=True)
def test_initial_replacement_identity_is_retained_and_cannot_change(retained, new_customer):
    store, path, clock, journey, _, saved, _ = retained
    assert journey["kind"] == "initial-enrollment"
    reservations, replacement = claimed(retained)
    identity = str(uuid4())
    result = prepared_result(replacement, clock) | {"expected_customer_id": "customer-original"}
    with store.database.transaction() as connection:
        reservations.record_result(connection, **arguments(saved), successor_journey_id=identity, result=result)
    reopened = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    owner = SourceReplacementReservations(reopened)
    successor_id = result["successor"]["operation_id"]
    with reopened.database.transaction() as connection:
        recorded = owner.initial_identity(connection, successor_id)
        assert recorded["expected_customer_id"] == "customer-original"
        assert recorded["intended_creator_id"] == "creator.test"
        assert recorded["predecessor_operation_id"] == saved["operation_id"]
        successor = owner.preparations.get(successor_id, connection=connection)
    clock[0] += timedelta(minutes=6)
    with reopened.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET state='expired' WHERE journey_id=?", (identity,))
        owner.reserve(connection, **arguments(successor))
        second = owner.claim_send(connection, **arguments(successor))
        response = prepared_result(second, clock)
        if new_customer is not None:
            response["expected_customer_id"] = new_customer
        if new_customer != "customer-original":
            with pytest.raises(OnboardingJourneyUnavailable):
                owner.record_result(connection, **arguments(successor), successor_journey_id=str(uuid4()), result=response)
            assert owner.outcome(connection, successor_id) is None
        else:
            outcome = owner.record_result(connection, **arguments(successor), successor_journey_id=str(uuid4()), result=response)
            assert outcome["status"] == "prepared"
            assert owner.initial_identity(connection, response["successor"]["operation_id"])["expected_customer_id"] == new_customer
