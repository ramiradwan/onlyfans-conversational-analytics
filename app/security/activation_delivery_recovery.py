"""Resume an accepted activation through Brain-owned durable delivery records."""
from __future__ import annotations

import json

from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.capability_license_redemption_journal import (
    RedemptionJournalError, SQLiteRedemptionJournal, canonical, digest, provider_origin,
)
from app.security.delivery_recovery_contract import (
    MAX_GENERATION, PROFILE, PROOF_PROFILE, RecoveryRefused, RecoveryUnavailable, timestamp, validate,
)


class ActivationDeliveryJournal:
    def __init__(self, database, origin):
        self.database, self.origin = database, provider_origin(origin)

    def reserve(self, operation_id, binding):
        validate("proof-challenge-request", {"profile": PROOF_PROFILE,
            "purpose": "capability-license-delivery-read", "request": {"profile": PROFILE,
                "activation_operation_id": operation_id, "delivery_generation": None, "binding": binding}})
        with self.database.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO capability_license_activation_deliveries "
                "(activation_operation_id,provider_origin,binding_json) VALUES (?,?,?)",
                (operation_id, self.origin, canonical(binding)))
        return self.require(operation_id, binding)

    def require(self, operation_id, binding):
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM capability_license_activation_deliveries "
                "WHERE activation_operation_id=?", (operation_id,)).fetchone()
        if row is None or row["provider_origin"] != self.origin or row["binding_json"] != canonical(binding):
            raise RedemptionJournalError("Original activation delivery record is unavailable")
        value = dict(row)
        for field in ("recover_request_json", "accepted_result_json", "redemption_json"):
            value[field] = None if value[field] is None else json.loads(value[field])
        if value["recover_request_json"] is not None:
            request = validate("recover-request", value["recover_request_json"])["request"]
            if (request["binding"] != binding or request["activation_operation_id"] != operation_id
                    or request["delivery_generation"] != value["requested_generation"]):
                raise RedemptionJournalError("Recovery journal scope changed")
        return value

    def activation_committed(self, operation_id, binding):
        # Only the activation owner calls resume after its authoritative result.
        self.require(operation_id, binding)
        with self.database.transaction() as connection:
            connection.execute("UPDATE capability_license_activation_deliveries SET activation_committed=1 "
                "WHERE activation_operation_id=? AND provider_origin=? AND binding_json=?",
                (operation_id, self.origin, canonical(binding)))

    def handoff(self, operation_id, binding, generation, redemption, *, accepted=None):
        validate("read-response", {"profile": PROFILE, "activation_operation_id": operation_id,
            "binding": binding, "requested_generation": None, "current_generation": generation,
            "state": "authorization-available", "redemption": redemption})
        row = self.require(operation_id, binding)
        if generation < row["current_generation"]:
            raise RedemptionJournalError("Delivery generation moved backwards")
        old = row["redemption_json"]
        if generation == row["current_generation"] and old is not None and old != redemption:
            raise RedemptionJournalError("Accepted delivery authorization changed")
        if accepted is None and generation == row["current_generation"]:
            accepted = row["accepted_result_json"]
        if accepted is not None:
            validate("recover-response", accepted)
            if (accepted["activation_operation_id"] != operation_id or accepted["binding"] != binding
                    or accepted["delivery_generation"] != generation or accepted["redemption"] != redemption):
                raise RedemptionJournalError("Accepted recovery scope changed")
            if generation == row["current_generation"] and row["accepted_result_json"] not in (None, accepted):
                raise RedemptionJournalError("Accepted recovery result changed")
        with self.database.transaction() as connection:
            if old is not None and old != redemption:
                prior = connection.execute("SELECT response_json FROM capability_license_redemption_journal "
                    "WHERE provider_origin=? AND continuation_digest=?", (self.origin, digest(old["continuation"]))).fetchone()
                if prior is not None and prior["response_json"] is not None:
                    raise RedemptionJournalError("Completed redemption cannot be superseded")
                connection.execute("DELETE FROM capability_license_redemption_journal "
                    "WHERE provider_origin=? AND continuation_digest=? AND response_json IS NULL",
                    (self.origin, digest(old["continuation"])))
            changed = connection.execute("UPDATE capability_license_activation_deliveries "
                "SET current_generation=?,redemption_json=?,accepted_result_json=?,requested_generation=NULL,"
                "recover_request_json=NULL,retry_at=NULL,failure_count=0 WHERE activation_operation_id=? "
                "AND current_generation=? AND binding_json=? AND provider_origin=?",
                (generation, canonical(redemption), canonical(accepted) if accepted else None,
                    operation_id, row["current_generation"], canonical(binding), self.origin)).rowcount
            if changed != 1:
                raise RedemptionJournalError("Delivery journal changed concurrently")

    def request(self, operation_id, binding, envelope):
        request = validate("recover-request", envelope)["request"]
        row = self.require(operation_id, binding)
        if request["activation_operation_id"] != operation_id or request["binding"] != binding:
            raise RedemptionJournalError("Recovery request scope changed")
        if row["requested_generation"] is not None and row["requested_generation"] != request["delivery_generation"]:
            raise RedemptionJournalError("Unresolved recovery generation changed")
        with self.database.transaction() as connection:
            changed = connection.execute("UPDATE capability_license_activation_deliveries SET requested_generation=?,"
                "recover_request_json=?,retry_at=NULL WHERE activation_operation_id=? AND current_generation=? "
                "AND recover_request_json IS ?",
                (request["delivery_generation"], canonical(envelope), operation_id, row["current_generation"],
                    None if row["recover_request_json"] is None else canonical(row["recover_request_json"]))).rowcount
            if changed != 1:
                raise RedemptionJournalError("Recovery request changed concurrently")

    def reconciled(self, operation_id, binding, current):
        row = self.require(operation_id, binding)
        if current < row["current_generation"]:
            raise RedemptionJournalError("Delivery high-water mark moved backwards")
        with self.database.transaction() as connection:
            connection.execute("UPDATE capability_license_activation_deliveries SET current_generation=?,"
                "requested_generation=NULL,recover_request_json=NULL WHERE activation_operation_id=?",
                (current, operation_id))

    def defer(self, operation_id, binding, now, retry_at=None):
        row = self.require(operation_id, binding)
        failures = min(6, row["failure_count"] + 1)
        deadline = max(now + min(60, max(5, 2 ** failures)), retry_at or 0)
        with self.database.transaction() as connection:
            connection.execute("UPDATE capability_license_activation_deliveries SET retry_at=?,failure_count=? "
                "WHERE activation_operation_id=?", (deadline, failures, operation_id))

    def completed(self, operation_id, reference_id):
        with self.database.transaction() as connection:
            connection.execute("UPDATE capability_license_activation_deliveries SET completed_reference_id=?,"
                "retry_at=NULL,failure_count=0 WHERE activation_operation_id=?", (reference_id, operation_id))


