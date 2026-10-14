from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import pytest

import app.security.capability_license_transport as capability_transport
from app.persistence.auth import (
    InstallationKeyReference,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security.capability_license_composition import (
    CapabilityLicenseDeliveryReceipt,
    durable_capability_license_delivery,
)
from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.capability_license_redemption import durable_capability_license_opaque_redemption
from app.security.capability_license_redemption_journal import RedemptionJournalError, SQLiteRedemptionJournal
from app.security.capability_license_verifier import (
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
)
from app.security.grant_types import ACTIVATION_GRANT_TYPES
from app.security.hosted_grants import TransportResponse
from app.security.installation_key import InstallationProof

pytestmark = [pytest.mark.ci_tier('fast')]

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

    first_pending = SQLiteCapabilityLicenseDeliveryJournal(first_store.database, "https://control.example").pending(
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
    assert server.challenge_count == 1
    assert len(server.commit_observed) == 2
    assert server.commit_observed[0][0] == server.commit_observed[1][0]
    assert server.commit_observed[0][1] == server.commit_observed[1][1]
    assert SQLiteCapabilityLicenseDeliveryJournal(restarted_store.database, "https://control.example").pending(
        "activate", EXCHANGE_ID
    ) is None
    assert restarted_store.verified_capability_license(result.reference_id) is not None


@pytest.mark.parametrize("outcome", ["complete", "missing_reference", "invalid_signature", "wrong_seat",
    "metadata_mismatch", "digest_mismatch", "pending_issuance", "complete_before_prepare"])
def test_completed_manual_redemption_reverifies_locally_without_new_issuance(tmp_path, monkeypatch, outcome):
    context = json.loads((VECTORS / "accepted/verification-context.json").read_text("utf-8"))
    token = (VECTORS / "accepted/token.jws").read_text("ascii").strip()
    payload = json.loads((VECTORS / "accepted/payload.json").read_text("utf-8"))
    auth_path = tmp_path / "auth.sqlite3"
    origin = "https://control.example"
    continuation = "clr1." + "A" * 43
    package = _activation_package(context)
    seat_id = str(context["expected_subject"]).split(":seat:", 1)[1].split(":capability:", 1)[0]
    key = _key(context)
    store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    _seed_activation_grants(store, context)
    scope = {"organization_id": context["expected_organization_id"],
        "installation_id": context["expected_installation_id"],
        "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt}
    redemption = SQLiteRedemptionJournal(store.database, origin)
    pending = redemption.prepare(continuation, scope, {"request": {
        "profile": "urn:bridge-clean:capability-license-redemption:v1", "continuation": continuation,
        "redemption_id": EXCHANGE_ID, "organization_id": scope["organization_id"],
        "installation_id": scope["installation_id"]}, "proof": {}}, "original-redemption-key")
    redemption.complete(pending, _json_response(201, {
        "profile": "urn:bridge-clean:capability-license-redemption:v1", "redemption_id": EXCHANGE_ID,
        "result": "accepted", "authorization_state": "delivery_available",
        "delivery": {"operation": "activation", "package": package, "seat_id": seat_id}}))
    redemption.bind_delivery(continuation, scope, "activate", EXCHANGE_ID)
    server = _CommitThenLoseResponse(context=context, token=token, payload=payload)
    server.lose_first_response = False
    monkeypatch.setattr(capability_transport, "_RawCapabilityLicenseHTTPTransport",
        lambda base_url, *, timeout_seconds=10.0: server)

    def delivery(open_store, hosted_origin):
        return durable_capability_license_delivery(open_store, hosted_origin=hosted_origin,
            proof_authority_factory=lambda store: _ProofAuthority(key),
            verifier_factory=lambda: CapabilityLicenseVerifier(FixtureCapabilityLicenseTrustProvider()),
            now=lambda: NOW)

    raced_receipts = []
    if outcome == "complete_before_prepare":
        original_request = server.request
        completion_started = False

        def complete_during_challenge(method, path, **kwargs):
            nonlocal completion_started
            response = original_request(method, path, **kwargs)
            if path.endswith("/capability-license-proof-challenges") and not completion_started:
                completion_started = True
                raced_receipts.append(delivery(lambda: store, origin).activate(package=package, seat_id=seat_id))
            return response

        monkeypatch.setattr(server, "request", complete_during_challenge)
    first = delivery(lambda: store, origin).activate(package=package, seat_id=seat_id)
    assert isinstance(first, CapabilityLicenseDeliveryReceipt)
    if outcome == "complete_before_prepare":
        assert raced_receipts == [first]
    with store.database.read() as connection:
        assert connection.execute("SELECT completed_reference_id FROM capability_license_redemption_journal").fetchone()[0] == first.reference_id
    if outcome == "complete":
        protected_delivery = {"operation": "activation", "package": package, "seat_id": seat_id}
        completed = redemption.completed_delivery("activate", EXCHANGE_ID, scope, protected_delivery)
        assert completed == (first.reference_id, hashlib.sha256(token.encode("ascii")).hexdigest())
        for field in scope:
            with pytest.raises(RedemptionJournalError):
                redemption.completed_delivery("activate", EXCHANGE_ID, {**scope, field: "different"}, protected_delivery)
        for field in protected_delivery:
            with pytest.raises(RedemptionJournalError):
                redemption.completed_delivery("activate", EXCHANGE_ID, scope, {**protected_delivery, field: "different"})
        assert redemption.completed_delivery("finalize", EXCHANGE_ID, scope, protected_delivery) is None
        assert redemption.completed_delivery("activate", "different", scope, protected_delivery) is None
        assert SQLiteRedemptionJournal(store.database, "https://another.example").completed_delivery(
            "activate", EXCHANGE_ID, scope, protected_delivery) is None
    if outcome in {"missing_reference", "invalid_signature", "metadata_mismatch"}:
        with store.database.transaction() as connection:
            if outcome == "missing_reference":
                connection.execute("DELETE FROM capability_license_references")
            elif outcome == "invalid_signature":
                connection.execute("UPDATE capability_license_references SET compact_jws=?", (token + "x",))
            else:
                connection.execute("UPDATE capability_license_references SET object_digest=?", ("0" * 64,))
    elif outcome == "digest_mismatch":
        original_completed = SQLiteRedemptionJournal.completed_delivery

        def wrong_digest(self, *args):
            completed = original_completed(self, *args)
            assert completed is not None
            return completed[0], "0" * 64

        monkeypatch.setattr(SQLiteRedemptionJournal, "completed_delivery", wrong_digest)
    elif outcome == "pending_issuance":
        original_key, original_body = server.commit_observed[0]
        # Counterexample state: a completed opaque result is not evidence about an unbound issuance.
        encoded = json.dumps(original_body, sort_keys=True, separators=(",", ":"))
        with store.database.transaction() as connection:
            connection.execute("INSERT INTO capability_license_pending_deliveries "
                "(operation_type,authority_reference_id,endpoint,idempotency_key,request_body,request_digest,created_at,provider_origin) "
                "VALUES ('activate',?,?,?,?,?,?,?)", (EXCHANGE_ID,
                    f"/v1/capability-license-exchanges/{EXCHANGE_ID}:activate", original_key, encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(), NOW.isoformat(), origin + "/"))

    reopened = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    unexpected_calls = []

    def no_network(*args, **kwargs):
        unexpected_calls.append(True)
        raise AssertionError("Completed delivery must not start new hosted work")

    class NoNetwork:
        request = staticmethod(no_network)

    # A pending original issuance still takes precedence and reuses its exact request.
    if outcome != "pending_issuance":
        monkeypatch.setattr(server, "request", no_network)
    epoch = reopened.build_runtime_policy().authorization_epoch
    if outcome == "wrong_seat":
        result = delivery(lambda: reopened, origin).activate(package=package, seat_id="different-seat")
    else:
        action = durable_capability_license_opaque_redemption(lambda: reopened, hosted_origin=origin,
            proof_authority_factory=lambda store: _ProofAuthority(key),
            transport_factory=lambda *_: NoNetwork(),
            delivery_factory=delivery, now=lambda: NOW)
        result = action.redeem(continuation=continuation)
    if outcome in {"complete", "pending_issuance", "complete_before_prepare"}:
        assert result == first
    elif outcome == "wrong_seat":
        assert result == "durable_store_unavailable"
    else:
        assert result == "capability_license_verification_refused"
    assert not unexpected_calls
    assert server.challenge_count == (2 if outcome == "complete_before_prepare" else 1)
    assert server.issue_count == 1
    assert len(server.commit_observed) == (2 if outcome == "pending_issuance" else 1)
    assert SQLiteCapabilityLicenseDeliveryJournal(reopened.database, origin).pending("activate", EXCHANGE_ID) is None
    assert reopened.build_runtime_policy().authorization_epoch == epoch


class _RecordingDelivery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def activate(self, *, package: str, seat_id: str):
        self.calls.append(("activate", package, seat_id))
        return CapabilityLicenseDeliveryReceipt("caplic.ref", "license-1", "issuance-1")

    def finalize_reissue(self, *, package: str, seat_id: str):
        self.calls.append(("reissue", package, seat_id))
        return CapabilityLicenseDeliveryReceipt("caplic.ref2", "license-2", "issuance-2")


def test_normal_runtime_registers_capability_license_delivery_routes() -> None:
    from app.main import app

    assert str(app.url_path_for("activate_capability_license")) == (
        "/api/v1/capability-license/activate"
    )
    assert str(app.url_path_for("finalize_capability_license_reissue")) == (
        "/api/v1/capability-license/reissue"
    )
