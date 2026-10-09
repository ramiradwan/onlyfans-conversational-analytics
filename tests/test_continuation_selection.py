"""Saved target selection against disposable encrypted authentication stores."""

from datetime import datetime, timedelta, timezone

import pytest

from app.persistence.auth import (
    ClaimSubmission, InstallationKeyReference, InstallationKeyReservation,
    ProvisioningCandidate, ProvisioningCandidateState, SQLiteAuthenticationStore,
)
from app.persistence.continuation_selection import (
    ContinuationSelectionStore, ContinuationSelectionUnavailable,
)
from app.persistence.onboarding import OnboardingJourneyStore


pytestmark = [pytest.mark.ci_tier("fast")]
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)


def enroll(store, *, claim_id="claim-1", installation="installation-1", activate=True):
    if activate and store.installation_key_reference() is None:
        reservation = InstallationKeyReservation("test-provider", "test-key", "ES256", NOW)
        store.reserve_installation_key(reservation)
        store.activate_installation_key(InstallationKeyReference(
            reservation.provider_name, reservation.provider_key_name, reservation.algorithm,
            "ik1." + "a" * 43, "a" * 43, '{"fixture":"selection-only"}', NOW, NOW,
        ))
    store.record_claim_submission(ClaimSubmission(
        claim_id, "source-transaction", "organization-1", installation, NOW,
    ))
    store.resolve_claim_submission(claim_id, outcome=None, resolved_at=NOW)


def add_candidate(store, suffix="1", *, creator=None, approved=False, **coordinates):
    values = dict(
        association_request_id=f"request-{suffix}", installation_id="installation-1",
        onboarding_transaction_id="source-transaction", organization_id="organization-1",
        creator_account_id=creator or f"creator-{suffix}",
        state=ProvisioningCandidateState.PENDING, requested_at=NOW,
    )
    values.update(coordinates)
    store.record_provisioning_candidate(ProvisioningCandidate(**values))
    if approved:
        store.approve_provisioning_candidate(values["association_request_id"], resolved_at=NOW)
    return values["association_request_id"]


def finalize_fixture(store, *, revoked=False):
    # Selection uses the durable account slot; no signed authority is simulated.
    with store.database.transaction() as connection:
        connection.execute(
            "INSERT INTO authorized_account_bindings VALUES (?,?,?,?,?,?,?)",
            ("creator-1", "installation-1", "creator-1", "request-1", "a" * 64,
             NOW.isoformat(), NOW.isoformat() if revoked else None),
        )


def refused(store, reason, **intent):
    with pytest.raises(ContinuationSelectionUnavailable) as raised:
        ContinuationSelectionStore(store).select(**intent)
    assert raised.value.reason == reason


def test_no_enrollment_is_read_only_and_never_creates_a_key(store, monkeypatch):
    from app.security.installation_key import InstallationKeyAuthority

    def forbidden(*args, **kwargs):
        pytest.fail("selection entered a key-creating helper")

    monkeypatch.setattr(InstallationKeyAuthority, "ensure_ready", forbidden)
    refused(store, "registration_required")
    assert store.installation_key_reservation() is None
    enroll(store, activate=False)
    add_candidate(store)
    refused(store, "installation_key_unavailable")
    assert store.installation_key_reservation() is None


def test_reserved_but_inactive_key_cannot_be_selected(store):
    enroll(store, activate=False)
    store.reserve_installation_key(InstallationKeyReservation("test", "reserved", "ES256", NOW))
    add_candidate(store)
    refused(store, "installation_key_unavailable")
    assert store.installation_key_reference() is None


def test_unresolved_claim_prevents_selection_of_consumed_claim(store):
    enroll(store)
    add_candidate(store)
    store.record_claim_submission(ClaimSubmission("unknown", "other-txn", "org-2", "other", NOW))
    refused(store, "enrollment_unresolved")


def test_multiple_consumed_claims_are_not_selected_by_age(store):
    enroll(store)
    enroll(store, claim_id="claim-2", installation="installation-2")
    add_candidate(store)
    refused(store, "enrollment_ambiguous")


def test_unique_pending_wins_over_multiple_coherent_approved_accounts(store):
    enroll(store)
    add_candidate(store, "1", approved=True)
    add_candidate(store, "2", approved=True)
    expected = add_candidate(store, "3")
    result = ContinuationSelectionStore(store).select()
    assert result.candidate.association_request_id == expected
    assert result.candidate.state is ProvisioningCandidateState.PENDING
    assert result.finalized is False


