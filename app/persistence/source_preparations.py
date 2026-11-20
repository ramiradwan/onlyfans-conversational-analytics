"""Bounded retained prepare evidence; absence is never proof of rollback."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta

from app.persistence.onboarding import OnboardingJourneyUnavailable, require_journey
from app.security.installation_continuation import canonical_request

# Shorter than the published 90-day maximum. Never extend on replay.
RETENTION = timedelta(days=30)
INITIAL = "initial-enrollment"
REGISTERED = "registered-continuation"


class SourcePreparations:
    def __init__(self, authentication):
        self.authentication = authentication

    @staticmethod
    def _validate(row, request):
        from app.security import initial_handoff, installation_continuation
        kind = row["kind"]
        if kind == INITIAL:
            initial_handoff.validate_contract("proof-challenge-request", {
                "profile": initial_handoff.PROOF_PROFILE,
                "target": {"operation": "initial-handoff-prepare", "request": request}})
            destination = request["destination"]
        elif kind == REGISTERED:
            # The existing owner has already validated the selected scope.
            # Retain only its closed structural fields; wire validation belongs
            # to the proof client, including any later replacement request.
            if (set(request) != {"profile", "operation_id", "scope"}
                    or request["profile"] != installation_continuation.PROFILE
                    or not isinstance(request["scope"], dict)
                    or set(request["scope"]) != {"organization_id", "installation_id", "installation_key_id",
                        "source_onboarding_transaction_id", "creator_account_id", "association_request_id"}
                    or not all(isinstance(value, str) and value for value in request["scope"].values())):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            destination = request["scope"]
        else:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if (request["operation_id"] != row["operation_id"]
                or destination["installation_id"] != row["installation_id"]):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return canonical_request(request)

    @staticmethod
    def _same_journey(connection, evidence, row, now):
        return evidence["journey_id"] == row["journey_id"] or connection.execute(
            "SELECT 1 FROM onboarding_workspace_recovery WHERE previous_journey_id=? "
            "AND resolved_journey_id=? AND operation_id=? AND installation_id=? AND expires_at>?",
            (evidence["journey_id"], row["journey_id"], row["operation_id"], row["installation_id"],
            now.isoformat())).fetchone() is not None

    def retain(self, connection, row, request, *, predecessor_operation_id=None, review_predecessor_operation_id=None):
        """Within the owner's transaction before send, or verified adoption."""
        raw = self._validate(row, request)
        if len(raw) > 8192:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        require_journey(row["journey_id"])
        digest = hashlib.sha256(raw).hexdigest()
        existing = connection.execute("SELECT * FROM onboarding_source_preparations WHERE operation_id=?",
            (row["operation_id"],)).fetchone()
        now = self.authentication._now()
        if existing is not None:
            if (existing["kind"] != row["kind"] or existing["installation_id"] != row["installation_id"]
                    or existing["prepare_json"] != raw.decode() or existing["prepare_digest"] != digest
                    or existing["retain_until"] <= now.timestamp()):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            # Only the retained operation's exact recovery may reattach to
            # another local journey. A random local row cannot claim it.
            if not self._same_journey(connection, existing, row, now):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return dict(existing)
        created = datetime.fromisoformat(row["created_at"])
        recovery = connection.execute("SELECT previous_journey_id,retired_at FROM onboarding_workspace_recovery "
            "WHERE resolved_journey_id=? AND operation_id=? AND installation_id=?",
            (row["journey_id"], row["operation_id"], row["installation_id"])).fetchall()
        if len(recovery) > 1:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        origin = row["journey_id"]
        if recovery:
            origin = recovery[0]["previous_journey_id"]
            created = min(created, datetime.fromisoformat(recovery[0]["retired_at"]) - timedelta(minutes=30))
        if created.tzinfo is None or not created <= now < created + RETENTION:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        retain_until = (created + RETENTION).timestamp()
        if predecessor_operation_id is not None and review_predecessor_operation_id is not None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if review_predecessor_operation_id is not None:
            parent = self.get(review_predecessor_operation_id, connection=connection)
            reservation = connection.execute("SELECT * FROM desktop_activation_review_replacements "
                "WHERE source_operation_id=? AND successor_source_operation_id=? AND status='send-claimed'",
                (review_predecessor_operation_id, row["operation_id"])).fetchone()
            if (parent is None or reservation is None or parent["kind"] != REGISTERED or row["kind"] != REGISTERED
                    or parent["installation_id"] != row["installation_id"]
                    or reservation["successor_journey_id"] != row["journey_id"]):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            from app.security.activation_review_replacement import prepare_request
            if raw != canonical_request(prepare_request(json.loads(reservation["request_json"]), successor=True)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            created = datetime.fromtimestamp(parent["created_at"], tz=created.tzinfo)
            retain_until = parent["retain_until"]
        if predecessor_operation_id is not None:
            parent = self.get(predecessor_operation_id, connection=connection)
            reservation = connection.execute("SELECT * FROM onboarding_source_replacement_reservations "
                "WHERE predecessor_operation_id=? AND successor_operation_id=? AND state='send-claimed'",
                (predecessor_operation_id, row["operation_id"])).fetchone()
            if parent is None or reservation is None or parent["kind"] != row["kind"] \
                    or parent["installation_id"] != row["installation_id"]:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            from app.security.source_replacement import original_prepare
            retained_request = json.loads(reservation["request_json"])
            kind = "initial-handoff" if row["kind"] == INITIAL else "registered-continuation"
            if raw != canonical_request(original_prepare(kind, retained_request, successor=True)):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            created = datetime.fromtimestamp(parent["created_at"], tz=created.tzinfo)
            retain_until = parent["retain_until"]
        self.cleanup(connection, now)
        if connection.execute("SELECT count(*) FROM onboarding_source_preparations").fetchone()[0] >= 128:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        connection.execute("INSERT INTO onboarding_source_preparations VALUES (?,?,?,?,?,?,?,?)",
            (row["operation_id"], origin, row["kind"], row["installation_id"], raw.decode(), digest,
            created.timestamp(), retain_until))
        return dict(connection.execute("SELECT * FROM onboarding_source_preparations WHERE operation_id=?",
            (row["operation_id"],)).fetchone())

    def get(self, operation_id, *, connection=None):
        with (self.authentication.database.read() if connection is None else nullcontext(connection)) as current:
            row = current.execute("SELECT * FROM onboarding_source_preparations WHERE operation_id=? AND retain_until>?",
                (operation_id, self.authentication._now().timestamp())).fetchone()
        if row is None:
            return None
        value = dict(row)
        raw = value["prepare_json"].encode()
        if hashlib.sha256(raw).hexdigest() != value["prepare_digest"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return value

    def record_prepared(self, connection, row, *, expires_at, continuation_id=None):
        """Record owner-verified prepare coordinates, never its entry locator."""
        evidence = self.get(row["operation_id"], connection=connection)
        if evidence is None:
            # Legacy missing evidence is not a license to reconstruct it.
            return
        if (evidence["kind"] != row["kind"] or evidence["installation_id"] != row["installation_id"]
                or not self._same_journey(connection, evidence, row, self.authentication._now())):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if (row["kind"] == INITIAL and continuation_id is not None
                or row["kind"] == REGISTERED and (not isinstance(continuation_id, str) or not continuation_id)):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        existing = connection.execute("SELECT * FROM onboarding_source_prepared_receipts WHERE operation_id=?",
            (row["operation_id"],)).fetchone()
        if existing is not None:
            if (existing["provider_expires_at"] != deadline.timestamp()
                    or existing["continuation_id"] != continuation_id):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            return
        connection.execute("INSERT INTO onboarding_source_prepared_receipts VALUES (?,?,?)",
            (row["operation_id"], deadline.timestamp(), continuation_id))

    def classify(self, journey_id, *, connection):
        """Local evidence only; missing/uncertain records are not fresh sources."""
        require_journey(journey_id)
        row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (journey_id,)).fetchone()
        context = connection.execute("SELECT * FROM installation_continuation_contexts WHERE journey_id=?",
            (journey_id,)).fetchone()
        if row is None:
            history = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements "
                "WHERE journey_id=? AND expires_at>?", (journey_id, self.authentication._now().timestamp())).fetchone()
            if history is not None:
                document = json.loads(history["evidence_json"])
                row, context = document.get("journey"), document.get("context")
        if row is None or row["journey_id"] != journey_id:
            return "indeterminate"
        preparation = None if row["operation_id"] is None else self.get(row["operation_id"], connection=connection)
        if preparation is not None:
            if (preparation["journey_id"] != journey_id or preparation["kind"] != row["kind"]
                    or preparation["installation_id"] != row["installation_id"]):
                return "indeterminate"
            return "retained-prepare"
        if row["state"] != "new" or connection.execute(
                "SELECT 1 FROM onboarding_source_preparations WHERE journey_id=?", (journey_id,)).fetchone():
            return "indeterminate"
        if row["kind"] == INITIAL and row["operation_id"] is None and row["scope_json"] is None:
            return "never-prepared"
        if (row["kind"] == REGISTERED and context is not None and context["continuation_id"] is None
                and context["provider_expires_at"] is None and context["provider_state"] is None):
            return "never-prepared"
        return "indeterminate"

    @staticmethod
    def cleanup(connection, now):
        connection.execute("DELETE FROM onboarding_source_preparations WHERE retain_until<=?", (now.timestamp(),))