class ActivationDeliveryRecovery:
    def __init__(self, journal, client, redemption, *, now):
        self.journal, self.client, self.redemption, self.now = journal, client, redemption, now
        self.redemption_journal = SQLiteRedemptionJournal(journal.database, journal.origin)

    def resume(self, operation_id, binding, initial_redemption=None):
        try:
            row = self.journal.require(operation_id, binding)
            self.journal.activation_committed(operation_id, binding)
            if row["completed_reference_id"] is not None:
                # Readiness must still be reconstructed from verified local authority.
                return "redemption_conflict"
            if row["retry_at"] is not None and self.now().timestamp() < row["retry_at"]:
                return "hosted_unavailable"
            if (initial_redemption is not None and row["redemption_json"] is None
                    and row["current_generation"] == 0 and row["requested_generation"] is None):
                self.journal.handoff(operation_id, binding, 0, initial_redemption)
                row = self.journal.require(operation_id, binding)
            outcome = self._redeliver(operation_id, binding, row)
            if outcome is not None:
                return outcome
            if row["recover_request_json"] is not None:
                try:
                    return self._accept(operation_id, binding, self.client.send("recover", row["recover_request_json"]))
                except RecoveryRefused as refusal:
                    if refusal.reason in {"rate_limited", "temporarily_unavailable", "authority_unavailable"}:
                        raise
                exact = self.client.read(operation_id, binding, row["requested_generation"])
                if exact["state"] in {"redemption-completed", "unavailable"}:
                    raise RecoveryUnavailable("Committed delivery cannot be recovered without its local journal")
                if exact["state"] in {"authorization-available", "authorization-expired"}:
                    # An accepted same-generation command needs fresh proof if a different
                    # envelope won the race. Its immutable accepted result remains unchanged.
                    return self._recover(operation_id, binding, row["requested_generation"])
                if exact["state"] not in {"not-issued", "superseded"}:
                    raise RecoveryUnavailable("Recovery outcome is unresolved")
            current = self.client.read(operation_id, binding)
            state, generation = current["state"], current["current_generation"]
            if state == "authorization-available":
                self.journal.handoff(operation_id, binding, generation, current["redemption"])
                return self._redeliver(operation_id, binding, self.journal.require(operation_id, binding)) or "hosted_unavailable"
            if state != "authorization-expired":
                raise RecoveryUnavailable("Delivery authority is unavailable")
            retry_at = timestamp(current["retry_at"]).timestamp()
            if self.now().timestamp() < retry_at:
                self.journal.defer(operation_id, binding, self.now().timestamp(), retry_at)
                return "hosted_unavailable"
            requested = row["requested_generation"]
            if generation >= MAX_GENERATION or requested is not None and requested > generation + 1:
                raise RecoveryUnavailable("Recovery generation is unavailable")
            self.journal.reconciled(operation_id, binding, generation)
            return self._recover(operation_id, binding, generation + 1)
        except RecoveryUnavailable as error:
            try:
                self.journal.defer(operation_id, binding, self.now().timestamp(),
                    error.retry_at if isinstance(error, RecoveryRefused) else None)
            except (RedemptionJournalError, ValueError, TypeError, KeyError):
                return "durable_store_unavailable"
            return "hosted_unavailable"
        except (RedemptionJournalError, ValueError, TypeError, KeyError):
            return "durable_store_unavailable"

    def _recover(self, operation_id, binding, generation):
        envelope = self.client.prepare("recover", operation_id, binding, generation)
        self.journal.request(operation_id, binding, envelope)
        return self._accept(operation_id, binding, self.client.send("recover", envelope))

    def _accept(self, operation_id, binding, accepted):
        self.journal.handoff(operation_id, binding, accepted["delivery_generation"], accepted["redemption"], accepted=accepted)
        return self._redeliver(operation_id, binding, self.journal.require(operation_id, binding)) or "hosted_unavailable"

    def _redeliver(self, operation_id, binding, row):
        handoff = row["redemption_json"]
        if handoff is None:
            return None
        scope = {key: value for key, value in binding.items() if key != "creator_account_id"}
        pending = self.redemption_journal.pending(handoff["continuation"], scope)
        if pending is None and timestamp(handoff["expires_at"]) <= self.now():
            return None
        outcome = self.redemption.redeem(continuation=handoff["continuation"], expected_scope=(
            binding["organization_id"], binding["installation_id"], binding["installation_key_id"], binding["installation_key_jkt"]))
        if isinstance(outcome, CapabilityLicenseDeliveryReceipt):
            self.journal.completed(operation_id, outcome.reference_id)
            return outcome
        if pending is not None and pending.response is not None or outcome not in {
                "redemption_expired", "redemption_invalid", "redemption_unauthorized", "redemption_conflict"}:
            self.journal.defer(operation_id, binding, self.now().timestamp())
            return outcome
        return None