def test_exact_intent_preserves_selected_approved_target_despite_pending_account(store):
    enroll(store)
    expected = add_candidate(store, approved=True)
    add_candidate(store, "2")
    result = ContinuationSelectionStore(store).select(association_request_id=expected)
    assert result.candidate.association_request_id == expected


def test_missing_explicit_target_never_substitutes_unique_pending(store):
    enroll(store)
    add_candidate(store)
    refused(store, "target_unavailable", association_request_id="missing")


def test_sole_approved_target_can_resume_without_unexpired_grants(store):
    enroll(store)
    add_candidate(store, approved=True)
    result = ContinuationSelectionStore(store).select()
    assert result.candidate.state is ProvisioningCandidateState.APPROVED
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0] == 0


def test_multiple_unnamed_approved_accounts_require_resolution(store):
    enroll(store)
    add_candidate(store, approved=True)
    add_candidate(store, "2", approved=True)
    refused(store, "selection_required")


def test_competing_histories_for_same_creator_are_not_a_chooser(store):
    enroll(store)
    add_candidate(store, approved=True)
    add_candidate(store, "2", creator="creator-1")
    refused(store, "lineage_conflict")


@pytest.mark.parametrize("revoked", [False, True])
def test_finalized_account_keeps_its_slot_even_after_revocation(store, revoked):
    enroll(store)
    add_candidate(store, approved=True)
    finalize_fixture(store, revoked=revoked)
    add_candidate(store, "2")
    result = ContinuationSelectionStore(store).select()
    assert result.candidate.association_request_id == "request-1"
    assert result.finalized is True
    assert (result.binding_revoked_at is not None) is revoked
    refused(store, "account_reassignment", association_request_id="request-2")


@pytest.mark.parametrize("coordinate", ["organization_id", "onboarding_transaction_id"])
def test_candidate_with_wrong_enrollment_provenance_is_refused(store, coordinate):
    enroll(store)
    add_candidate(store, **{coordinate: "wrong"})
    refused(store, "lineage_conflict")


def test_foreign_or_cancelled_candidates_cannot_become_targets(store):
    enroll(store)
    add_candidate(store)
    store.cancel_provisioning_candidate("request-1", resolved_at=NOW)
    add_candidate(store, "2", installation_id="foreign")
    refused(store, "target_unavailable")
    refused(store, "target_unavailable", association_request_id="request-1")
    refused(store, "target_unavailable", association_request_id="request-2")


def test_old_journey_expiry_and_deletion_does_not_erase_saved_target(store):
    enroll(store)
    add_candidate(store)
    journeys = OnboardingJourneyStore(store)
    old = journeys.open()
    store._clock = lambda: NOW + timedelta(hours=2)
    assert journeys.get(old["journey_id"]) is None
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 0
    result = ContinuationSelectionStore(store).select()
    assert result.claim.onboarding_transaction_id == "source-transaction"
    assert result.candidate.association_request_id == "request-1"


@pytest.mark.parametrize("mutation", ["key", "candidate", "binding"])
def test_selection_is_rechecked_in_context_commit_transaction(store, mutation):
    enroll(store)
    add_candidate(store, approved=True)
    selector = ContinuationSelectionStore(store)
    target = selector.select()
    if mutation == "key":
        with store.database.transaction() as connection:
            connection.execute("UPDATE installation_key_reference SET installation_key_id = ?", ("ik1." + "b" * 43,))
    elif mutation == "candidate":
        with store.database.transaction() as connection:
            connection.execute("UPDATE provisioning_candidates SET creator_account_id = 'changed'")
    else:
        finalize_fixture(store)
    with store.database.transaction() as connection:
        with pytest.raises(ContinuationSelectionUnavailable) as raised:
            selector.require_current(connection, target)
    assert raised.value.reason == "selection_changed"


def test_finalized_binding_requires_its_exact_candidate(store):
    enroll(store)
    add_candidate(store, approved=True)
    finalize_fixture(store)
    with store.database.transaction() as connection:
        connection.execute("UPDATE authorized_account_bindings SET creator_account_id = 'different'")
    refused(store, "lineage_conflict")
