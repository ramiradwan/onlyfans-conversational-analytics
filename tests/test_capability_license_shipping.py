from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest
from fastapi.testclient import TestClient

import app.security.capability_license_transport as capability_transport
from app.packaged_entry import (
    PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE,
    PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE,
    select_brain_application,
)
from app.persistence.auth import (
    InstallationKeyReference,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.provisioning.app import (
    PROVISIONING_CAPABILITY_LICENSE_ACTIVATE_PATH,
    PROVISIONING_CAPABILITY_LICENSE_REISSUE_PATH,
)
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)
from app.security.capability_license_composition import (
    CapabilityLicenseDeliveryReceipt,
    durable_capability_license_delivery,
)
from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.capability_license_verifier import (
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
)
from app.security.grant_types import ACTIVATION_GRANT_TYPES
from app.security.hosted_grants import TransportResponse
from app.security.installation_key import InstallationProof

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
EXCHANGE_ID = "0198a1b2-c3d4-7300-8000-000000000002"
HANDOFF_TOKEN = "t" * 32


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _activation_package(context: Mapping[str, object]) -> str:
    installation_id = str(context["expected_installation_id"])
    document = {
        "profile": "urn:bridge-clean:capability-license-activation-package:v1",
        "exchange_id": EXCHANGE_ID,
        "organization_id": str(context["expected_organization_id"]),
        "installation_id": installation_id,
        "capability": "analysis-run",
        "authority_target": "capability-license",
        "proof_profile": "urn:bridge-clean:capability-license-activation-proof:v1",
        "proof_purpose": "capability-license-activate",
        "proof_challenge_path": (
            f"/v1/installations/{installation_id}/capability-license-proof-challenges"
        ),
        "activation_path": f"/v1/capability-license-exchanges/{EXCHANGE_ID}:activate",
    }
    return _b64(json.dumps(document, separators=(",", ":")).encode("utf-8"))


def _key(context: Mapping[str, object]) -> InstallationKeyReference:
    return InstallationKeyReference(
        provider_name="test",
        provider_key_name="key",
        algorithm="ES256",
        installation_key_id=str(context["expected_installation_key_id"]),
        installation_key_jkt=str(context["expected_installation_key_jkt"]),
        public_key_jwk="{}",
        created_at=NOW - timedelta(hours=1),
        activated_at=NOW - timedelta(minutes=50),
    )


class _ProofAuthority:
    def __init__(self, key: InstallationKeyReference) -> None:
        self.key = key

    def ensure_ready(self) -> InstallationKeyReference:
        return self.key

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        assert challenge
        return InstallationProof(
            self.key.installation_key_id,
            "ES256",
            b"\x00" * 31 + b"\x01" + b"\x00" * 31 + b"\x01",
        )


def _seed_activation_grants(
    store: SQLiteAuthenticationStore,
    context: Mapping[str, object],
) -> None:
    grants: list[VerifiedGrantReference] = []
    for grant_type in ACTIVATION_GRANT_TYPES:
        reference_id = f"{grant_type}-current"
        membership = grant_type == "membership_snapshot"
        grants.append(
            VerifiedGrantReference(
                reference_id=reference_id,
                grant_identifier=f"jti-{reference_id}",
                grant_type=grant_type,
                grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
                issuer="urn:bridge-clean:commercial-control-plane",
                subject="principal-1",
                installation_id=str(context["expected_installation_id"]),
                creator_account_id=None,
                valid_from=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
                verified_at=NOW - timedelta(minutes=5),
                organization_id=str(context["expected_organization_id"]),
                installation_key_id=str(context["expected_installation_key_id"]),
                installation_key_jkt=str(context["expected_installation_key_jkt"]),
                membership_id="membership-1" if membership else None,
                allowed_creator_account_ids=("creator-1",) if membership else None,
                membership_roles=("owner",) if membership else None,
            )
        )
    store.record_verified_grants(tuple(grants))


