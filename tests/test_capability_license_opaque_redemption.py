from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.api.endpoints.capability_license import CapabilityLicenseContinuationRequest
from app.persistence.auth import InstallationKeyReference, SQLiteAuthenticationStore
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.capability_license_redemption import (
    CapabilityLicenseRedemptionClient,
    CapabilityLicenseRedemptionRefused,
    CapabilityLicenseRedemptionUnavailable,
    durable_capability_license_opaque_redemption,
)
from app.security.hosted_grants import TransportResponse
from app.security.installation_key import InstallationKeyError, InstallationProof

CONTINUATION = "clr1." + "A" * 43
REDEMPTION_ID = "0199a1b2-c3d4-7300-8000-000000000021"
CHALLENGE_ID = "0199a1b2-c3d4-7300-8000-000000000022"
ORGANIZATION_ID = "org.acme"
INSTALLATION_ID = "install.primary"
KEY_ID = "ik1." + "K" * 22
KEY_JKT = "J" * 43
NOW = datetime(2026, 9, 16, 1, 0, tzinfo=timezone.utc)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _key() -> InstallationKeyReference:
    return InstallationKeyReference(
        provider_name="test",
        provider_key_name="key",
        algorithm="ES256",
        installation_key_id=KEY_ID,
        installation_key_jkt=KEY_JKT,
        public_key_jwk="{}",
        created_at=NOW,
        activated_at=NOW,
    )


class ProofAuthority:
    def __init__(self, key: InstallationKeyReference) -> None:
        self.key = key
        self.signed: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        return self.key

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed.append(challenge)
        return InstallationProof(
            self.key.installation_key_id,
            "ES256",
            b"\x00" * 31 + b"\x01" + b"\x00" * 31 + b"\x01",
        )


class UnavailableProofAuthority(ProofAuthority):
    def ensure_ready(self) -> InstallationKeyReference:
        raise InstallationKeyError("installation key unavailable")


class QueueTransport:
    def __init__(self, responses: list[TransportResponse | BaseException]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, object]]] = []
        self.headers: list[Mapping[str, str] | None] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        assert method == "POST"
        self.requests.append((path, json_body))
        self.headers.append(headers)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(status: int, document: Mapping[str, object]) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        body=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )


def _challenge(*, challenge: str | None = None) -> TransportResponse:
    return _response(
        201,
        {
            "profile": "urn:bridge-clean:capability-license-redemption-proof:v1",
            "continuation": CONTINUATION,
            "redemption_id": REDEMPTION_ID,
            "organization_id": ORGANIZATION_ID,
            "installation_id": INSTALLATION_ID,
            "purpose": "capability-license-redeem",
            "proof_challenge_id": CHALLENGE_ID,
            "challenge": challenge or _b64(b"\x02" * 32),
            "audience": (
                "urn:bridge-clean:commercial-control-plane:capability-license-redemption"
            ),
            "issued_at": "2026-09-16T01:00:00.000Z",
            "expires_at": "2026-09-16T01:01:00.000Z",
        },
    )


def _delivery(
    *,
    operation: str = "activation",
    result: str = "accepted",
    status: int = 201,
) -> TransportResponse:
    return _response(
        status,
        {
            "profile": "urn:bridge-clean:capability-license-redemption:v1",
            "redemption_id": REDEMPTION_ID,
            "result": result,
            "authorization_state": "delivery_available",
            "delivery": {
                "operation": operation,
                "package": "QUJD",
                "seat_id": "seat.primary",
            },
        },
    )


def _error(status: int, code: str, detail: str) -> TransportResponse:
    return _response(status, {"code": code, "detail": detail})


def test_browser_contract_accepts_only_opaque_continuation() -> None:
    parsed = CapabilityLicenseContinuationRequest.model_validate(
        {"continuation": CONTINUATION}
    )
    assert parsed.model_dump() == {"continuation": CONTINUATION}
    for field, value in (
        ("package", "QUJD"),
        ("seat_id", "seat.primary"),
        ("organization_id", ORGANIZATION_ID),
        ("installation_id", INSTALLATION_ID),
        ("capability_license", "header.payload.signature"),
        ("signing_key_id", "kid"),
    ):
        with pytest.raises(ValidationError):
            CapabilityLicenseContinuationRequest.model_validate(
                {"continuation": CONTINUATION, field: value}
            )


