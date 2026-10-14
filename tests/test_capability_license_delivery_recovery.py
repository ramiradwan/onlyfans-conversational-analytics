from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence import sqlite_api
from app.security.activation_delivery_recovery import ActivationDeliveryJournal
from app.security.capability_license_redemption_journal import SQLiteRedemptionJournal
from app.security.capability_license_delivery_journal import (
    SQLiteCapabilityLicenseDeliveryJournal,
)
from app.security.capability_license_transport import DurableCapabilityLicenseTransport
from app.security.capability_license_verifier import (
    CapabilityLicenseAuthorityService,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
)
from app.security.hosted_grants import TransportResponse

pytestmark = [pytest.mark.ci_tier('fast')]

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _context() -> CapabilityLicenseVerificationContext:
    data = json.loads(
        (VECTORS / "accepted" / "verification-context.json").read_text("utf-8")
    )
    subject = data["expected_subject"]
    return CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_seat_id=subject.split(":seat:", 1)[1].split(":capability:", 1)[0],
        expected_seat_scope=data["expected_seat_scope"],
        expected_capability=data["expected_capability"],
        expected_target_major=data["expected_target_major"],
        expected_artifact_family=data["expected_artifact_family"],
        requested_update_mode=data["requested_update_mode"],
        expected_fallback_major=data.get("expected_fallback_major"),
    )


def _authority(store: SQLiteAuthenticationStore) -> CapabilityLicenseAuthorityService:
    verifier = CapabilityLicenseVerifier(FixtureCapabilityLicenseTrustProvider())
    return CapabilityLicenseAuthorityService(
        store,
        verifier,
        clock=lambda: NOW,
        verification_source="conformance",
    )


