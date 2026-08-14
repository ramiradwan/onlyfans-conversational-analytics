"""Durable, installation-scoped provisioning candidate/approval state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.persistence.auth import (
    AuthenticationStateError,
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