def test_valid_activation_redemption_requires_local_installation_proof() -> None:
    key = _key()
    proof = ProofAuthority(key)
    transport = QueueTransport([_challenge(), _delivery()])
    result = CapabilityLicenseRedemptionClient(transport, proof).redeem(
        CONTINUATION,
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        key=key,
    )
    assert result.operation == "activation"
    assert result.package == "QUJD"
    assert result.seat_id == "seat.primary"
    assert len(proof.signed) == 1
    assert proof.signed[0].startswith(
        b"BRIDGE-CLEAN-CAPABILITY-LICENSE-REDEMPTION-PROOF-V1\x00"
    )
    challenge_path, challenge_body = transport.requests[0]
    assert challenge_path == "/v1/capability-license-redemption-proof-challenges"
    assert challenge_body == {
        "profile": "urn:bridge-clean:capability-license-redemption-proof:v1",
        "continuation": CONTINUATION,
        "organization_id": ORGANIZATION_ID,
        "installation_id": INSTALLATION_ID,
    }
    redemption_path, redemption_body = transport.requests[1]
    assert redemption_path == "/v1/capability-license-redemptions"
    request = redemption_body["request"]
    assert isinstance(request, dict)
    assert "package" not in request and "seat_id" not in request
    assert set(redemption_body) == {"request", "proof"}
    assert transport.headers[0] is None
    assert transport.headers[1] is not None
    assert set(transport.headers[1] or {}) == {"Idempotency-Key"}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_error(410, "expired", "continuation_expired"), "redemption_expired"),
        (_error(404, "not_found", "continuation_invalid"), "redemption_invalid"),
        (
            _error(401, "unauthorized", "installation_proof_invalid"),
            "redemption_unauthorized",
        ),
        (
            _error(409, "conflict", "installation_binding_mismatch"),
            "redemption_mismatch",
        ),
        (
            _error(409, "conflict", "reissue_authorization_required"),
            "reissue_authorization_required",
        ),
    ],
)
def test_authoritative_hosted_refusals_fail_closed(
    response: TransportResponse,
    expected: str,
) -> None:
    key = _key()
    client = CapabilityLicenseRedemptionClient(
        QueueTransport([response]),
        ProofAuthority(key),
    )
    with pytest.raises(CapabilityLicenseRedemptionRefused) as refused:
        client.redeem(
            CONTINUATION,
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            key=key,
        )
    assert refused.value.result == expected


def test_invalid_reference_never_reaches_hosted() -> None:
    key = _key()
    transport = QueueTransport([])
    with pytest.raises(CapabilityLicenseRedemptionRefused) as refused:
        CapabilityLicenseRedemptionClient(transport, ProofAuthority(key)).redeem(
            "not-a-continuation",
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            key=key,
        )
    assert refused.value.result == "redemption_invalid"
    assert transport.requests == []


def test_ambiguous_commit_reproofs_same_continuation_and_recovers_completed() -> None:
    key = _key()
    proof = ProofAuthority(key)
    transport = QueueTransport(
        [
            _challenge(),
            OSError("response lost after hosted commit"),
            _challenge(),
            _delivery(result="already_completed", status=200),
        ]
    )
    result = CapabilityLicenseRedemptionClient(transport, proof).redeem(
        CONTINUATION,
        organization_id=ORGANIZATION_ID,
        installation_id=INSTALLATION_ID,
        key=key,
    )
    assert result.hosted_result == "already_completed"
    assert len(proof.signed) == 2
    challenge_requests = [body for path, body in transport.requests if path.endswith("challenges")]
    assert len(challenge_requests) == 2
    assert {body["continuation"] for body in challenge_requests} == {CONTINUATION}
    redemption_requests = [body for path, body in transport.requests if path.endswith("redemptions")]
    assert len(redemption_requests) == 2
    assert {
        body["request"]["redemption_id"]
        for body in redemption_requests
        if isinstance(body["request"], dict)
    } == {REDEMPTION_ID}


