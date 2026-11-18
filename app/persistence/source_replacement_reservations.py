"""Durable non-authorizing reservations for exact expired source replacement.

The domain owner must authorize against its retained source and exact current
scope inside the same transaction. Reservation is not a source or permission
to send. Claiming the send is irreversible; reconciliation must use that
same identity rather than claim or create another one.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.persistence.auth import _new_uuid7
from app.persistence.onboarding import OnboardingJourneyUnavailable, require_journey
from app.persistence.source_preparations import SourcePreparations, INITIAL, REGISTERED
from app.security.installation_continuation import canonical_request
from app.security.initial_handoff import valid_identifier
from app.security.source_replacement import (
    INITIAL as INITIAL_PROFILE, REGISTERED as REGISTERED_PROFILE, replacement_request,
    SourceReplacementClient, SourceReplacementRefused, original_prepare,
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
        if connection.execute("SELECT 1 FROM desktop_activation_review_replacements WHERE source_operation_id=?",
                (operation_id,)).fetchone():
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

    def describe(self, connection, *, journey_id, creator_account_id, authorize):
        """Read exact eligible predecessor coordinates without reserving a successor."""
        rows = connection.execute("SELECT operation_id,prepare_digest FROM onboarding_source_preparations "
            "WHERE journey_id=? AND retain_until>? LIMIT 2",
            (journey_id, self.authentication._now().timestamp())).fetchall()
        if len(rows) != 1:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        row = rows[0]
        evidence, _, kind, original = self._candidate(connection, row["operation_id"], row["prepare_digest"],
            creator_account_id, authorize=authorize)
        self._terminal(connection, evidence)
        # A legacy local record with invalid wire coordinates must not offer
        # the published replacement path.
        if kind == REGISTERED_PROFILE:
            from app.security.installation_continuation import _contract, PROOF_PROFILE
            _contract().validate("proof-challenge-request", {"profile": PROOF_PROFILE,
                "target": {"operation": "installation-continuation-prepare", "request": original}})
        return {"operation_id": evidence["operation_id"], "expected_digest": evidence["prepare_digest"],
                "installation_id": evidence["installation_id"], "kind": evidence["kind"]}

    def claim_reconciliation(self, connection, *, operation_id, expected_digest, creator_account_id, authorize):
        _, _, _, _, request = self._claimed(connection, operation_id, expected_digest, creator_account_id, authorize)
        if connection.execute("SELECT 1 FROM onboarding_source_replacement_reconciliations WHERE predecessor_operation_id=?",
                (operation_id,)).fetchone():
            return None
        connection.execute("INSERT INTO onboarding_source_replacement_reconciliations VALUES (?,?)",
            (operation_id, self.authentication._now().timestamp()))
        authorize(connection=connection)
        return request

    def require_claimed(self, connection, *, operation_id, expected_digest, creator_account_id, authorize):
        return self._claimed(connection, operation_id, expected_digest, creator_account_id, authorize)[4]

    @staticmethod
    def initial_identity(connection, operation_id):
        identity = connection.execute("SELECT * FROM onboarding_initial_replacement_identities "
            "WHERE successor_operation_id=?", (operation_id,)).fetchone()
        predecessor = connection.execute("SELECT r.predecessor_operation_id,r.creator_account_id "
            "FROM onboarding_source_replacement_reservations r JOIN onboarding_source_replacement_outcomes o "
            "ON r.predecessor_operation_id=o.predecessor_operation_id WHERE r.successor_operation_id=? "
            "AND o.status='prepared'", (operation_id,)).fetchone()
        if predecessor is None:
            if identity is not None:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return None
        # Older adopted successors without a retained identity cannot safely
        # reconstruct whether the Provider required the opaque customer.
        if (identity is None or identity["predecessor_operation_id"] != predecessor["predecessor_operation_id"]
                or identity["intended_creator_id"] != predecessor["creator_account_id"]):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return dict(identity)

    def outcome(self, connection, operation_id):
        row = connection.execute("SELECT * FROM onboarding_source_replacement_outcomes "
            "WHERE predecessor_operation_id=?", (operation_id,)).fetchone()
        return None if row is None else dict(row)

    def _terminal(self, connection, evidence):
        identity = evidence["journey_id"]
        row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (identity,)).fetchone()
        detail = connection.execute("SELECT * FROM installation_continuation_contexts WHERE journey_id=?", (identity,)).fetchone()
        if row is None:
            retained = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements "
                "WHERE journey_id=? AND expires_at>?", (identity, self.authentication._now().timestamp())).fetchone()
            if retained is not None:
                document = json.loads(retained["evidence_json"])
                row, detail = document.get("journey"), document.get("context")
        if (row is None or row["kind"] != evidence["kind"] or row["installation_id"] != evidence["installation_id"]
                or row["operation_id"] != evidence["operation_id"] or row["state"] != "expired"
                or detail is not None and detail["provider_state"] == "revoked"):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return row, detail

    def _claimed(self, connection, operation_id, expected_digest, creator_account_id, authorize):
        evidence, receipt, kind, original = self._candidate(connection, operation_id, expected_digest,
            creator_account_id, authorize=authorize)
        row, detail = self._terminal(connection, evidence)
        reservation = connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
            "WHERE predecessor_operation_id=?", (operation_id,)).fetchone()
        if (reservation is None or reservation["state"] != "send-claimed"
                or reservation["creator_account_id"] != creator_account_id
                or self.outcome(connection, operation_id) is not None):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        request = replacement_request(kind, original, reservation["successor_operation_id"],
            continuation_id=receipt["continuation_id"])
        if canonical_request(request).decode() != reservation["request_json"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return evidence, row, detail, kind, request

    def record_result(self, connection, *, operation_id, expected_digest, creator_account_id,
                      successor_journey_id, result, authorize):
        """Only a validated prepared response can create the next local source."""
        evidence, prior, detail, kind, request = self._claimed(connection, operation_id,
            expected_digest, creator_account_id, authorize)
        client = SourceReplacementClient(kind, None, None, clock=self.authentication._now)
        result = client.validate_result(request, result, intended_creator_id=creator_account_id)
        status = result["status"]
        if status != "prepared":
            domain_id = result["accepted_operation_id" if status == "accepted" else "completion_operation_id"]
            # Do not materialize a successor, synthesize scope or mark enrollment complete.
            authorize(connection=connection)
            connection.execute("INSERT INTO onboarding_source_replacement_outcomes VALUES (?,?,NULL,?)",
                (operation_id, status, domain_id))
            return self.outcome(connection, operation_id)
        require_journey(successor_journey_id)
        if successor_journey_id == evidence["journey_id"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        successor = result["successor"]
        now = self.authentication._now()
        expiry = datetime.fromisoformat(successor["expires_at"].replace("Z", "+00:00"))
        if expiry.timestamp() > evidence["retain_until"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        prepared = original_prepare(kind, request, successor=True)
        if kind == INITIAL_PROFILE:
            previous_identity = self.initial_identity(connection, operation_id)
            if previous_identity is not None and (previous_identity["intended_creator_id"] != creator_account_id
                    or previous_identity["expected_customer_id"] is not None
                    and previous_identity["expected_customer_id"] != result.get("expected_customer_id")):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        if kind == REGISTERED_PROFILE:
            from app.persistence.installation_continuation import InstallationContinuationStore, _scope, _generation, _json
            contexts = InstallationContinuationStore(self.authentication)
            target = contexts.selection.select_in_transaction(connection, association_request_id=prepared["scope"]["association_request_id"])
            if (detail is None or detail["selection_json"] != _generation(target)
                    or detail["scope_json"] != _json(_scope(target))
                    or prepared["scope"] != _scope(target) or target.binding_revoked_at is not None):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            contexts.selection.require_current(connection, target)
        else:
            from app.persistence.onboarding import OnboardingJourneyStore
            OnboardingJourneyStore(self.authentication)._require_initial_native_candidate(connection)
        authorize(connection=connection)
        connection.execute("INSERT INTO onboarding_journeys (journey_id,kind,created_at,expires_at,operation_id,"
            "installation_id,state,handoff_reference,handoff_expires_at,prepare_json) VALUES (?,?,?,?,?,?,'waiting',?,?,?)",
            (successor_journey_id, evidence["kind"], now.isoformat(), expiry.isoformat(), successor["operation_id"],
                evidence["installation_id"], successor["reference"] if kind == INITIAL_PROFILE else None,
                successor["expires_at"] if kind == INITIAL_PROFILE else None,
                canonical_request(prepared).decode() if kind == INITIAL_PROFILE else None))
        row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (successor_journey_id,)).fetchone()
        self.preparations.retain(connection, row, prepared, predecessor_operation_id=operation_id)
        self.preparations.record_prepared(connection, row, expires_at=successor["expires_at"],
            continuation_id=successor.get("continuation_id"))
        if kind == REGISTERED_PROFILE:
            connection.execute("INSERT INTO installation_continuation_contexts (journey_id,operation_profile,"
                "association_request_id,selection_json,scope_json,continuation_id,reference,provider_expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (successor_journey_id, prepared["profile"], target.candidate.association_request_id,
                    _generation(target), _json(_scope(target)), successor["continuation_id"], successor["reference"],
                    successor["expires_at"]))
            contexts.selection.require_current(connection, target)
        if kind == INITIAL_PROFILE:
            connection.execute("INSERT INTO onboarding_initial_replacement_identities VALUES (?,?,?,?)",
                (successor["operation_id"], operation_id, creator_account_id, result.get("expected_customer_id")))
        connection.execute("INSERT INTO onboarding_source_replacement_outcomes VALUES (?,'prepared',?,NULL)",
            (operation_id, successor_journey_id))
        return self.outcome(connection, operation_id)

    def record_commercial_refusal(self, connection, *, operation_id, expected_digest, creator_account_id,
                                  refusal, authorize):
        _, _, _, kind, _ = self._claimed(connection, operation_id, expected_digest, creator_account_id, authorize)
        if (kind != REGISTERED_PROFILE or not isinstance(refusal, SourceReplacementRefused)
                or refusal.reason != "commercial_reconciliation_required"):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        authorize(connection=connection)
        connection.execute("INSERT INTO onboarding_source_replacement_outcomes "
            "VALUES (?,'commercial-reconciliation-required',NULL,NULL)", (operation_id,))
        return self.outcome(connection, operation_id)