class _CommitThenLoseResponse:
    """Raw hosted transport whose committed idempotency state survives restart."""

    def __init__(
        self,
        *,
        context: Mapping[str, object],
        token: str,
        payload: Mapping[str, object],
    ) -> None:
        self.context = context
        self.token = token
        self.payload = payload
        self.issue_count = 0
        self.challenge_count = 0
        self.committed: dict[str, tuple[dict[str, object], TransportResponse]] = {}
        self.commit_observed: list[tuple[str, dict[str, object]]] = []
        self.lose_first_response = True

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        assert method == "POST"
        if path.endswith("/capability-license-proof-challenges"):
            self.challenge_count += 1
            request = dict(json_body)
            return _json_response(
                201,
                {
                    "profile": "urn:bridge-clean:capability-license-activation-proof:v1",
                    "purpose": request["purpose"],
                    "organization_id": request["organization_id"],
                    "authority_reference_id": request["authority_reference_id"],
                    "installation_id": str(self.context["expected_installation_id"]),
                    "proof_challenge_id": (
                        f"0198a1b2-c3d4-7300-8000-00000000000{self.challenge_count}"
                    ),
                    "challenge": (
                        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                        if self.challenge_count == 1
                        else "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
                    ),
                    "audience": (
                        "urn:bridge-clean:commercial-control-plane:"
                        "capability-license-activation"
                    ),
                    "issued_at": "2026-09-13T08:03:00.000Z",
                    "expires_at": "2026-09-13T08:04:00.000Z",
                },
            )

        assert path.endswith(":activate")
        key = (headers or {}).get("Idempotency-Key")
        assert isinstance(key, str) and key
        body = copy.deepcopy(dict(json_body))
        self.commit_observed.append((key, body))
        committed = self.committed.get(key)
        if committed is None:
            self.issue_count += 1
            response = _json_response(
                201,
                {
                    "profile": "urn:bridge-clean:capability-license-activation:v1",
                    "exchange_id": EXCHANGE_ID,
                    "activation_id": "0198a1b2-c3d4-7300-8000-000000000003",
                    "organization_id": self.context["expected_organization_id"],
                    "installation_id": self.context["expected_installation_id"],
                    "installation_key_id": self.context["expected_installation_key_id"],
                    "installation_key_jkt": self.context["expected_installation_key_jkt"],
                    "seat_id": (
                        str(self.context["expected_subject"])
                        .split(":seat:", 1)[1]
                        .split(":capability:", 1)[0]
                    ),
                    "seat_scope": self.context["expected_seat_scope"],
                    "capability": self.context["expected_capability"],
                    "licensed_major_version": self.context["expected_target_major"],
                    "license_id": self.payload["license_id"],
                    "issuance_id": self.payload["issuance_id"],
                    "issuance_state": "current",
                    "activated_at": "2026-09-13T08:04:00.000Z",
                    "capability_license": self.token,
                },
            )
            self.committed[key] = (body, response)
            if self.lose_first_response:
                self.lose_first_response = False
                raise SystemExit("Brain exited after server commit")
            return response
        committed_body, response = committed
        assert body == committed_body
        return response

    def close(self) -> None:
        return None


def _json_response(status: int, value: Mapping[str, object]) -> TransportResponse:
    return TransportResponse(
        status,
        json.dumps(value, separators=(",", ":")).encode("utf-8"),
        "application/json",
    )


