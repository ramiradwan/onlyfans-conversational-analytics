"""Exact source evidence and one irreversible send claim, in real SQLCipher."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.persistence.auth import InstallationKeyReservation, SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.persistence.source_preparations import SourcePreparations, RETENTION
from app.persistence.source_replacement_reservations import SourceReplacementReservations
from app.security.source_replacement import INITIAL, REGISTERED, original_prepare
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
    with store.database.transaction() as connection:
        connection.execute("INSERT INTO onboarding_journeys (journey_id,kind,installation_id,operation_id,"
            "created_at,expires_at,state) VALUES (?,?,?,?,?,?,'waiting')",
            (identity, source_kind, destination["installation_id"], request["operation_id"], NOW.isoformat(),
                (NOW + timedelta(minutes=30)).isoformat()))
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
