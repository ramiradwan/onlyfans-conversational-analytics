from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
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
        first_store.database,
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

    # Process-local state is discarded. The restarted Brain generates a new
    # challenge/proof and a new provisional key, but the durable transport must
    # ignore both and replay the exact pending commit.
    restarted_store = SQLiteAuthenticationStore(auth_path, clock=lambda: NOW)
    restarted_journal = SQLiteCapabilityLicenseDeliveryJournal(
        restarted_store.database,
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
