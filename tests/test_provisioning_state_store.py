"""Durable provisioning state: claim submissions and candidate/approval rows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.persistence.auth import (
    AuthenticationStateError,
    ClaimSubmission,
    ClaimSubmissionState,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def instant() -> datetime:
    return datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(instant: datetime) -> MutableClock:
    return MutableClock(instant)


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)


def candidate(
    instant: datetime,
    *,
    association_request_id: str = "assoc-1",
    installation_id: str = "install-1",
    onboarding_transaction_id: str = "txn-1",
    organization_id: str = "org-1",
    creator_account_id: str = "creator-1",
) -> ProvisioningCandidate:
    return ProvisioningCandidate(
        association_request_id=association_request_id,
        installation_id=installation_id,
        onboarding_transaction_id=onboarding_transaction_id,
        organization_id=organization_id,
        creator_account_id=creator_account_id,
        state=ProvisioningCandidateState.PENDING,
        requested_at=instant,
    )


def test_record_and_read_provisioning_candidate_round_trips(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))

    read_by_id = store.provisioning_candidate("assoc-1")
    read_by_installation = store.pending_provisioning_candidate("install-1")

    assert read_by_id == candidate(instant)
    assert read_by_installation == candidate(instant)


def test_unknown_candidate_and_installation_read_as_none(
    store: SQLiteAuthenticationStore,
) -> None:
    assert store.provisioning_candidate("missing") is None
    assert store.pending_provisioning_candidate("missing-installation") is None


def test_second_pending_candidate_for_the_same_installation_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))

    with pytest.raises(AuthenticationStateError, match="already pending"):
        store.record_provisioning_candidate(
            candidate(
                instant,
                association_request_id="assoc-2",
                creator_account_id="creator-2",
            )
        )

    # The first candidate is untouched; no second row was created.
    assert store.pending_provisioning_candidate("install-1").association_request_id == (
        "assoc-1"
    )
    assert store.provisioning_candidate("assoc-2") is None


def test_a_pending_candidate_for_a_different_installation_does_not_conflict(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))
    store.record_provisioning_candidate(
        candidate(instant, association_request_id="assoc-2", installation_id="install-2")
    )

    assert store.pending_provisioning_candidate("install-1") is not None
    assert store.pending_provisioning_candidate("install-2") is not None


def test_cancelling_a_pending_candidate_frees_the_installation_slot(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))

    cancelled = store.cancel_provisioning_candidate(
        "assoc-1", resolved_at=instant + timedelta(minutes=1)
    )

    assert cancelled is True
    assert store.pending_provisioning_candidate("install-1") is None
    stored = store.provisioning_candidate("assoc-1")
    assert stored.state is ProvisioningCandidateState.CANCELLED
    assert stored.resolved_at == instant + timedelta(minutes=1)

    # A new/different candidate may now start a fresh pending transaction.
    store.record_provisioning_candidate(
        candidate(instant, association_request_id="assoc-2", creator_account_id="creator-2")
    )
    assert store.pending_provisioning_candidate("install-1").association_request_id == (
        "assoc-2"
    )


def test_approving_a_pending_candidate_resolves_it_and_clears_the_pending_slot(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))

    approved = store.approve_provisioning_candidate(
        "assoc-1", resolved_at=instant + timedelta(minutes=2)
    )

    assert approved is True
    assert store.pending_provisioning_candidate("install-1") is None
    stored = store.provisioning_candidate("assoc-1")
    assert stored.state is ProvisioningCandidateState.APPROVED
    assert stored.resolved_at == instant + timedelta(minutes=2)


def test_approved_rows_are_additive_across_multiple_accounts(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """A9b: an approved row is never overwritten by another account."""

    store.record_provisioning_candidate(candidate(instant))
    store.approve_provisioning_candidate("assoc-1", resolved_at=instant)

    store.record_provisioning_candidate(
        candidate(instant, association_request_id="assoc-2", creator_account_id="creator-2")
    )
    store.approve_provisioning_candidate("assoc-2", resolved_at=instant)

    first = store.provisioning_candidate("assoc-1")
    second = store.provisioning_candidate("assoc-2")
    assert first.state is ProvisioningCandidateState.APPROVED
    assert first.creator_account_id == "creator-1"
    assert second.state is ProvisioningCandidateState.APPROVED
    assert second.creator_account_id == "creator-2"


def test_approving_an_already_cancelled_candidate_does_not_resurrect_it(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """A stale/competing transition on a resolved row must not succeed."""

    store.record_provisioning_candidate(candidate(instant))
    store.cancel_provisioning_candidate("assoc-1", resolved_at=instant + timedelta(minutes=1))

    late_approve = store.approve_provisioning_candidate(
        "assoc-1", resolved_at=instant + timedelta(minutes=5)
    )

    assert late_approve is False
    stored = store.provisioning_candidate("assoc-1")
    assert stored.state is ProvisioningCandidateState.CANCELLED
    assert stored.resolved_at == instant + timedelta(minutes=1)


def test_approving_an_already_approved_candidate_a_second_time_is_a_no_op(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_provisioning_candidate(candidate(instant))
    store.approve_provisioning_candidate("assoc-1", resolved_at=instant + timedelta(minutes=1))

    second_call = store.approve_provisioning_candidate(
        "assoc-1", resolved_at=instant + timedelta(minutes=9)
    )

    assert second_call is False
    stored = store.provisioning_candidate("assoc-1")
    assert stored.resolved_at == instant + timedelta(minutes=1)


def test_competing_provisioning_candidate_transitions_resolve_exactly_once(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """Concurrent approve/cancel races against the same pending candidate: exactly
    one transition may win, mirroring the store's other compare-and-swap
    consumers (`advance_webauthn_signature_count`, `consume_ticket`)."""

    store.record_provisioning_candidate(candidate(instant))
    contenders = 12
    barrier = Barrier(contenders)

    def approve() -> bool:
        barrier.wait()
        return store.approve_provisioning_candidate("assoc-1", resolved_at=instant)

    with ThreadPoolExecutor(max_workers=contenders) as executor:
        results = list(executor.map(lambda _: approve(), range(contenders)))

    assert results.count(True) == 1
    assert results.count(False) == contenders - 1
    assert store.provisioning_candidate("assoc-1").state == (
        ProvisioningCandidateState.APPROVED
    )


def test_recording_an_empty_field_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        store.record_provisioning_candidate(candidate(instant, installation_id=""))


def test_recording_a_non_pending_or_already_resolved_candidate_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    approved = ProvisioningCandidate(
        association_request_id="assoc-1",
        installation_id="install-1",
        onboarding_transaction_id="txn-1",
        organization_id="org-1",
        creator_account_id="creator-1",
        state=ProvisioningCandidateState.APPROVED,
        requested_at=instant,
    )
    with pytest.raises(ValueError, match="must start pending"):
        store.record_provisioning_candidate(approved)

    resolved_pending = ProvisioningCandidate(
        association_request_id="assoc-1",
        installation_id="install-1",
        onboarding_transaction_id="txn-1",
        organization_id="org-1",
        creator_account_id="creator-1",
        state=ProvisioningCandidateState.PENDING,
        requested_at=instant,
        resolved_at=instant,
    )
    with pytest.raises(ValueError, match="must not be resolved"):
        store.record_provisioning_candidate(resolved_pending)


def test_provisioning_candidates_are_durable_across_store_reopen(
    tmp_path: Path, clock: MutableClock, instant: datetime
) -> None:
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=clock)
    store.record_provisioning_candidate(candidate(instant))
    store.approve_provisioning_candidate("assoc-1", resolved_at=instant)

    reopened = SQLiteAuthenticationStore(path, clock=clock)
    stored = reopened.provisioning_candidate("assoc-1")
    assert stored is not None
    assert stored.state is ProvisioningCandidateState.APPROVED


def submission(
    instant: datetime,
    *,
    claim_id: str = "claim-1",
    onboarding_transaction_id: str = "txn-1",
    organization_id: str = "org-1",
    installation_id: str = "install-1",
) -> ClaimSubmission:
    return ClaimSubmission(
        claim_id=claim_id,
        onboarding_transaction_id=onboarding_transaction_id,
        organization_id=organization_id,
        installation_id=installation_id,
        submitted_at=instant,
    )


def test_a_recorded_claim_submission_reads_back_unresolved(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))

    stored = store.claim_submission("claim-1")

    assert stored == submission(instant)
    assert stored.state is ClaimSubmissionState.SUBMITTED
    assert stored.outcome is None
    assert stored.resolved_at is None
    assert store.unresolved_claim_submissions() == (submission(instant),)


def test_an_unknown_claim_reads_as_none(store: SQLiteAuthenticationStore) -> None:
    assert store.claim_submission("missing") is None
    assert store.unresolved_claim_submissions() == ()


def test_a_consumed_claim_leaves_the_unresolved_set(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))

    resolved = store.resolve_claim_submission(
        "claim-1", outcome=None, resolved_at=instant + timedelta(seconds=3)
    )

    assert resolved is True
    stored = store.claim_submission("claim-1")
    assert stored.state is ClaimSubmissionState.CONSUMED
    assert stored.outcome is None
    assert stored.resolved_at == instant + timedelta(seconds=3)
    assert store.unresolved_claim_submissions() == ()


def test_a_refused_claim_records_its_nonsecret_reason(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))

    resolved = store.resolve_claim_submission(
        "claim-1", outcome="hosted_unavailable", resolved_at=instant
    )

    assert resolved is True
    stored = store.claim_submission("claim-1")
    assert stored.state is ClaimSubmissionState.REFUSED
    assert stored.outcome == "hosted_unavailable"
    assert store.unresolved_claim_submissions() == ()


def test_resolving_an_unrecorded_claim_moves_nothing(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    assert (
        store.resolve_claim_submission("claim-1", outcome=None, resolved_at=instant)
        is False
    )
    assert store.claim_submission("claim-1") is None


def test_a_retried_claim_reopens_the_same_row_as_uncertain(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """A retry after a refusal is uncertain again until the plane answers."""

    store.record_claim_submission(submission(instant))
    store.resolve_claim_submission(
        "claim-1", outcome="hosted_unavailable", resolved_at=instant
    )
    retried_at = instant + timedelta(minutes=5)

    store.record_claim_submission(submission(retried_at))

    stored = store.claim_submission("claim-1")
    assert stored == submission(retried_at)
    assert store.unresolved_claim_submissions() == (submission(retried_at),)


def test_a_consumed_claim_is_terminal_and_a_later_attempt_cannot_erase_it(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))
    store.resolve_claim_submission("claim-1", outcome=None, resolved_at=instant)
    later = instant + timedelta(minutes=5)

    store.record_claim_submission(submission(later))
    late_refusal = store.resolve_claim_submission(
        "claim-1", outcome="claim_already_consumed", resolved_at=later
    )

    assert late_refusal is False
    stored = store.claim_submission("claim-1")
    assert stored.state is ClaimSubmissionState.CONSUMED
    assert stored.submitted_at == instant
    assert stored.resolved_at == instant


def test_different_coordinates_under_a_recorded_claim_id_are_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))

    with pytest.raises(AuthenticationStateError, match="do not match"):
        store.record_claim_submission(submission(instant, installation_id="install-2"))

    assert store.claim_submission("claim-1").installation_id == "install-1"


def test_two_claims_are_independently_recoverable(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    later = instant + timedelta(minutes=1)
    store.record_claim_submission(submission(instant))
    store.record_claim_submission(submission(later, claim_id="claim-2"))

    store.resolve_claim_submission("claim-1", outcome=None, resolved_at=later)

    assert store.unresolved_claim_submissions() == (
        submission(later, claim_id="claim-2"),
    )


def test_recording_an_empty_or_already_resolved_claim_submission_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        store.record_claim_submission(submission(instant, organization_id=""))

    with pytest.raises(ValueError, match="must start submitted"):
        store.record_claim_submission(
            ClaimSubmission(
                claim_id="claim-1",
                onboarding_transaction_id="txn-1",
                organization_id="org-1",
                installation_id="install-1",
                submitted_at=instant,
                state=ClaimSubmissionState.CONSUMED,
                resolved_at=instant,
            )
        )

    with pytest.raises(ValueError, match="must not be resolved"):
        store.record_claim_submission(
            ClaimSubmission(
                claim_id="claim-1",
                onboarding_transaction_id="txn-1",
                organization_id="org-1",
                installation_id="install-1",
                submitted_at=instant,
                outcome="hosted_unavailable",
            )
        )

    assert store.claim_submission("claim-1") is None


def test_resolving_with_an_empty_outcome_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_claim_submission(submission(instant))

    with pytest.raises(ValueError, match="must not be empty"):
        store.resolve_claim_submission("claim-1", outcome="", resolved_at=instant)

    assert store.claim_submission("claim-1").state is ClaimSubmissionState.SUBMITTED


def test_claim_submissions_are_durable_across_store_reopen(
    tmp_path: Path, clock: MutableClock, instant: datetime
) -> None:
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=clock)
    store.record_claim_submission(submission(instant))
    store.record_claim_submission(submission(instant, claim_id="claim-2"))
    store.resolve_claim_submission("claim-2", outcome=None, resolved_at=instant)

    reopened = SQLiteAuthenticationStore(path, clock=clock)

    assert reopened.unresolved_claim_submissions() == (submission(instant),)
    assert reopened.claim_submission("claim-2").state is ClaimSubmissionState.CONSUMED
