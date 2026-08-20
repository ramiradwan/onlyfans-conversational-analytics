"""Creator-association initiation from durable enrollment coordinates."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.persistence.auth import (
    ClaimSubmission,
    SQLiteAuthenticationStore,
)
from app.provisioning.app import (
    PROVISIONING_CREATOR_ASSOCIATION_PATH,
    create_provisioning_app,
)
from app.provisioning.creator_association import (
    durable_creator_association_initiation,
    initiate_creator_association,
    new_association_request_id,
)
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)
from app.security.hosted_grants import (
    CreatorAssociationRequest,
    CreatorAssociationStatus,
)


CLAIM_ID = "claim-1"
ONBOARDING_TRANSACTION_ID = "onboarding-1"
ORGANIZATION_ID = "organization-1"
INSTALLATION_ID = "installation-1"
ACCOUNT_ID = "creator-account-1"
REQUEST_ID = "0198a1b2-c3d4-7000-8000-000000000001"
UPDATED_AT = "2026-08-20T00:00:00Z"
HANDOFF_TOKEN = "t" * 32


class RecordingHostedClient:
    def __init__(self) -> None:
        self.requests: list[CreatorAssociationRequest] = []

    def request_creator_association(
        self, association: CreatorAssociationRequest
    ) -> CreatorAssociationStatus:
        self.requests.append(association)
        return CreatorAssociationStatus(UPDATED_AT)


def consumed_claim(store: SQLiteAuthenticationStore) -> None:
    instant = datetime(2026, 8, 20, tzinfo=timezone.utc)
    store.record_claim_submission(
        ClaimSubmission(
            claim_id=CLAIM_ID,
            onboarding_transaction_id=ONBOARDING_TRANSACTION_ID,
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            submitted_at=instant,
        )
    )
    assert store.resolve_claim_submission(CLAIM_ID, outcome=None, resolved_at=instant)


def test_initiation_builds_the_hosted_tuple_from_the_consumed_claim(tmp_path) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    consumed_claim(store)
    client = RecordingHostedClient()

    result = initiate_creator_association(
        store=store,
        client=client,
        detected_creator_account_id=ACCOUNT_ID,
        request_id_factory=lambda: REQUEST_ID,
        now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert result.association_request_id == REQUEST_ID
    assert result.status == "pending"
    assert result.updated_at == UPDATED_AT
    assert client.requests == [
        CreatorAssociationRequest(
            association_request_id=REQUEST_ID,
            onboarding_transaction_id=ONBOARDING_TRANSACTION_ID,
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            creator_account_id=ACCOUNT_ID,
        )
    ]
    candidate = store.provisioning_candidate(REQUEST_ID)
    assert candidate is not None
    assert candidate.installation_id == INSTALLATION_ID
    assert candidate.organization_id == ORGANIZATION_ID
    assert candidate.onboarding_transaction_id == ONBOARDING_TRANSACTION_ID
    assert candidate.creator_account_id == ACCOUNT_ID


def test_caller_coordinates_are_refused_before_the_hosted_request(tmp_path) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    consumed_claim(store)
    hosted = RecordingHostedClient()
    action = durable_creator_association_initiation(
        lambda: store,
        hosted_origin="https://hosted.example",
        client_factory=lambda _: hosted,
        request_id_factory=lambda: REQUEST_ID,
    )
    application = create_provisioning_app(
        claim_submission=lambda *, package: None,
        creator_association_initiation=action,
        completion_ready=lambda: False,
        finalize_action=lambda **_: None,
        launcher_handoff_token=HANDOFF_TOKEN,
    )
    client, headers = _bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_ASSOCIATION_PATH,
        json={
            "detected_creator_account_id": ACCOUNT_ID,
            "onboarding_transaction_id": "attacker-onboarding",
            "organization_id": "attacker-organization",
            "installation_id": "attacker-installation",
        },
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "claim_coordinates_from_caller",
    }
    assert hosted.requests == []
    assert store.provisioning_candidate(REQUEST_ID) is None


def test_uuidv7_request_identifiers_use_the_rfc_timestamp_version_and_variant() -> None:
    identifier = new_association_request_id(
        now=1_723_124_800.123,
        entropy=bytes.fromhex("00112233445566778899"),
    )

    assert identifier == "0191323d-da7b-7011-a233-445566778899"
    assert identifier == identifier.lower()
    assert identifier[14] == "7"
    assert identifier[19] in "89ab"


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
    cookie = {
        "Cookie": (
            f"{PROVISIONING_SESSION_COOKIE_NAME}="
            f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
        )
    }
    token = client.get("/provisioning", headers=cookie).text.split(
        'data-provisioning-csrf="'
    )[1].split('"')[0]
    return client, {
        **cookie,
        "Origin": PROVISIONING_ORIGIN,
        PROVISIONING_CSRF_HEADER: token,
    }
