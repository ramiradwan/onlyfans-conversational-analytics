"""Creator-account binding acquisition from durable provisioning state."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import packaged_entry
from app.persistence.auth import (
    AuthenticationStore,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.provisioning import binding_acquisition as binding_acquisition_module
from app.provisioning.app import PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH
from app.provisioning.binding_acquisition import (
    BindingAcquisitionStatus,
    acquire_creator_account_binding,
)
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)
from app.security.hosted_grants import CreatorAssociationRequest


ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
ACCOUNT_ID = "creator-account-1"
OTHER_ACCOUNT_ID = "creator-account-2"
ASSOCIATION_REQUEST_ID = "0198a1b2-c3d4-7000-8000-000000000001"
MEMBERSHIP_REFERENCE_ID = "membership-reference-1"
HANDOFF_TOKEN = "t" * 32
HOSTED_ORIGIN = "https://hosted.example"


def _now() -> datetime:
    return datetime(2026, 8, 20, tzinfo=timezone.utc)


def _grant(
    grant_type: str,
    *,
    reference_id: str,
    creator_account_id: str | None = None,
    verified_at: datetime | None = None,
) -> VerifiedGrantReference:
    instant = _now()
    verification_time = verified_at or instant
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"{reference_id}-identifier",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(reference_id.encode("ascii")).hexdigest(),
        issuer="https://identity.example",
        subject="customer-subject-1",
        installation_id=INSTALLATION_ID,
        creator_account_id=creator_account_id,
        valid_from=instant - timedelta(hours=1),
        expires_at=instant + timedelta(hours=1),
        verified_at=verification_time,
        organization_id=ORGANIZATION_ID,
        installation_key_id="installation-key-1",
        installation_key_jkt="installation-key-jkt-1",
        membership_id="membership-1" if grant_type == "membership_snapshot" else None,
        approval_id="approval-1" if grant_type == "creator_account_binding" else None,
        approval_revision=1 if grant_type == "creator_account_binding" else None,
        allowed_creator_account_ids=(OTHER_ACCOUNT_ID,)
        if grant_type == "membership_snapshot"
        else None,
        membership_roles=("owner",) if grant_type == "membership_snapshot" else None,
    )


def _record_pending_candidate(store: AuthenticationStore) -> None:
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=ASSOCIATION_REQUEST_ID,
            installation_id=INSTALLATION_ID,
            onboarding_transaction_id="onboarding-1",
            organization_id=ORGANIZATION_ID,
            creator_account_id=ACCOUNT_ID,
            state=ProvisioningCandidateState.PENDING,
            requested_at=_now(),
        )
    )


class RecordingBindingClient:
    def __init__(
        self,
        _store: AuthenticationStore,
        *,
        binding_verified_at: datetime | None = None,
    ) -> None:
        self.calls: list[tuple[CreatorAssociationRequest, str]] = []
        self.binding_verified_at = binding_verified_at

    def acquire_creator_account_binding(
        self,
        association: CreatorAssociationRequest,
        *,
        membership_reference_id: str,
    ) -> VerifiedGrantReference:
        self.calls.append((association, membership_reference_id))
        binding = _grant(
            "creator_account_binding",
            reference_id="creator-binding-reference-1",
            creator_account_id=association.creator_account_id,
            verified_at=self.binding_verified_at,
        )
        return binding


def test_acquisition_reconstructs_the_request_from_the_candidate_and_approves_it(
    tmp_path: Path,
) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    store.record_verified_grant(
        _grant("membership_snapshot", reference_id=MEMBERSHIP_REFERENCE_ID)
    )
    _record_pending_candidate(store)
    client = RecordingBindingClient(store)

    result = acquire_creator_account_binding(
        store=store,
        client=client,
        now=_now,
    )

    assert result.association_request_id == ASSOCIATION_REQUEST_ID
    assert result.status == "approved"
    assert client.calls == [
        (
            CreatorAssociationRequest(
                association_request_id=ASSOCIATION_REQUEST_ID,
                onboarding_transaction_id="onboarding-1",
                organization_id=ORGANIZATION_ID,
                installation_id=INSTALLATION_ID,
                creator_account_id=ACCOUNT_ID,
            ),
            MEMBERSHIP_REFERENCE_ID,
        )
    ]
    candidate = store.provisioning_candidate(ASSOCIATION_REQUEST_ID)
    assert candidate is not None
    assert candidate.state is ProvisioningCandidateState.APPROVED
    assert sorted(grant.grant_type for grant in store.verified_grants()) == [
        "creator_account_binding",
        "membership_snapshot",
    ]
    assert store.authorized_account_bindings() == ()


def test_acquisition_retry_commits_binding_and_approval_after_interrupted_approval(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    store.record_verified_grant(
        _grant("membership_snapshot", reference_id=MEMBERSHIP_REFERENCE_ID)
    )
    _record_pending_candidate(store)
    client = RecordingBindingClient(store)
    original_approve = store._approve_provisioning_candidate_in_transaction

    def interrupt_approval(*_args, **_kwargs) -> bool:
        raise sqlite3.OperationalError("interrupted candidate approval")

    monkeypatch.setattr(
        store, "_approve_provisioning_candidate_in_transaction", interrupt_approval
    )
    with pytest.raises(sqlite3.OperationalError, match="interrupted candidate approval"):
        acquire_creator_account_binding(store=store, client=client, now=_now)

    candidate_after_interruption = store.provisioning_candidate(ASSOCIATION_REQUEST_ID)
    assert candidate_after_interruption is not None
    assert candidate_after_interruption.state is ProvisioningCandidateState.PENDING
    assert [grant.grant_type for grant in store.verified_grants()] == [
        "membership_snapshot"
    ]

    monkeypatch.setattr(
        store, "_approve_provisioning_candidate_in_transaction", original_approve
    )
    try:
        result = acquire_creator_account_binding(store=store, client=client, now=_now)
    except Exception as error:
        result = error

    assert result == BindingAcquisitionStatus(ASSOCIATION_REQUEST_ID)
    assert len(client.calls) == 2
    candidate_after_retry = store.provisioning_candidate(ASSOCIATION_REQUEST_ID)
    assert candidate_after_retry is not None
    assert candidate_after_retry.state is ProvisioningCandidateState.APPROVED
    assert sorted(grant.grant_type for grant in store.verified_grants()) == [
        "creator_account_binding",
        "membership_snapshot",
    ]


def test_acquisition_retry_approves_a_pending_candidate_with_its_reverified_binding(
    tmp_path: Path,
) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    store.record_verified_grant(
        _grant("membership_snapshot", reference_id=MEMBERSHIP_REFERENCE_ID)
    )
    _record_pending_candidate(store)
    store.record_verified_grant(
        _grant(
            "creator_account_binding",
            reference_id="creator-binding-reference-1",
            creator_account_id=ACCOUNT_ID,
            verified_at=_now(),
        )
    )
    client = RecordingBindingClient(
        store,
        binding_verified_at=_now() + timedelta(minutes=1),
    )

    try:
        result = acquire_creator_account_binding(store=store, client=client, now=_now)
    except Exception as error:
        result = error

    assert result == BindingAcquisitionStatus(ASSOCIATION_REQUEST_ID)
    assert len(client.calls) == 1
    candidate = store.provisioning_candidate(ASSOCIATION_REQUEST_ID)
    assert candidate is not None
    assert candidate.state is ProvisioningCandidateState.APPROVED
    assert sorted(grant.grant_type for grant in store.verified_grants()) == [
        "creator_account_binding",
        "membership_snapshot",
    ]


class ComposedBindingClient(RecordingBindingClient):
    instances: list["ComposedBindingClient"] = []

    def __init__(self, _transport, _proof_authority, store: AuthenticationStore) -> None:
        super().__init__(store)
        self.instances.append(self)


def test_packaged_entry_routes_binding_acquisition_from_local_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    data_directory = tmp_path / "runtime-data"
    data_directory.mkdir()
    store = SQLiteAuthenticationStore(data_directory / "auth.sqlite3")
    store.record_verified_grant(
        _grant("membership_snapshot", reference_id=MEMBERSHIP_REFERENCE_ID)
    )
    _record_pending_candidate(store)
    ComposedBindingClient.instances.clear()
    monkeypatch.setattr(
        binding_acquisition_module, "HostedGrantClient", ComposedBindingClient
    )
    monkeypatch.setenv(
        packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, HANDOFF_TOKEN
    )
    monkeypatch.setenv(
        packaged_entry.PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, HOSTED_ORIGIN
    )

    application = packaged_entry.select_brain_application(data_directory)
    client, headers = _bounded_session(application)
    response = client.post(
        PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH,
        json={},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "association_request_id": ASSOCIATION_REQUEST_ID,
        "status": "approved",
    }
    assert len(ComposedBindingClient.instances) == 1
    assert ComposedBindingClient.instances[0].calls[0][0].creator_account_id == ACCOUNT_ID
    candidate = store.provisioning_candidate(ASSOCIATION_REQUEST_ID)
    assert candidate is not None
    assert candidate.state is ProvisioningCandidateState.APPROVED
    assert store.authorized_account_bindings() == ()


def _bounded_session(application) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + HANDOFF_TOKEN},
    )
    redeemed = client.get(
        f"/provisioning/handoff?code={handoff.json()['handoff_code']}",
        follow_redirects=False,
    )
    cookie = (
        f"{PROVISIONING_SESSION_COOKIE_NAME}="
        f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
    )
    token = (
        client.get("/provisioning", headers={"Cookie": cookie})
        .text.split('data-provisioning-csrf="')[1]
        .split('"')[0]
    )
    return client, {
        "Cookie": cookie,
        "Origin": PROVISIONING_ORIGIN,
        PROVISIONING_CSRF_HEADER: token,
    }