def test_shipping_delivery_action_recovers_committed_activation_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = json.loads(
        (VECTORS / "accepted" / "verification-context.json").read_text("utf-8")
    )
    token = (VECTORS / "accepted" / "token.jws").read_text("ascii").strip()
    payload = json.loads((VECTORS / "accepted" / "payload.json").read_text("utf-8"))
    auth_path = tmp_path / "auth.sqlite3"
    package = _activation_package(context)
    seat_id = (
        str(context["expected_subject"])
        .split(":seat:", 1)[1]
        .split(":capability:", 1)[0]
    )
    key = _key(context)
    server = _CommitThenLoseResponse(context=context, token=token, payload=payload)
    monkeypatch.setattr(
        capability_transport,
        "_RawCapabilityLicenseHTTPTransport",
        lambda base_url, *, timeout_seconds=10.0: server,
    )

    first_store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    _seed_activation_grants(first_store, context)
    first_action = durable_capability_license_delivery(
        lambda: first_store,
        hosted_origin="https://control.example",
        proof_authority_factory=lambda store: _ProofAuthority(key),
        verifier_factory=lambda: CapabilityLicenseVerifier(
            FixtureCapabilityLicenseTrustProvider()
        ),
        now=lambda: NOW,
    )

    with pytest.raises(SystemExit, match="server commit"):
        first_action.activate(package=package, seat_id=seat_id)

    first_pending = SQLiteCapabilityLicenseDeliveryJournal(first_store.database).pending(
        "activate", EXCHANGE_ID
    )
    assert first_pending is not None
    assert server.issue_count == 1

    # Process-local action/store/client state is discarded. A new shipping action
    # opens the same auth DB and must recover with the original request and key.
    restarted_store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    restarted_action = durable_capability_license_delivery(
        lambda: restarted_store,
        hosted_origin="https://control.example",
        proof_authority_factory=lambda store: _ProofAuthority(key),
        verifier_factory=lambda: CapabilityLicenseVerifier(
            FixtureCapabilityLicenseTrustProvider()
        ),
        now=lambda: NOW,
    )
    result = restarted_action.activate(package=package, seat_id=seat_id)

    assert isinstance(result, CapabilityLicenseDeliveryReceipt)
    assert server.issue_count == 1
    assert len(server.commit_observed) == 2
    assert server.commit_observed[0][0] == server.commit_observed[1][0]
    assert server.commit_observed[0][1] == server.commit_observed[1][1]
    assert SQLiteCapabilityLicenseDeliveryJournal(restarted_store.database).pending(
        "activate", EXCHANGE_ID
    ) is None
    assert restarted_store.verified_capability_license(result.reference_id) is not None


class _RecordingDelivery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def activate(self, *, package: str, seat_id: str):
        self.calls.append(("activate", package, seat_id))
        return CapabilityLicenseDeliveryReceipt("caplic.ref", "license-1", "issuance-1")

    def finalize_reissue(self, *, package: str, seat_id: str):
        self.calls.append(("reissue", package, seat_id))
        return CapabilityLicenseDeliveryReceipt("caplic.ref2", "license-2", "issuance-2")


def _provisioning_session(application) -> tuple[TestClient, dict[str, str], str]:
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
    shell = client.get("/provisioning", headers=cookie)
    csrf = shell.text.split('data-provisioning-csrf="')[1].split('"')[0]
    return client, cookie, csrf


def test_packaged_entry_exposes_activation_and_reissue_delivery_actions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.security.capability_license_composition as composition

    recorder = _RecordingDelivery()
    monkeypatch.setattr(
        composition,
        "durable_capability_license_delivery",
        lambda open_store, *, hosted_origin: recorder,
    )
    monkeypatch.setenv(PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, HANDOFF_TOKEN)
    monkeypatch.setenv(
        PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE,
        "https://control.example",
    )

    application = select_brain_application(tmp_path)
    client, cookie, csrf = _provisioning_session(application)
    headers = {
        **cookie,
        "Origin": PROVISIONING_ORIGIN,
        PROVISIONING_CSRF_HEADER: csrf,
    }

    activation = client.post(
        PROVISIONING_CAPABILITY_LICENSE_ACTIVATE_PATH,
        json={"package": "activation-package", "seat_id": "seat.analysis.001"},
        headers=headers,
    )
    reissue = client.post(
        PROVISIONING_CAPABILITY_LICENSE_REISSUE_PATH,
        json={"package": "reissue-package", "seat_id": "seat.analysis.001"},
        headers=headers,
    )

    assert activation.status_code == 200
    assert activation.json()["state"] == "capability_license_active"
    assert reissue.status_code == 200
    assert reissue.json()["state"] == "capability_license_active"
    assert recorder.calls == [
        ("activate", "activation-package", "seat.analysis.001"),
        ("reissue", "reissue-package", "seat.analysis.001"),
    ]


def test_normal_runtime_registers_capability_license_delivery_routes() -> None:
    from app.main import app

    assert str(app.url_path_for("activate_capability_license")) == (
        "/api/v1/capability-license/activate"
    )
    assert str(app.url_path_for("finalize_capability_license_reissue")) == (
        "/api/v1/capability-license/reissue"
    )