class CommitThenLoseResponse:
    """Control-plane stand-in whose committed idempotency state survives Brain restart."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.issue_count = 0
        self.committed: dict[str, tuple[dict[str, object], TransportResponse]] = {}
        self.observed: list[tuple[str, dict[str, object]]] = []
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
        key = (headers or {}).get("Idempotency-Key")
        assert isinstance(key, str) and key
        body = copy.deepcopy(dict(json_body))
        self.observed.append((key, body))

        committed = self.committed.get(key)
        if committed is None:
            self.issue_count += 1
            response = TransportResponse(
                201,
                json.dumps(
                    {"capability_license": self.token},
                    separators=(",", ":"),
                ).encode("utf-8"),
                "application/json",
            )
            self.committed[key] = (body, response)
            if self.lose_first_response:
                self.lose_first_response = False
                raise SystemExit("Brain exited after server commit")
            return response

        committed_body, response = committed
        assert body == committed_body
        return response


@pytest.mark.parametrize(
    ("operation", "path", "reference_field", "authority_reference"),
    [
        (
            "activate",
            "/v1/capability-license-exchanges/exchange-1:activate",
            "exchange_id",
            "exchange-1",
        ),
        (
            "finalize",
            "/v1/capability-license-reissues/reissue-1:finalize",
            "reissue_authorization_id",
            "reissue-1",
        ),
    ],
)
def test_committed_delivery_recovers_exact_request_after_brain_restart(
    tmp_path: Path,
    operation: str,
    path: str,
    reference_field: str,
    authority_reference: str,
) -> None:
    auth_path = tmp_path / "auth.sqlite3"
    token = (VECTORS / "accepted" / "token.jws").read_text("ascii").strip()
    server = CommitThenLoseResponse(token)

    first_store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    first_journal = SQLiteCapabilityLicenseDeliveryJournal(
        first_store.database, "https://control.example",
        clock=lambda: NOW,
    )
    first_transport = DurableCapabilityLicenseTransport(server, first_journal)
    first_body = {
        "request": {reference_field: authority_reference},
        "proof": {"challenge": "first-challenge", "signature": "first-signature"},
    }

    with pytest.raises(SystemExit, match="server commit"):
        first_transport.request(
            "POST",
            path,
            json_body=first_body,
            headers={"Idempotency-Key": "0198a1b2-c3d4-7300-8000-0000000000aa"},
        )

    pending = first_journal.pending(operation, authority_reference)  # type: ignore[arg-type]
    assert pending is not None
    assert pending.request_body == first_body
    assert pending.response_object_digest is None
    assert server.issue_count == 1

    # The delivery client already checks the journal before requesting a proof.
    # The transport independently fences a caller supplying a different request.
    restarted_store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    restarted_journal = SQLiteCapabilityLicenseDeliveryJournal(
        restarted_store.database, "https://control.example",
        clock=lambda: NOW,
    )
    restarted_transport = DurableCapabilityLicenseTransport(server, restarted_journal)
    second_body = {
        "request": {reference_field: authority_reference},
        "proof": {"challenge": "second-challenge", "signature": "second-signature"},
    }
    response = restarted_transport.request(
        "POST",
        path,
        json_body=second_body,
        headers={"Idempotency-Key": "0198a1b2-c3d4-7300-8000-0000000000bb"},
    )

    assert response.status_code == 201
    assert server.issue_count == 1
    assert [item[0] for item in server.observed] == [
        "0198a1b2-c3d4-7300-8000-0000000000aa",
        "0198a1b2-c3d4-7300-8000-0000000000aa",
    ]
    assert server.observed[1][1] == first_body

    bound = restarted_journal.pending(operation, authority_reference)  # type: ignore[arg-type]
    assert bound is not None
    assert bound.response_object_digest is not None

    # Real signature verification and durable authority persistence trigger
    # journal cleanup in the same SQLite transaction as the authority INSERT.
    authority = _authority(restarted_store)
    reference = authority.verify(token, context=_context())
    authority.persist(reference)

    assert restarted_journal.pending(
        operation, authority_reference  # type: ignore[arg-type]
    ) is None
    assert restarted_store.verified_capability_license(reference.reference_id) == reference


@pytest.mark.parametrize("already_verified", [False, True])
def test_verified_license_atomically_completes_redemption_and_activation(tmp_path, already_verified):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    origin = "https://control.example"
    context = _context()
    token = (VECTORS / "accepted/token.jws").read_text("ascii").strip()
    authority = _authority(store)
    reference = authority.verify(token, context=context)
    if already_verified:
        authority.persist(reference)
    epoch = store.build_runtime_policy().authorization_epoch
    scope = {"organization_id": context.expected_organization_id,
        "installation_id": context.expected_installation_id,
        "installation_key_id": context.expected_installation_key_id,
        "installation_key_jkt": context.expected_installation_key_jkt}
    binding = {**scope, "creator_account_id": "creator.fixture"}
    operation = "019a00b0-0000-7000-8000-000000000034"
    continuation = "clr1." + "A" * 43
    activation = ActivationDeliveryJournal(store.database, origin)
    activation.reserve(operation, binding)
    activation.handoff(operation, binding, 0, {
        "profile": "urn:bridge-clean:capability-license-redemption-continuation:v1",
        "continuation": continuation, "expires_at": "2026-09-13T12:10:00.000Z"})
    redemption = SQLiteRedemptionJournal(store.database, origin)
    request = {"request": {"continuation": continuation}, "proof": {}}
    opaque = redemption.prepare(continuation, scope, request, "original-redemption-key")
    redemption.complete(opaque, TransportResponse(201, b'{"fixture":"protected-package"}', "application/json"))
    redemption.bind_delivery(continuation, scope, "activate", "exchange.fixture")
    delivery = SQLiteCapabilityLicenseDeliveryJournal(store.database, origin)
    pending = delivery.prepare("activate", "exchange.fixture", "/fixture:activate", "original-issuance-key",
        {"request": {"organization_id": scope["organization_id"], "installation_id": scope["installation_id"]},
            "proof": {"key_id": scope["installation_key_id"]}})
    delivery.bind_committed_response(pending, token)
    delivery.prepare("activate", "exchange.unresolved", "/unresolved:activate", "unresolved-issuance-key",
        {"request": {}, "proof": {}})

    # A failed authority transaction must not settle either delivery record.
    with store.database.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_qualification_completion AFTER UPDATE ON capability_license_redemption_journal "
            "BEGIN SELECT RAISE(ABORT, 'fixture interrupted commit'); END")
    with pytest.raises(sqlite_api.IntegrityError, match="fixture interrupted commit"):
        authority.persist(reference)
    assert store.verified_capability_license(reference.reference_id) == (reference if already_verified else None)
    assert store.build_runtime_policy().authorization_epoch == epoch
    assert activation.require(operation, binding)["completed_reference_id"] is None
    assert delivery.pending("activate", "exchange.fixture") is not None
    with store.database.transaction() as connection:
        assert connection.execute("SELECT completed_reference_id FROM capability_license_redemption_journal").fetchone()[0] is None
        connection.execute("DROP TRIGGER fail_qualification_completion")

    authority.persist(reference)
    reopened = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    assert reopened.verified_capability_license(reference.reference_id) == reference
    if already_verified:
        assert reopened.build_runtime_policy().authorization_epoch == epoch
    else:
        assert reopened.build_runtime_policy().authorization_epoch > epoch
    assert ActivationDeliveryJournal(reopened.database, origin).require(operation, binding)["completed_reference_id"] == reference.reference_id
    assert SQLiteCapabilityLicenseDeliveryJournal(reopened.database, origin).pending("activate", "exchange.fixture") is None
    assert SQLiteCapabilityLicenseDeliveryJournal(reopened.database, origin).pending("activate", "exchange.unresolved") is not None
    with reopened.database.read() as connection:
        assert connection.execute("SELECT completed_reference_id FROM capability_license_redemption_journal").fetchone()[0] == reference.reference_id
