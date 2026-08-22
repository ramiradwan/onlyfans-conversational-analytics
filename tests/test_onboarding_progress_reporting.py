"""A8 closed-envelope, proof, durability, and failure-separation tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import struct
import base64
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.persistence.auth import (
    AgentPairing,
    AuthContext,
    AuthorizedAccountBinding,
    ClaimSubmission,
    InstallationKeyReference,
    InstallationKeyReservation,
    OnboardingProgressEvent,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.provisioning.progress_reporting import OnboardingProgressCoordinator
from app.security.hosted_grants import (
    PROGRESS_PROFILE,
    PROGRESS_PROOF_AUDIENCE,
    PROGRESS_PROOF_PROFILE,
    HostedGrantClient,
    TransportResponse,
    _PROOF_DOMAIN,
)
from app.security.installation_key import InstallationProof


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
ORGANIZATION_ID = "0198a1b2-c3d4-7000-8000-000000000001"
INSTALLATION_ID = "0198a1b2-c3d4-7000-8000-000000000002"
EVENT_ID = "0198a1b2-c3d4-7400-8000-000000000001"
CORRELATION_ID = "correlation-fixture-001"
KEY_ID = "ik1.AAAAAAAAAAAAAAAAAAAAAA"
MILESTONES = ("installed", "enrolled", "account-bound", "first-capture-ready")
CONTRACTS = Path(__file__).parents[1] / "contracts"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingProofAuthority:
    def __init__(self, key: InstallationKeyReference) -> None:
        self.key = key
        self.signed: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        return self.key

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed.append(challenge)
        return InstallationProof(KEY_ID, "ES256", b"s" * 64)


class ProgressTransport:
    def __init__(self, *, report_status: int = 200, malformed_once: bool = False) -> None:
        self.report_status = report_status
        self.malformed_once = malformed_once
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.seen: set[str] = set()

    def request(
        self, method: str, path: str, *, json_body: dict[str, object]
    ) -> TransportResponse:
        assert method == "POST"
        self.requests.append((path, json_body))
        if path.endswith("/proof-challenges"):
            return _response(
                201,
                {
                    "profile": PROGRESS_PROOF_PROFILE,
                    "purpose": "onboarding-progress-report",
                    "installation_id": INSTALLATION_ID,
                    "challenge": _b64url(b"b" * 32),
                    "audience": PROGRESS_PROOF_AUDIENCE,
                    "issued_at": "2026-07-19T00:00:00.000Z",
                    "expires_at": "2026-07-19T00:01:00.000Z",
                },
            )
        if self.report_status != 200:
            return _response(self.report_status, {"detail": "closed_refusal"})
        request = json_body["request"]
        assert isinstance(request, dict)
        event_id = str(request["event_id"])
        status = "duplicate" if event_id in self.seen else "recorded"
        self.seen.add(event_id)
        if self.malformed_once:
            self.malformed_once = False
            return _response(200, {"status": status})
        return _response(
            200,
            {
                "profile": PROGRESS_PROFILE,
                "event_id": event_id,
                "milestone": request["milestone"],
                "status": status,
                "recorded_at": "2026-07-19T00:00:01.000Z",
            },
        )


def test_all_four_facts_serialize_through_the_pinned_closed_schema_and_fifth_is_refused(
    tmp_path: Path,
) -> None:
    store, key = _enrolled_store(tmp_path)
    authority = RecordingProofAuthority(key)
    transport = ProgressTransport()
    client = HostedGrantClient(transport, authority, store, trust_set={})
    schema = json.loads(
        (CONTRACTS / "schemas/provisioning/v1/onboarding-progress-report.schema.json")
        .read_text("utf-8")
    )["properties"]["request"]
    expected_fields = set(schema["properties"])
    accepted = set(schema["properties"]["milestone"]["enum"])

    for index, milestone in enumerate(MILESTONES, start=1):
        event = _event(milestone, suffix=index)
        assert client.report_onboarding_progress(event) == "delivered"
        request = transport.requests[-1][1]["request"]
        assert isinstance(request, dict)
        assert set(request) == expected_fields == set(schema["required"])
        assert request["milestone"] in accepted

    with pytest.raises(ValueError, match="milestone"):
        client.report_onboarding_progress(_event("fifth-fact", suffix=9))  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["creator_account_id", "count", "error", "metadata"])
def test_prohibited_progress_fields_are_structurally_impossible(
    tmp_path: Path, field: str
) -> None:
    signature = inspect.signature(OnboardingProgressEvent)
    assert field not in signature.parameters
    with pytest.raises(TypeError):
        OnboardingProgressEvent(**{**_event_kwargs("installed", 1), field: "forbidden"})
    event = _event("installed", suffix=1)
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        setattr(event, field, "forbidden")

    store, key = _enrolled_store(tmp_path)
    transport = ProgressTransport()
    client = HostedGrantClient(
        transport, RecordingProofAuthority(key), store, trust_set={}
    )
    assert client.report_onboarding_progress(event) == "delivered"
    emitted = transport.requests[-1][1]["request"]
    assert isinstance(emitted, dict)
    assert field not in emitted
    assert set(emitted) == {
        "profile",
        "event_id",
        "milestone",
        "occurred_at",
        "onboarding_transaction_id",
        "organization_id",
        "installation_id",
        "correlation_id",
    }


def test_progress_proof_binds_purpose_method_path_body_digest_and_active_key(
    tmp_path: Path,
) -> None:
    store, key = _enrolled_store(tmp_path)
    authority = RecordingProofAuthority(key)
    transport = ProgressTransport()
    client = HostedGrantClient(transport, authority, store, trust_set={})
    event = _event("installed", suffix=1)

    assert client.report_onboarding_progress(event) == "delivered"
    fields = _proof_fields(authority.signed[-1])
    request = transport.requests[-1][1]["request"]
    assert fields[0] == b"b" * 32
    assert fields[1] == b"POST"
    assert fields[2] == f"/v1/installations/{INSTALLATION_ID}/onboarding-progress".encode()
    assert fields[3] == hashlib.sha256(_canonical_json(request)).digest()
    assert fields[4] == b"onboarding-progress-report"
    assert fields[5] == INSTALLATION_ID.encode()
    assert fields[6] == b""
    assert fields[7] == KEY_ID.encode()
    assert fields[8] == PROGRESS_PROOF_AUDIENCE.encode()


def test_event_id_survives_restart_and_duplicate_delivery_is_safe(
    tmp_path: Path,
) -> None:
    store, key = _enrolled_store(tmp_path)
    clock = MutableClock()
    transport = ProgressTransport(malformed_once=True)

    first = OnboardingProgressCoordinator(
        lambda: store,
        lambda _: HostedGrantClient(
            transport, RecordingProofAuthority(key), store, trust_set={}
        ),
        clock=clock,
    )
    first.mark_and_flush("installed")
    initial = store.onboarding_progress_events()[0]
    assert _outbox_state(store, initial.event_id) == ("pending", 1)

    restarted_store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    clock.value += timedelta(seconds=31)
    restarted = OnboardingProgressCoordinator(
        lambda: restarted_store,
        lambda _: HostedGrantClient(
            transport, RecordingProofAuthority(key), restarted_store, trust_set={}
        ),
        clock=clock,
    )
    restarted.flush()
    final = restarted_store.onboarding_progress_events()[0]

    assert final.event_id == initial.event_id
    assert transport.requests[-1][1]["request"]["event_id"] == initial.event_id
    assert _outbox_state(restarted_store, initial.event_id) == ("delivered", 2)


def test_installed_waits_for_bound_grant_then_delivers_same_event(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    key = _installation_key(store)
    submission = ClaimSubmission(
        claim_id="0198a1b2-c3d4-7400-8000-000000000020",
        onboarding_transaction_id="onboarding-fixture-001",
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        submitted_at=NOW,
    )
    store.record_claim_submission(submission)
    initial = store.onboarding_progress_events()[0]
    transport = ProgressTransport()
    coordinator = OnboardingProgressCoordinator(
        lambda: store,
        lambda _: HostedGrantClient(
            transport, RecordingProofAuthority(key), store, trust_set={}
        ),
        clock=clock,
    )

    coordinator.flush()
    assert _outbox_state(store, initial.event_id) == ("pending", 1)
    assert transport.requests == []

    store.record_verified_grant(_installation_grant())
    clock.value += timedelta(seconds=31)
    coordinator.flush()

    delivered = store.onboarding_progress_events()[0]
    assert delivered.event_id == initial.event_id
    assert _outbox_state(store, initial.event_id) == ("delivered", 2)
    assert transport.requests[-1][1]["request"]["event_id"] == initial.event_id


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_retryable_http_statuses_remain_queued(tmp_path: Path, status: int) -> None:
    store, key = _enrolled_store(tmp_path)
    event = _event("installed", suffix=1)
    store.enqueue_onboarding_progress(
        "installed",
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
    )
    coordinator = OnboardingProgressCoordinator(
        lambda: store,
        lambda _: HostedGrantClient(
            ProgressTransport(report_status=status),
            RecordingProofAuthority(key),
            store,
            trust_set={},
        ),
        clock=lambda: NOW,
    )

    coordinator.flush()

    assert _outbox_state(store, event.event_id) == ("pending", 1)


@pytest.mark.parametrize("status", [400, 401, 409, 422])
def test_four_xx_refusal_is_terminal_and_does_not_loop(
    tmp_path: Path, status: int
) -> None:
    store, key = _enrolled_store(tmp_path)
    event = _event("installed", suffix=1)
    store.enqueue_onboarding_progress(
        "installed",
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
    )
    coordinator = OnboardingProgressCoordinator(
        lambda: store,
        lambda _: HostedGrantClient(
            ProgressTransport(report_status=status),
            RecordingProofAuthority(key),
            store,
            trust_set={},
        ),
        clock=lambda: NOW,
    )

    coordinator.flush()
    coordinator.flush()

    assert _outbox_state(store, event.event_id) == ("refused", 1)


def test_reporting_failure_does_not_change_runtime_policy_or_grant_validity(
    tmp_path: Path,
) -> None:
    store, key = _enrolled_store(tmp_path)
    event = _event("installed", suffix=1)
    store.enqueue_onboarding_progress(
        "installed",
        event_id=event.event_id,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
    )
    before_policy = store.build_runtime_policy()
    before_grants = store.verified_grants()
    coordinator = OnboardingProgressCoordinator(
        lambda: store,
        lambda _: HostedGrantClient(
            ProgressTransport(report_status=503),
            RecordingProofAuthority(key),
            store,
            trust_set={},
        ),
        clock=lambda: NOW,
    )

    coordinator.flush()

    assert store.build_runtime_policy() == before_policy
    assert store.verified_grants() == before_grants


def test_claim_enrollment_and_pairing_activation_queue_truthful_ordered_facts(
    tmp_path: Path,
) -> None:
    store, _ = _enrolled_store(
        tmp_path, preserve_progress=True, activate_pairing=False
    )

    events = store.onboarding_progress_events()
    assert [event.milestone for event in events] == ["installed", "enrolled"]
    assert events[0].occurred_at < events[1].occurred_at

    policy = store.build_runtime_policy(
        AuthContext("progress-principal", "creator-fixture-001", "agent")
    )
    assert store.activate_agent_pairing(policy, "progress-pairing")
    events = store.onboarding_progress_events()

    assert [event.milestone for event in events] == [
        "installed",
        "enrolled",
        "account-bound",
    ]
    assert len({event.event_id for event in events}) == 3


@pytest.mark.parametrize(
    "entitlement_state", ["missing", "expired", "revoked", "wrong-key"]
)
def test_first_capture_ready_requires_a_current_exact_entitlement(
    tmp_path: Path, entitlement_state: str
) -> None:
    store, _ = _enrolled_store(tmp_path, entitlement_state=entitlement_state)

    event = store.enqueue_onboarding_progress(
        "first-capture-ready",
        event_id=EVENT_ID,
        correlation_id=CORRELATION_ID,
        occurred_at=NOW,
    )

    assert event is None
    assert store.onboarding_progress_events() == ()


def test_first_capture_ready_accepts_current_exact_entitlement(tmp_path: Path) -> None:
    store, _ = _enrolled_store(tmp_path)

    event = store.enqueue_onboarding_progress(
        "first-capture-ready",
        event_id=EVENT_ID,
        correlation_id=CORRELATION_ID,
        occurred_at=NOW,
    )

    assert event is not None
    assert event.milestone == "first-capture-ready"


def _enrolled_store(
    tmp_path: Path,
    *,
    preserve_progress: bool = False,
    activate_pairing: bool = True,
    entitlement_state: str = "valid",
) -> tuple[SQLiteAuthenticationStore, InstallationKeyReference]:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    key = _installation_key(store)
    grant = _installation_grant()
    store.record_verified_grant(grant)
    if entitlement_state != "missing":
        entitlement = VerifiedGrantReference(
            reference_id="vg1.entitlement",
            grant_identifier="0198a1b2-c3d4-7400-8000-000000000011",
            grant_type="license_entitlement",
            grant_digest="3" * 64,
            issuer="issuer",
            subject=f"installation:{INSTALLATION_ID}",
            installation_id=INSTALLATION_ID,
            creator_account_id=None,
            valid_from=NOW - timedelta(days=2),
            expires_at=(
                NOW - timedelta(days=1)
                if entitlement_state == "expired"
                else NOW + timedelta(days=1)
            ),
            verified_at=NOW - timedelta(days=2),
            organization_id=ORGANIZATION_ID,
            installation_key_id=KEY_ID,
            installation_key_jkt=(
                "B" * 43 if entitlement_state == "wrong-key" else "A" * 43
            ),
            entitlement_id="entitlement-fixture-001",
            product_id="product-fixture-001",
        )
        store.record_verified_grant(entitlement)
        if entitlement_state == "revoked":
            store.revoke(
                RevocationKey(
                    RevocationScopeType.VERIFIED_GRANT, entitlement.reference_id
                )
            )
    store.record_claim_submission(
        ClaimSubmission(
            claim_id="0198a1b2-c3d4-7400-8000-000000000020",
            onboarding_transaction_id="onboarding-fixture-001",
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            submitted_at=NOW - timedelta(minutes=1),
        )
    )
    store.resolve_claim_submission(
        "0198a1b2-c3d4-7400-8000-000000000020",
        outcome=None,
        resolved_at=NOW,
    )
    candidate = ProvisioningCandidate(
        association_request_id="0198a1b2-c3d4-7400-8000-000000000030",
        installation_id=INSTALLATION_ID,
        onboarding_transaction_id="onboarding-fixture-001",
        organization_id=ORGANIZATION_ID,
        creator_account_id="creator-fixture-001",
        state=ProvisioningCandidateState.PENDING,
        requested_at=NOW,
    )
    store.record_provisioning_candidate(candidate)
    assert store.approve_provisioning_candidate(
        candidate.association_request_id, resolved_at=NOW
    )
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=candidate.creator_account_id,
            installation_id=INSTALLATION_ID,
            platform_creator_id=candidate.creator_account_id,
            association_request_id=candidate.association_request_id,
            grant_bundle_sha256="2" * 64,
            authorized_at=NOW,
            grant_reference_ids=(grant.reference_id,),
        )
    )
    pairing_grants = (
        VerifiedGrantReference(
            reference_id="vg1.pairing-installation",
            grant_identifier="0198a1b2-c3d4-7400-8000-000000000012",
            grant_type="installation_grant",
            grant_digest="4" * 64,
            issuer="pairing-issuer",
            subject="pairing-subject",
            installation_id=INSTALLATION_ID,
            creator_account_id=None,
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            verified_at=NOW - timedelta(minutes=1),
        ),
        VerifiedGrantReference(
            reference_id="vg1.pairing-binding",
            grant_identifier="0198a1b2-c3d4-7400-8000-000000000013",
            grant_type="creator_account_binding",
            grant_digest="5" * 64,
            issuer="pairing-issuer",
            subject="pairing-subject",
            installation_id=INSTALLATION_ID,
            creator_account_id=candidate.creator_account_id,
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
            verified_at=NOW - timedelta(minutes=1),
        ),
    )
    for pairing_grant in pairing_grants:
        store.record_verified_grant(pairing_grant)
    store.register_agent_pairing(
        AgentPairing(
            pairing_id="progress-pairing",
            key_id="progress-agent-key",
            principal_id="progress-principal",
            creator_account_id=candidate.creator_account_id,
            agent_installation_id="progress-agent-installation",
            external_issuer="pairing-issuer",
            external_subject="pairing-subject",
            installation_id=INSTALLATION_ID,
            public_key=b"progress-public-key",
            key_fingerprint="progress-key-fingerprint",
            created_at=NOW - timedelta(minutes=1),
            grant_reference_ids=tuple(
                pairing_grant.reference_id for pairing_grant in pairing_grants
            ),
        )
    )
    if activate_pairing:
        policy = store.build_runtime_policy(
            AuthContext("progress-principal", candidate.creator_account_id, "agent")
        )
        assert store.activate_agent_pairing(policy, "progress-pairing")
    if not preserve_progress:
        with store.database.transaction() as connection:
            connection.execute("DELETE FROM onboarding_progress_outbox")
    return store, key


def _installation_key(store: SQLiteAuthenticationStore) -> InstallationKeyReference:
    key = InstallationKeyReference(
        provider_name="test",
        provider_key_name="test-key",
        algorithm="ES256",
        installation_key_id=KEY_ID,
        installation_key_jkt="A" * 43,
        public_key_jwk='{"crv":"P-256","kid":"ik1.AAAAAAAAAAAAAAAAAAAAAA","kty":"EC","x":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","y":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}',
        created_at=NOW - timedelta(minutes=2),
        activated_at=NOW - timedelta(minutes=1),
    )
    store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name=key.provider_name,
            provider_key_name=key.provider_key_name,
            algorithm=key.algorithm,
            created_at=key.created_at,
        )
    )
    store.activate_installation_key(key)
    return key


def _installation_grant() -> VerifiedGrantReference:
    return VerifiedGrantReference(
        reference_id="vg1.installation",
        grant_identifier="0198a1b2-c3d4-7400-8000-000000000010",
        grant_type="installation_grant",
        grant_digest="1" * 64,
        issuer="issuer",
        subject=f"installation:{INSTALLATION_ID}",
        installation_id=INSTALLATION_ID,
        creator_account_id=None,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        verified_at=NOW,
        organization_id=ORGANIZATION_ID,
        installation_key_id=KEY_ID,
        installation_key_jkt="A" * 43,
    )


def _event(milestone: str, *, suffix: int) -> OnboardingProgressEvent:
    return OnboardingProgressEvent(**_event_kwargs(milestone, suffix))  # type: ignore[arg-type]


def _event_kwargs(milestone: str, suffix: int) -> dict[str, Any]:
    return {
        "milestone": milestone,
        "event_id": f"0198a1b2-c3d4-7400-8000-00000000000{suffix}",
        "occurred_at": NOW,
        "onboarding_transaction_id": "onboarding-fixture-001",
        "organization_id": ORGANIZATION_ID,
        "installation_id": INSTALLATION_ID,
        "correlation_id": f"correlation-fixture-00{suffix}",
    }


def _response(status: int, body: dict[str, object]) -> TransportResponse:
    return TransportResponse(
        status, json.dumps(body, separators=(",", ":")).encode(), "application/json"
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _proof_fields(value: bytes) -> list[bytes]:
    assert value.startswith(_PROOF_DOMAIN)
    fields: list[bytes] = []
    offset = len(_PROOF_DOMAIN)
    for _ in range(9):
        (length,) = struct.unpack_from("!I", value, offset)
        offset += 4
        fields.append(value[offset : offset + length])
        offset += length
    assert offset == len(value)
    return fields


def _outbox_state(
    store: SQLiteAuthenticationStore, event_id: str
) -> tuple[str, int]:
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT state, attempts FROM onboarding_progress_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    assert row is not None
    return str(row["state"]), int(row["attempts"])
