"""Durable read budget and terminal evidence for the original activation."""
from __future__ import annotations

import hashlib
import json

from app.persistence.installation_continuation import InstallationContinuationStore, _scope
from app.persistence.source_preparations import SourcePreparations, REGISTERED
from app.security.activation_reconciliation import exact_request, validate_result
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.capability_license_redemption_journal import provider_origin
from app.security.installation_continuation import canonical_request
from app.security.initial_handoff import timestamp


MAX_ATTEMPTS = 3
BACKOFF = (15, 30, 60)


class ActivationReconciliationJournal:
    def __init__(self, store, *, origin=None):
        self.store = store
        self.origin = provider_origin(origin) if origin is not None else None
        self.preparations = SourcePreparations(store)
        self.contexts = InstallationContinuationStore(store)

    def request(self, connection, operation, *, allow_replaced=False):
        """Derive only from the admitted original action and its immutable ceremony."""
        saved = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        source = connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        ceremony = connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        if (saved is None or saved["owner_scope_json"] != operation["owner_scope_json"]
                or saved["journey_id"] != operation["journey_id"] or saved["action"] != "activation.review"
                or (saved["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    and not (allow_replaced and saved["state"] == "refused" and saved["reason"] == "operation_expired"
                        and connection.execute("SELECT 1 FROM desktop_activation_review_replacements WHERE original_operation_id=?",
                            (operation["operation_id"],)).fetchone()))
                or source is None or ceremony is None):
            raise HostedGrantUnavailable("Original activation is unavailable")
        selected = json.loads(saved["owner_scope_json"])
        binding = json.loads(ceremony["binding_json"])
        original = binding.get("source", {})
        if (binding.get("purpose") != "activate-complimentary" or original.get("kind") != "registered"
                or binding.get("activation_operation_id") != source["activation_operation_id"]):
            raise HostedGrantUnavailable("Original activation changed")
        evidence = self.preparations.get(original["operation_id"], connection=connection)
        receipt = connection.execute("SELECT * FROM onboarding_source_prepared_receipts WHERE operation_id=?",
            (original["operation_id"],)).fetchone()
        if (evidence is None or receipt is None or evidence["kind"] != REGISTERED
                or evidence["journey_id"] != source["source_journey_id"]
                or receipt["continuation_id"] != original.get("continuation_id")
                or evidence["prepare_digest"] != original.get("request_digest")
                or timestamp(binding["source_expires_at"]).timestamp() != receipt["provider_expires_at"]):
            raise HostedGrantUnavailable("Original activation source is unavailable")
        request = exact_request(json.loads(evidence["prepare_json"]), continuation_id=receipt["continuation_id"],
            activation_operation_id=source["activation_operation_id"])
        scope = request["scope"]
        if (evidence["prepare_digest"] != request["source_prepare_digest"]
                or original.get("profile") != json.loads(evidence["prepare_json"])["profile"]
                or any(scope[name] != selected.get(name) for name in
                    ("organization_id", "installation_id", "creator_account_id", "association_request_id"))
                or binding.get("intended_creator_id") != scope["creator_account_id"]
                or binding.get("installation_id") != scope["installation_id"]
                or binding.get("organization_id") != scope["organization_id"]):
            raise HostedGrantUnavailable("Original activation scope changed")
        key = self.store.installation_key_reference(connection=connection)
        if (key is None or selected.get("key") != {"installation_key_id": key.installation_key_id,
                "installation_key_jkt": key.installation_key_jkt}
                or scope["installation_key_id"] != key.installation_key_id
                or binding.get("installation_key_id") != key.installation_key_id
                or binding.get("installation_key_jkt") != key.installation_key_jkt):
            raise HostedGrantUnavailable("Original activation key changed")
        target = self.contexts.selection.select_in_transaction(connection,
            association_request_id=scope["association_request_id"])
        if not target.finalized or target.binding_revoked_at is not None or _scope(target) != scope:
            raise HostedGrantUnavailable("Original activation account changed")
        self.contexts.selection.require_current(connection, target)
        # The existing delivery reservation must already match this very action.
        delivery = connection.execute("SELECT * FROM capability_license_activation_deliveries WHERE activation_operation_id=?",
            (source["activation_operation_id"],)).fetchone()
        expected = {"organization_id": scope["organization_id"], "installation_id": scope["installation_id"],
            "creator_account_id": scope["creator_account_id"], "installation_key_id": key.installation_key_id,
            "installation_key_jkt": key.installation_key_jkt}
        if (delivery is None or self.origin is None or delivery["provider_origin"] != self.origin
                or json.loads(delivery["binding_json"]) != expected):
            raise HostedGrantUnavailable("Original activation delivery is unavailable")
        return request, receipt["provider_expires_at"]

    def row(self, connection, operation_id):
        row = connection.execute("SELECT * FROM desktop_activation_reconciliations WHERE operation_id=?",
            (operation_id,)).fetchone()
        return None if row is None else dict(row)

    def _require(self, connection, operation, expected, *, authorize, allow_replaced=False):
        authorize()
        request, deadline = self.request(connection, operation, allow_replaced=allow_replaced)
        if request != expected or self.store._now().timestamp() < deadline:
            raise HostedGrantUnavailable("Original activation is not eligible for reconciliation")
        return hashlib.sha256(canonical_request(request)).hexdigest()

    def exact_row(self, connection, operation, expected, *, authorize, allow_replaced=False):
        digest = self._require(connection, operation, expected, authorize=authorize, allow_replaced=allow_replaced)
        row = self.row(connection, operation["operation_id"])
        if row is not None and (row["request_digest"] != digest or row["source_operation_id"] != expected["source_operation_id"]
                or row["activation_operation_id"] != expected["activation_operation_id"]):
            raise HostedGrantUnavailable("Original activation reconciliation changed")
        return row, digest

    def claim(self, connection, operation, expected, *, authorize):
        row, digest = self.exact_row(connection, operation, expected, authorize=authorize)
        operation_id = operation["operation_id"]
        now = self.store._now().timestamp()
        if row is not None and (row["status"] is not None or row["attempts"] >= MAX_ATTEMPTS or now < row["retry_at"]):
            return None
        attempt = 1 if row is None else row["attempts"] + 1
        if row is None:
            connection.execute("INSERT INTO desktop_activation_reconciliations VALUES (?,?,?,?,?,?,NULL,NULL)",
                (operation_id, expected["source_operation_id"], expected["activation_operation_id"],
                    digest, attempt, now + BACKOFF[attempt-1]))
        else:
            connection.execute("UPDATE desktop_activation_reconciliations SET attempts=?,retry_at=? WHERE operation_id=?",
                (attempt, now + BACKOFF[attempt-1], operation_id))
        authorize()
        return attempt

    def guard(self, connection, operation, expected, attempt, *, authorize):
        digest = self._require(connection, operation, expected, authorize=authorize)
        row = self.row(connection, operation["operation_id"])
        if row is None or row["request_digest"] != digest or row["attempts"] != attempt or row["status"] is not None:
            raise HostedGrantUnavailable("Original activation reconciliation changed")

    def record(self, connection, operation, expected, attempt, result, *, authorize):
        self.guard(connection, operation, expected, attempt, authorize=authorize)
        status = validate_result(expected, result)["status"]
        if status != "unresolved":
            connection.execute("UPDATE desktop_activation_reconciliations SET status=?,observed_at=? WHERE operation_id=?",
                (status, self.store._now().timestamp(), operation["operation_id"]))
        authorize()
        return status

    def refuse(self, connection, operation, expected, attempt, *, authorize):
        self.guard(connection, operation, expected, attempt, authorize=authorize)
        connection.execute("UPDATE desktop_activation_reconciliations SET status='refused',observed_at=? WHERE operation_id=?",
            (self.store._now().timestamp(), operation["operation_id"]))
        authorize()
