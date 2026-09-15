from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

from app.persistence.auth import (
    ClaimSubmission,
    ClaimSubmissionState,
    InstallationKeyReference,
    VerifiedGrantReference,
)
from app.provisioning.claim_package import (
    CLAIM_PACKAGE_PROFILE_V1,
    CLAIM_PACKAGE_PROFILE_V2,
    ClaimPackageError,
    decode_claim_package,
)
from app.security.hosted_grants import (
    BOOTSTRAP_RECOVERY_PROFILE,
    CLAIM_PROFILE_V2,
    BootstrapRecoveryRefused,
    HostedGrantClient,
    HostedGrantUnavailable,
    TransportResponse,
)
from app.security.installation_key import InstallationProof

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "contracts" / "bootstrap-recovery-v2"
NOW = datetime(2026, 9, 13, 8, 10, tzinfo=timezone.utc)


def _b64(document: Mapping[str, object]) -> str:
    raw = json.dumps(document, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _v2_package() -> str:
    claim_id = "0198a1b2-c3d4-7300-8000-000000000001"
    return _b64(
        {
            "profile": CLAIM_PACKAGE_PROFILE_V2,
            "claim_profile": CLAIM_PROFILE_V2,
            "claim_id": claim_id,
            "claim_secret": "A" * 43,
            "challenge": "B" * 42 + "A",
            "onboarding_transaction_id": "onboarding.001",
            "organization_id": "org.acme",
            "installation_id": claim_id,
            "consume_path": f"/v1/installation-claims/{claim_id}:consume",
        }
    )


def test_new_production_claim_package_requires_v2_and_persists_profile_coordinate() -> None:
    decoded = decode_claim_package(_v2_package(), production=True)
    assert decoded.durable_state()["claim_profile"] == CLAIM_PROFILE_V2
    assert decoded.release_claim().claim_profile == CLAIM_PROFILE_V2

    v1 = _b64(
        {
            "profile": CLAIM_PACKAGE_PROFILE_V1,
            "claim_id": "0198a1b2-c3d4-7300-8000-000000000001",
            "claim_secret": "A" * 43,
            "challenge": "B" * 42 + "A",
            "onboarding_transaction_id": "onboarding.001",
            "organization_id": "org.acme",
            "installation_id": "0198a1b2-c3d4-7300-8000-000000000001",
            "consume_path": "/v1/installation-claims/0198a1b2-c3d4-7300-8000-000000000001:consume",
        }
    )
    with pytest.raises(ClaimPackageError, match="Installation claim package is invalid"):
        decode_claim_package(v1, production=True)
    assert decode_claim_package(v1).durable_state().get("claim_profile") is None


class FakeProofAuthority:
    def __init__(self, key: InstallationKeyReference) -> None:
        self.key = key

    def ensure_ready(self) -> InstallationKeyReference:
        return self.key

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        return InstallationProof(self.key.installation_key_id, "ES256", b"\x01" * 64)


class FakeStore:
    def __init__(self, submission: ClaimSubmission | None) -> None:
        self.submission = submission
        self.recorded: list[VerifiedGrantReference] = []

    def claim_submission(self, claim_id: str) -> ClaimSubmission | None:
        return self.submission if self.submission and self.submission.claim_id == claim_id else None

    def record_verified_grants(self, grants: tuple[VerifiedGrantReference, ...]) -> None:
        self.recorded.extend(grants)

    def resolve_claim_submission(self, claim_id: str, *, outcome: str | None, resolved_at: datetime, enrolled_at: datetime | None = None) -> bool:
        assert self.submission is not None and claim_id == self.submission.claim_id
        self.submission = replace(
            self.submission,
            state=ClaimSubmissionState.CONSUMED if outcome is None else ClaimSubmissionState.REFUSED,
            outcome=outcome,
            resolved_at=resolved_at,
            enrolled_at=enrolled_at or self.submission.enrolled_at,
        )
        return True


class VectorTransport:
    def __init__(self, key: InstallationKeyReference, final: TransportResponse) -> None:
        self.key = key
        self.final = final
        self.calls = 0

    def request(self, method: str, path: str, *, json_body: Mapping[str, object]) -> TransportResponse:
        self.calls += 1
        if self.calls == 1:
            return _response(
                201,
                {
                    "profile": "urn:bridge-clean:provisioning-proof:v1",
                    "purpose": "bootstrap-recovery",
                    "installation_id": self.key.installation_key_id.replace("ik1.", "install.") if False else "0198a1b2-c3d4-7300-8000-000000000001",
                    "challenge": "A" * 43,
                    "audience": "urn:bridge-clean:commercial-control-plane:provisioning-v2",
                    "issued_at": "2026-09-13T08:09:00.000Z",
                    "expires_at": "2026-09-13T08:10:00.000Z",
                },
            )
        return self.final


def _response(status: int, body: Mapping[str, object]) -> TransportResponse:
    return TransportResponse(status, json.dumps(body, separators=(",", ":")).encode(), "application/json")


def _key() -> InstallationKeyReference:
    return InstallationKeyReference(
        provider_name="test",
        provider_key_name="key",
        algorithm="ES256",
        installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
        installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        public_key_jwk="{}",
        created_at=NOW - timedelta(days=1),
        activated_at=NOW - timedelta(days=1),
    )


def _submission(*, consumed: bool, enrolled_at: datetime | None) -> ClaimSubmission:
    return ClaimSubmission(
        claim_id="0198a1b2-c3d4-7300-8000-000000000001",
        onboarding_transaction_id="onboarding.001",
        organization_id="org.acme",
        installation_id="0198a1b2-c3d4-7300-8000-000000000001",
        submitted_at=NOW - timedelta(minutes=20),
        claim_profile=CLAIM_PROFILE_V2,
        enrolled_at=enrolled_at,
        state=ClaimSubmissionState.CONSUMED if consumed else ClaimSubmissionState.SUBMITTED,
        resolved_at=(NOW - timedelta(minutes=19)) if consumed else None,
    )


def _fake_references() -> tuple[VerifiedGrantReference, ...]:
    return tuple(
        VerifiedGrantReference(
            reference_id=f"{kind}-recovered",
            grant_identifier=f"{kind}-jti",
            grant_type=kind,
            grant_digest=("a" if kind == "installation_grant" else "b") * 64,
            issuer="issuer",
            subject="subject",
            installation_id="0198a1b2-c3d4-7300-8000-000000000001",
            creator_account_id=None,
            valid_from=NOW,
            expires_at=NOW + timedelta(hours=1),
            verified_at=NOW,
            organization_id="org.acme",
            installation_key_id="ik1.CCCCCCCCCCCCCCCCCCCCCC",
            installation_key_jkt="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        for kind in ("installation_grant", "membership_snapshot")
    )


@pytest.mark.parametrize(
    "filename",
    [
        "bootstrap-recovery-v2-positive.json",
        "bootstrap-recovery-v2-wrong-installation.json",
        "bootstrap-recovery-v2-wrong-key.json",
        "bootstrap-recovery-v2-unknown-enrollment.json",
        "bootstrap-recovery-v2-proof-replay.json",
        "bootstrap-recovery-v2-lost-consume-response.json",
        "bootstrap-recovery-v2-temporarily-unavailable.json",
    ],
)
def test_bootstrap_recovery_matches_all_published_v2_outcome_classes(filename: str, monkeypatch) -> None:
    case = json.loads((VECTORS / filename).read_text("utf-8"))[0]
    key = _key()
    if filename == "bootstrap-recovery-v2-unknown-enrollment.json":
        store = FakeStore(None)
        client = HostedGrantClient(VectorTransport(key, _response(500, {})), FakeProofAuthority(key), store, trust_set={"keys": []})  # type: ignore[arg-type]
        with pytest.raises(BootstrapRecoveryRefused, match="Bootstrap recovery was refused") as exc:
            client.recover_bootstrap_v2(claim_id=case["request"]["request"]["claim_id"], recovery_request_id=case["request"]["request"]["recovery_request_id"])
        assert exc.value.result == case["expected_result"]
        return

    lost = filename == "bootstrap-recovery-v2-lost-consume-response.json"
    enrolled = None if lost else datetime(2026, 9, 13, 7, 51, tzinfo=timezone.utc)
    store = FakeStore(_submission(consumed=not lost, enrolled_at=enrolled))
    final = _response(case["expected_http_status"], case.get("response") or {
        "profile": "urn:bridge-clean:error:v1",
        "request_id": "0198a1b2-c3d4-7300-8000-000000000099",
        "code": case.get("expected_error_code", "invalid_request"),
        "detail": "refused",
    })
    client = HostedGrantClient(VectorTransport(key, final), FakeProofAuthority(key), store, trust_set={"keys": []})  # type: ignore[arg-type]
    monkeypatch.setattr(client, "_verify_bundle", lambda *args, **kwargs: _fake_references())
    request = case.get("request", {}).get("request", {})
    recovery_id = request.get("recovery_request_id", "0198a1b2-c3d4-7300-8000-000000000020")

    if case["expected_http_status"] == 200:
        recovered = client.recover_bootstrap_v2(claim_id=store.submission.claim_id, recovery_request_id=recovery_id)
        assert len(recovered.grant_reference_ids) == 2
        assert store.submission is not None and store.submission.state is ClaimSubmissionState.CONSUMED
        assert recovered.enrolled_at == datetime(2026, 9, 13, 7, 51, tzinfo=timezone.utc)
    elif case["expected_http_status"] == 503:
        with pytest.raises(HostedGrantUnavailable):
            client.recover_bootstrap_v2(claim_id=store.submission.claim_id, recovery_request_id=recovery_id)
    else:
        with pytest.raises(BootstrapRecoveryRefused) as exc:
            client.recover_bootstrap_v2(claim_id=store.submission.claim_id, recovery_request_id=recovery_id)
        assert exc.value.result == case["expected_result"]
