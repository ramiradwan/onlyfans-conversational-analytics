"""Durable non-authorizing reservations for exact expired source replacement.

The domain owner must authorize against its retained source and exact current
scope inside the same transaction. Reservation is not a source or permission
to send. Claiming the send is irreversible; reconciliation must use that
same identity rather than claim or create another one.
"""
from __future__ import annotations

import json

from app.persistence.auth import _new_uuid7
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.persistence.source_preparations import SourcePreparations, INITIAL, REGISTERED
from app.security.installation_continuation import canonical_request
from app.security.initial_handoff import valid_identifier
from app.security.source_replacement import (
    INITIAL as INITIAL_PROFILE, REGISTERED as REGISTERED_PROFILE, replacement_request,
)


class SourceReplacementReservations:
    def __init__(self, authentication):
        self.authentication = authentication
        self.preparations = SourcePreparations(authentication)

    def _candidate(self, connection, operation_id, expected_digest, creator_account_id, *, authorize):
        authorize(connection=connection)
        evidence = self.preparations.get(operation_id, connection=connection)
        receipt = connection.execute("SELECT * FROM onboarding_source_prepared_receipts WHERE operation_id=?",
            (operation_id,)).fetchone()
        now = self.authentication._now()
        if (evidence is None or receipt is None or evidence["prepare_digest"] != expected_digest
                or receipt["provider_expires_at"] > now.timestamp()
                or not valid_identifier(creator_account_id)
                or self.preparations.classify(evidence["journey_id"], connection=connection) != "retained-prepare"):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        # Do not reserve against a completed or revoked original, even if its
        # expiry/receipt still remains. Authority and original outcome must be
        # rechecked by the caller, including its Provider-specific proof reads.
        if connection.execute("SELECT 1 FROM onboarding_journeys WHERE operation_id=? "
                "AND state IN ('completed','revoked')", (operation_id,)).fetchone():
            raise OnboardingJourneyUnavailable("journey_unavailable")
        for row in connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements "
                "WHERE journey_id=? AND expires_at>?", (evidence["journey_id"], now.timestamp())):
            retired = json.loads(row["evidence_json"]).get("journey") or {}
            if retired.get("state") in {"completed", "revoked"}:
                raise OnboardingJourneyUnavailable("journey_unavailable")
        request = json.loads(evidence["prepare_json"])
        key = self.authentication.installation_key_reference(connection=connection)
        if key is None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if evidence["kind"] == INITIAL:
            kind = INITIAL_PROFILE
            original_key = request["destination"]["installation_key"]
            public = json.loads(key.public_key_jwk)
            if (original_key["kid"] != key.installation_key_id
                    or original_key["jwk"] != {name: public[name] for name in ("crv", "kty", "x", "y")}):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        elif evidence["kind"] == REGISTERED:
            kind = REGISTERED_PROFILE
            scope = request["scope"]
            if (scope["creator_account_id"] != creator_account_id
                    or scope["installation_key_id"] != key.installation_key_id):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        else:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return evidence, receipt, kind, request

    def reserve(self, connection, *, operation_id, expected_digest, creator_account_id, authorize):
        evidence, receipt, kind, original = self._candidate(connection, operation_id, expected_digest,
            creator_account_id, authorize=authorize)
        existing = connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
            "WHERE predecessor_operation_id=?", (operation_id,)).fetchone()
        successor = existing["successor_operation_id"] if existing is not None else _new_uuid7(self.authentication._now())
        request = replacement_request(kind, original, successor, continuation_id=receipt["continuation_id"])
        raw = canonical_request(request).decode()
        if existing is not None:
            if existing["creator_account_id"] != creator_account_id or existing["request_json"] != raw:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return dict(existing)
        connection.execute("INSERT INTO onboarding_source_replacement_reservations VALUES (?,?,?,?,?,'reserved')",
            (operation_id, successor, creator_account_id, raw, self.authentication._now().timestamp()))
        authorize(connection=connection)
        return dict(connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
            "WHERE predecessor_operation_id=?", (operation_id,)).fetchone())

    def claim_send(self, connection, *, operation_id, expected_digest, creator_account_id, authorize):
        evidence, receipt, kind, original = self._candidate(connection, operation_id, expected_digest,
            creator_account_id, authorize=authorize)
        row = connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
            "WHERE predecessor_operation_id=?", (operation_id,)).fetchone()
        if row is None or row["state"] != "reserved" or row["creator_account_id"] != creator_account_id:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        request = replacement_request(kind, original, row["successor_operation_id"],
            continuation_id=receipt["continuation_id"])
        if canonical_request(request).decode() != row["request_json"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        connection.execute("UPDATE onboarding_source_replacement_reservations SET state='send-claimed' "
            "WHERE predecessor_operation_id=? AND state='reserved'", (operation_id,))
        authorize(connection=connection)
        return request