def test_malformed_hosted_challenge_fails_closed_without_raw_decode_error() -> None:
    key = _key()
    client = CapabilityLicenseRedemptionClient(
        QueueTransport([_challenge(challenge="not/canonical"), _challenge(challenge="not/canonical")]),
        ProofAuthority(key),
    )
    with pytest.raises(CapabilityLicenseRedemptionUnavailable):
        client.redeem(
            CONTINUATION,
            organization_id=ORGANIZATION_ID,
            installation_id=INSTALLATION_ID,
            key=key,
        )


class RecordingDelivery:
    def __init__(self, result: CapabilityLicenseDeliveryReceipt | str) -> None:
        self.result = result
        self.activations: list[tuple[str, str]] = []
        self.reissues: list[tuple[str, str]] = []

    def activate(self, *, package: str, seat_id: str):
        self.activations.append((package, seat_id))
        return self.result

    def finalize_reissue(self, *, package: str, seat_id: str):
        self.reissues.append((package, seat_id))
        return self.result


def _composed_action(
    tmp_path,
    monkeypatch,
    *,
    protected_response: TransportResponse,
    delivery: RecordingDelivery,
    proof_authority: ProofAuthority | None = None,
):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    key = _key()
    proof = proof_authority or ProofAuthority(key)
    monkeypatch.setattr(
        "app.security.capability_license_redemption._current_local_coordinates",
        lambda _store, *, key, instant: (ORGANIZATION_ID, INSTALLATION_ID),
    )
    transport = QueueTransport([_challenge(), protected_response])
    action = durable_capability_license_opaque_redemption(
        lambda: store,
        hosted_origin="https://control-plane.example.invalid",
        transport_factory=lambda _origin, _journal: transport,
        proof_authority_factory=lambda _store: proof,
        delivery_factory=lambda _open_store, _origin: delivery,
        now=lambda: NOW,
    )
    return action, transport


def test_activation_protected_material_stays_inside_brain_and_delegates(
    tmp_path,
    monkeypatch,
) -> None:
    receipt = CapabilityLicenseDeliveryReceipt("ref-1", "license-1", "issuance-1")
    delivery = RecordingDelivery(receipt)
    action, transport = _composed_action(
        tmp_path,
        monkeypatch,
        protected_response=_delivery(),
        delivery=delivery,
    )
    assert action.redeem(continuation=CONTINUATION) == receipt
    assert delivery.activations == [("QUJD", "seat.primary")]
    assert delivery.reissues == []
    assert all("package" not in body for _path, body in transport.requests)


def test_reissue_is_hosted_selected_and_delegates_to_existing_reissue_authority(
    tmp_path,
    monkeypatch,
) -> None:
    receipt = CapabilityLicenseDeliveryReceipt("ref-2", "license-1", "issuance-2")
    delivery = RecordingDelivery(receipt)
    action, _transport = _composed_action(
        tmp_path,
        monkeypatch,
        protected_response=_delivery(operation="reissue"),
        delivery=delivery,
    )
    assert action.redeem(continuation=CONTINUATION) == receipt
    assert delivery.activations == []
    assert delivery.reissues == [("QUJD", "seat.primary")]


def test_delivery_verifier_refusal_is_propagated_without_synthetic_active_state(
    tmp_path,
    monkeypatch,
) -> None:
    delivery = RecordingDelivery("capability_license_verification_refused")
    action, _transport = _composed_action(
        tmp_path,
        monkeypatch,
        protected_response=_delivery(),
        delivery=delivery,
    )
    assert action.redeem(continuation=CONTINUATION) == (
        "capability_license_verification_refused"
    )


def test_continuation_possession_without_local_key_proof_cannot_redeem(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    key = _key()
    proof = UnavailableProofAuthority(key)
    delivery = RecordingDelivery(
        CapabilityLicenseDeliveryReceipt("ref", "license", "issuance")
    )
    action = durable_capability_license_opaque_redemption(
        lambda: store,
        hosted_origin="https://control-plane.example.invalid",
        transport_factory=lambda _origin, _journal: QueueTransport([]),
        proof_authority_factory=lambda _store: proof,
        delivery_factory=lambda _open_store, _origin: delivery,
        now=lambda: NOW,
    )
    assert action.redeem(continuation=CONTINUATION) == "installation_key_unavailable"
    assert delivery.activations == [] and delivery.reissues == []
