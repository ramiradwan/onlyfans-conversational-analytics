"""Retain one exact same-terms successor under current explicit actor authority."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4
from datetime import datetime

from app.persistence.auth import _new_uuid7, RevocationKey, RevocationScopeType
from app.persistence.onboarding import require_journey
from app.persistence.activation_reconciliation import ActivationReconciliationJournal
from app.persistence.installation_continuation import InstallationContinuationStore, _generation, _json, _scope
from app.persistence.source_preparations import SourcePreparations
from app.security.activation_review_replacement import replacement_request, prepare_request, validate_result, contract
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.installation_continuation import canonical_request
from app.security.source_replacement import SourceReplacementRefused


def _digest(value):
    return hashlib.sha256(canonical_request(value)).hexdigest()


class ActivationReviewReplacements:
    """Reservation/adoption only. No automatic admission, consent, quote or license."""
    def __init__(self, store, *, origin):
        self.store = store
        self.reconciliation = ActivationReconciliationJournal(store, origin=origin)
        self.contexts = InstallationContinuationStore(store)
        self.preparations = SourcePreparations(store)

    def row(self, connection, original_operation_id):
        row = connection.execute("SELECT * FROM desktop_activation_review_replacements WHERE original_operation_id=?",
            (original_operation_id,)).fetchone()
        return None if row is None else dict(row)

    def _eligible(self, connection, operation, actor_scope, *, authorize, successor=None, successor_desktop=None):
        authorize(connection=connection)
        original, deadline = self.reconciliation.request(connection, operation, allow_replaced=successor_desktop is not None)
        evidence = self.preparations.get(original["source_operation_id"], connection=connection)
        terminal, _ = self.reconciliation.exact_row(connection, operation, original,
            authorize=lambda: authorize(connection=connection), allow_replaced=successor_desktop is not None)
        selected = json.loads(operation["owner_scope_json"])
        actor = selected.get("actor")
        if (not isinstance(actor, dict) or set(actor) != {"principal_id", "creator_account_id", "role"}
                or actor["role"] != "creator" or actor["creator_account_id"] != original["scope"]["creator_account_id"]
                or not actor["principal_id"]
                or any(actor_scope.get(field) != selected.get(field) for field in
                    ("actor", "key", "organization_id", "installation_id", "creator_account_id", "association_request_id",
                     "source_journey_id"))
                or terminal is None or terminal["status"] != "expired-uncommitted" or evidence is None):
            raise HostedGrantUnavailable("Original commercial review is unavailable")
        delivery = connection.execute("SELECT activation_committed FROM capability_license_activation_deliveries "
            "WHERE activation_operation_id=?", (original["activation_operation_id"],)).fetchone()
        ceremony = connection.execute("SELECT domain_status FROM desktop_browser_ceremonies WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        if (delivery is None or delivery["activation_committed"] or ceremony is None
                or ceremony["domain_status"] in {"committed", "revoked"}):
            raise HostedGrantUnavailable("Original commercial outcome changed")
        target = self.contexts.selection.select_in_transaction(connection,
            association_request_id=original["scope"]["association_request_id"])
        if not target.finalized or target.binding_revoked_at is not None or _scope(target) != original["scope"]:
            raise HostedGrantUnavailable("Original commercial account changed")
        for kind, value in ((RevocationScopeType.INSTALLATION, target.claim.installation_id),
                (RevocationScopeType.CREATOR_ACCOUNT, target.candidate.creator_account_id),
                (RevocationScopeType.PRINCIPAL, actor["principal_id"])):
            if self.store._scope_is_revoked(connection, RevocationKey(kind, value)):
                raise HostedGrantUnavailable("Original commercial authority revoked")
        # An unresolved ordinary source replacement may already have created a
        # successor. Only its exact closed commercial refusal is compatible.
        competing = connection.execute("SELECT r.state,o.status FROM onboarding_source_replacement_reservations r "
            "LEFT JOIN onboarding_source_replacement_outcomes o USING(predecessor_operation_id) "
            "WHERE r.predecessor_operation_id=?", (original["source_operation_id"],)).fetchone()
        if competing is not None and competing["status"] != "commercial-reconciliation-required":
            raise HostedGrantUnavailable("Original commercial source has another owner")
        now = self.store._now()
        current_source = connection.execute("SELECT state FROM onboarding_journeys WHERE operation_id=?",
            (original["source_operation_id"],)).fetchone()
        if current_source is not None and current_source["state"] == "revoked":
            raise HostedGrantUnavailable("Original commercial source revoked")
        for retired in connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
                (evidence["journey_id"],)):
            if (json.loads(retired["evidence_json"]).get("journey") or {}).get("state") == "revoked":
                raise HostedGrantUnavailable("Original commercial source revoked")
        for row in connection.execute("SELECT j.operation_id,j.state,j.expires_at,c.provider_expires_at "
                "FROM onboarding_journeys j JOIN installation_continuation_contexts c ON c.journey_id=j.journey_id "
                "WHERE j.operation_id<>? AND j.state NOT IN ('expired','revoked')", (original["source_operation_id"],)):
            if row["operation_id"] == successor:
                continue
            if (row["provider_expires_at"] is None and row["state"] != "new"
                    or row["provider_expires_at"] is None and datetime.fromisoformat(row["expires_at"]) > now
                    or row["provider_expires_at"] is not None and timestamp(row["provider_expires_at"]) > now):
                raise HostedGrantUnavailable("Competing commercial source")
        if connection.execute("SELECT 1 FROM desktop_setup_operations WHERE journey_id=? AND action='activation.review' "
                "AND state IN ('accepted','waiting','outcome_unknown') AND operation_id NOT IN (?,?)",
                (operation["journey_id"], operation["operation_id"], successor_desktop or operation["operation_id"])).fetchone():
            raise HostedGrantUnavailable("Competing commercial review")
        # Do not compete with a different protected decision/authentication.
        for child in connection.execute("SELECT * FROM desktop_setup_operations WHERE journey_id=? AND "
                "action IN ('activation.confirm','auth.select','auth.switch','auth.cancel') "
                "AND state IN ('accepted','waiting','outcome_unknown')", (operation["journey_id"],)):
            saved = json.loads(child["owner_scope_json"])
            parent = (saved.get("desktop_decision") or {}).get("operation_id") if child["action"] == "activation.confirm" else (
                saved.get("authentication_intent") or {}).get("parent_operation_id")
            if parent != operation["operation_id"]:
                raise HostedGrantUnavailable("Competing protected commercial action")
        self.contexts.selection.require_current(connection, target)
        return original, evidence, target, _digest(actor)

    def describe(self, connection, operation, actor_scope, *, authorize):
        original, evidence, _, actor_digest = self._eligible(connection, operation, actor_scope, authorize=authorize)
        row = self.row(connection, operation["operation_id"])
        if row is not None:
            self._exact(connection, operation, actor_scope, authorize=authorize)
            if row["status"] != "reserved":
                return None
        return {"original_operation_id": operation["operation_id"], "source_operation_id": original["source_operation_id"],
            "activation_operation_id": original["activation_operation_id"],
            "source_prepare_digest": evidence["prepare_digest"], "actor_digest": actor_digest}

    def reserve(self, connection, operation, actor_scope, *, successor_desktop_operation_id, authorize):
        require_journey(successor_desktop_operation_id)
        if successor_desktop_operation_id == operation["operation_id"]:
            raise HostedGrantUnavailable("Review replacement must have a distinct desktop command")
        original, evidence, _, actor_digest = self._eligible(connection, operation, actor_scope, authorize=authorize)
        row = self.row(connection, operation["operation_id"])
        if row is not None:
            if row["successor_desktop_operation_id"] != successor_desktop_operation_id:
                raise HostedGrantUnavailable("Review replacement already reserved")
            return self._exact(connection, operation, actor_scope, authorize=authorize)[0]
        now = self.store._now()
        request = replacement_request(json.loads(evidence["prepare_json"]),
            continuation_id=original["source_continuation_id"], activation_operation_id=original["activation_operation_id"],
            successor_source_operation_id=_new_uuid7(now), successor_activation_operation_id=_new_uuid7(now))
        connection.execute("INSERT INTO desktop_activation_review_replacements "
            "(original_operation_id,source_operation_id,activation_operation_id,successor_source_operation_id,"
            "successor_activation_operation_id,successor_journey_id,successor_desktop_operation_id,request_json,provider_origin,actor_digest,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (operation["operation_id"], request["source_operation_id"],
                request["activation_operation_id"], request["successor_source_operation_id"], request["successor_activation_operation_id"],
                str(uuid4()), successor_desktop_operation_id, canonical_request(request).decode(),
                self.reconciliation.origin, actor_digest, now.timestamp()))
        authorize(connection=connection)
        return self.row(connection, operation["operation_id"])

    def _exact(self, connection, operation, actor_scope, *, authorize):
        row = self.row(connection, operation["operation_id"])
        if row is None or row["provider_origin"] != self.reconciliation.origin:
            raise HostedGrantUnavailable("Review replacement is unavailable")
        original, evidence, target, actor_digest = self._eligible(connection, operation, actor_scope,
            authorize=authorize, successor=row["successor_source_operation_id"] if row["status"] == "prepared" else None,
            successor_desktop=row["successor_desktop_operation_id"])
        request = replacement_request(json.loads(evidence["prepare_json"]),
            continuation_id=original["source_continuation_id"], activation_operation_id=original["activation_operation_id"],
            successor_source_operation_id=row["successor_source_operation_id"], successor_activation_operation_id=row["successor_activation_operation_id"])
        if (row["actor_digest"] != actor_digest or row["request_json"] != canonical_request(request).decode()
                or row["source_operation_id"] != original["source_operation_id"]
                or row["activation_operation_id"] != original["activation_operation_id"]):
            raise HostedGrantUnavailable("Review replacement identity changed")
        return row, request, evidence, target

    def retire_original(self, connection, operation, actor_scope, reservation, *, authorize):
        """Close only the proved expired original and its exact obsolete children.
        The caller inserts the accepted successor command in this same transaction.
        """
        row, _, _, _ = self._exact(connection, operation, actor_scope, authorize=authorize)
        if row != reservation or row["status"] != "reserved" or connection.execute(
                "SELECT 1 FROM desktop_setup_operations WHERE operation_id=?", (row["successor_desktop_operation_id"],)).fetchone():
            raise HostedGrantUnavailable("Review replacement reservation changed")
        changed = connection.execute("UPDATE desktop_setup_operations SET state='refused',reason='operation_expired' "
            "WHERE operation_id=? AND state IN ('accepted','waiting','outcome_unknown')",
            (operation["operation_id"],)).rowcount
        if changed != 1:
            raise HostedGrantUnavailable("Original review command changed")
        for child in connection.execute("SELECT * FROM desktop_setup_operations WHERE journey_id=? AND "
                "action IN ('activation.confirm','auth.select','auth.switch','auth.cancel') "
                "AND state IN ('accepted','waiting','outcome_unknown')", (operation["journey_id"],)).fetchall():
            saved = json.loads(child["owner_scope_json"])
            parent = (saved.get("desktop_decision") or {}).get("operation_id") if child["action"] == "activation.confirm" else (
                saved.get("authentication_intent") or {}).get("parent_operation_id")
            if parent != operation["operation_id"]:
                raise HostedGrantUnavailable("Competing protected commercial action")
            connection.execute("UPDATE desktop_setup_operations SET state='refused',reason='operation_expired' WHERE operation_id=?",
                (child["operation_id"],))
        return reservation

    @staticmethod
    def successor_intent(row):
        return {name: row[name] for name in ("original_operation_id", "source_operation_id", "activation_operation_id",
            "successor_desktop_operation_id", "successor_source_operation_id", "successor_activation_operation_id", "actor_digest")}

    def admitted_successor(self, connection, original, row):
        successor = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (row["successor_desktop_operation_id"],)).fetchone()
        parent = connection.execute("SELECT state,reason FROM desktop_setup_operations WHERE operation_id=?",
            (original["operation_id"],)).fetchone()
        if (parent is None or parent["state"] != "refused" or parent["reason"] != "operation_expired"
                or successor is None or successor["journey_id"] != original["journey_id"] or successor["action"] != "activation.review"
                or successor["state"] not in {"accepted","waiting","outcome_unknown"}
                or json.loads(successor["owner_scope_json"]).get("activation_replacement") != self.successor_intent(row)
                or any(json.loads(successor["owner_scope_json"]).get(field) != json.loads(original["owner_scope_json"]).get(field)
                    for field in ("actor", "key", "organization_id", "installation_id", "creator_account_id",
                                  "association_request_id", "source_journey_id"))):
            raise HostedGrantUnavailable("Review replacement command is not admitted")
        return dict(successor)

    def claim(self, connection, operation, actor_scope, *, authorize, reconcile=False):
        row, request, _, _ = self._exact(connection, operation, actor_scope, authorize=authorize)
        self.admitted_successor(connection, operation, row)
        if (row["status"], row["attempts"], reconcile) not in {("reserved", 0, False), ("reserved", 0, True),
                ("send-claimed", 1, True)}:
            return None
        attempt = row["attempts"] + 1
        connection.execute("UPDATE desktop_activation_review_replacements SET status='send-claimed',attempts=? "
            "WHERE original_operation_id=?", (attempt, operation["operation_id"]))
        authorize(connection=connection)
        return request, attempt

    def guard(self, connection, operation, actor_scope, request, attempt, *, authorize):
        row, expected, evidence, target = self._exact(connection, operation, actor_scope, authorize=authorize)
        self.admitted_successor(connection, operation, row)
        if row["status"] != "send-claimed" or row["attempts"] != attempt or request != expected:
            raise HostedGrantUnavailable("Review replacement is not the claimed operation")
        return row, evidence, target

    def record_refusal(self, connection, operation, actor_scope, request, attempt, refusal, *, authorize):
        self.guard(connection, operation, actor_scope, request, attempt, authorize=authorize)
        if (not isinstance(refusal, SourceReplacementRefused)
                or refusal.reason not in contract().profile["refusal_status"]
                or refusal.reason in {"rate_limited", "temporarily_unavailable"}):
            raise HostedGrantUnavailable("Review replacement refusal unavailable")
        connection.execute("UPDATE desktop_activation_review_replacements SET status='refused',refusal_reason=? "
            "WHERE original_operation_id=?", (refusal.reason, operation["operation_id"]))
        authorize(connection=connection)

    def record_prepared(self, connection, operation, actor_scope, request, attempt, result, *, authorize):
        row, evidence, target = self.guard(connection, operation, actor_scope, request, attempt, authorize=authorize)
        now = self.store._now()
        result = validate_result(request, result, now=now)
        successor = result["successor"]
        expiry = timestamp(successor["expires_at"])
        if expiry.timestamp() > evidence["retain_until"] or connection.execute(
                "SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
            raise HostedGrantUnavailable("Review replacement retention exceeded")
        identity = row["successor_journey_id"]
        connection.execute("INSERT INTO onboarding_journeys (journey_id,kind,created_at,expires_at,operation_id,"
            "installation_id,state) VALUES (?,'registered-continuation',?,?,?,?,'waiting')",
            (identity, now.isoformat(), expiry.isoformat(), successor["operation_id"], evidence["installation_id"]))
        journey = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (identity,)).fetchone()
        prepared = prepare_request(request, successor=True)
        self.preparations.retain(connection, journey, prepared, review_predecessor_operation_id=evidence["operation_id"])
        self.preparations.record_prepared(connection, journey, expires_at=successor["expires_at"],
            continuation_id=successor["continuation_id"])
        connection.execute("INSERT INTO installation_continuation_contexts (journey_id,operation_profile,association_request_id,"
            "selection_json,scope_json,continuation_id,reference,provider_expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (identity, prepared["profile"], target.candidate.association_request_id, _generation(target),
                _json(_scope(target)), successor["continuation_id"], successor["reference"], successor["expires_at"]))
        self.contexts.selection.require_current(connection, target)
        authorize(connection=connection)
        # Adopt only the accepted successor command, with its reserved source and
        # activation IDs. No prior quote, consent, or authentication is copied.
        connection.execute("INSERT INTO desktop_activation_sources VALUES (?,?,?)",
            (row["successor_desktop_operation_id"], identity, row["successor_activation_operation_id"]))
        connection.execute("UPDATE onboarding_journeys SET state='expired',revision=revision+1 "
            "WHERE operation_id=? AND state NOT IN ('expired','revoked')", (request["source_operation_id"],))
        authorize(connection=connection)
        connection.execute("UPDATE desktop_activation_review_replacements SET status='prepared' WHERE original_operation_id=?",
            (operation["operation_id"],))
        return self.row(connection, operation["operation_id"])
