"""Exact original initial outcome, independent of the expired presentation."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.persistence.auth import (RevocationKey, RevocationScopeType, _verified_grant_reference,
    _time_text, _new_uuid7)
from app.persistence.catchup_events import capture_authority_change
from app.persistence.source_preparations import SourcePreparations
from app.security.hosted_grants import HostedGrantUnavailable, require_hosted_origin
from app.security.initial_handoff import validate_receipt, validate_scope, timestamp
from app.security.installation_continuation import canonical_request

MAX_ATTEMPTS = 3
BACKOFF = (15, 30, 60)


class InitialReplacementRecovery:
    def __init__(self, store, origin):
        self.store = self.authentication = store
        self.origin = require_hosted_origin(origin)
        self.preparations = SourcePreparations(store)

    def context(self, connection, operation, *, authorize):
        authorize(connection=connection)
        selected = json.loads(operation["owner_scope_json"])
        proposal = selected["source_replacement"]
        coords = proposal["replacement"]
        original = coords["operation_id"]
        evidence = self.preparations.get(original, connection=connection)
        outcome = connection.execute("SELECT * FROM onboarding_source_replacement_outcomes WHERE predecessor_operation_id=?",
            (original,)).fetchone()
        reservation = connection.execute("SELECT * FROM onboarding_source_replacement_reservations WHERE predecessor_operation_id=?",
            (original,)).fetchone()
        prepared = connection.execute("SELECT * FROM onboarding_source_prepared_receipts WHERE operation_id=?", (original,)).fetchone()
        if (evidence is None or outcome is None or reservation is None or prepared is None
                or evidence["kind"] != "initial-enrollment" or coords["kind"] != evidence["kind"]
                or evidence["journey_id"] != proposal["source_journey_id"]
                or evidence["prepare_digest"] != coords["expected_digest"]
                or evidence["installation_id"] != coords["installation_id"]
                or reservation["state"] != "send-claimed"
                or reservation["creator_account_id"] != proposal["creator_account_id"]
                or reservation["successor_operation_id"] != selected["successor_operation_id"]
                or outcome["status"] not in {"accepted", "committed"}
                or outcome["status"] == "committed" and outcome["original_domain_operation_id"] != original
                or prepared["provider_expires_at"] > self.store._now().timestamp()):
            raise HostedGrantUnavailable("Original enrollment is unavailable")
        request = json.loads(evidence["prepare_json"])
        key = self.store.installation_key_reference(connection=connection)
        if (key is None or selected["key"] != {"installation_key_id": key.installation_key_id,
                "installation_key_jkt": key.installation_key_jkt}
                or request["destination"]["installation_id"] != evidence["installation_id"]
                or request["destination"]["installation_key"]["kid"] != key.installation_key_id
                or request["destination"]["installation_key"]["jwk"] != {name: json.loads(key.public_key_jwk)[name]
                    for name in ("crv", "kty", "x", "y")}):
            raise HostedGrantUnavailable("Original enrollment key changed")
        identity = evidence["journey_id"]
        current = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (identity,)).fetchone()
        retired = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=? AND expires_at>?",
            (identity, self.store._now().timestamp())).fetchone()
        workspace = connection.execute("SELECT * FROM onboarding_workspace_recovery WHERE previous_journey_id=?",
            (identity,)).fetchone()
        prior = None if retired is None else json.loads(retired["evidence_json"]).get("journey")
        scopes = []
        # These are existing owner-written scope records, not inferred from the
        # creator currently selected in Chrome or from an enrollment receipt.
        for item in (current, prior, workspace):
            if item is None:
                continue
            if (item["operation_id"] != original or item["installation_id"] != evidence["installation_id"]
                    or item["state"] in {"revoked", "transfer", "reauthentication-required"}):
                raise HostedGrantUnavailable("Original enrollment scope changed")
            if item["scope_json"] is not None:
                scopes.append(json.loads(item["scope_json"]))
        if workspace is not None and workspace['resolved_journey_id'] not in (None, identity):
            raise HostedGrantUnavailable('Original enrollment has another recovery owner')
        if current is None and prior is None:
            raise HostedGrantUnavailable("Original enrollment evidence is unavailable")
        scope = None if not scopes else scopes[0]
        if scope is not None:
            validate_scope(scope)
            if (any(value != scope for value in scopes)
                    or scope["intended_creator_id"] != proposal["creator_account_id"]
                    or scope["installation_id"] != evidence["installation_id"]
                    or scope["installation_key_jkt"] != key.installation_key_jkt):
                raise HostedGrantUnavailable("Original authorized enrollment scope changed")
        if any(self.store._scope_is_revoked(connection, RevocationKey(kind, value)) for kind, value in (
                (RevocationScopeType.INSTALLATION, evidence["installation_id"]),
                (RevocationScopeType.CREATOR_ACCOUNT, proposal["creator_account_id"]))):
            raise HostedGrantUnavailable("Original enrollment revoked")
        journal = connection.execute("SELECT * FROM onboarding_initial_replacement_recoveries WHERE predecessor_operation_id=?",
            (original,)).fetchone()
        if journal is not None and (journal["desktop_operation_id"] != operation["operation_id"]
                or journal["provider_origin"] != self.origin):
            raise HostedGrantUnavailable("Original enrollment recovery changed")
        if connection.execute("SELECT 1 FROM onboarding_transfer_intents UNION ALL SELECT 1 FROM onboarding_transfer_proofs").fetchone():
            raise HostedGrantUnavailable("Original enrollment has another protected owner")
        return {"evidence": evidence, "scope": scope, "key": key, "outcome": dict(outcome),
            "provider_expires_at": prepared["provider_expires_at"],
            "journal": None if journal is None else dict(journal)}

    @staticmethod
    def validate_original_receipt(context, receipt):
        validate_receipt(receipt)
        evidence, key, scope = context["evidence"], context["key"], context["scope"]
        if (timestamp(receipt["consumed_at"]).timestamp() >= context["provider_expires_at"]
                or receipt["operation_id"] != evidence["operation_id"]
                or receipt["installation_id"] != evidence["installation_id"]
                or receipt["installation_key_id"] != key.installation_key_id
                or receipt["installation_key_jkt"] != key.installation_key_jkt
                or scope is not None and any(receipt[name] != scope[name] for name in
                    ("onboarding_transaction_id", "organization_id"))):
            raise HostedGrantUnavailable("Original enrollment receipt changed")
        return receipt

    def _check_claims(self, connection, context, receipt):
        for claim in connection.execute("SELECT * FROM provisioning_claim_submissions"):
            if (receipt is None or claim["claim_id"] != receipt["claim_id"]
                    or claim["state"] == "refused" or any(claim[name] != receipt[name] for name in
                        ("installation_id", "organization_id", "onboarding_transaction_id"))
                    or claim["claim_profile"] != "urn:bridge-clean:installation-claim:v2"):
                raise HostedGrantUnavailable("Another enrollment is already recorded")
        for candidate in connection.execute("SELECT * FROM provisioning_candidates"):
            scope = context["scope"]
            if (scope is None or candidate["association_request_id"] != scope["association_request_id"]
                    or candidate["creator_account_id"] != scope["intended_creator_id"]
                    or any(candidate[name] != scope[name] for name in
                        ("installation_id", "organization_id", "onboarding_transaction_id"))
                    or candidate["state"] not in {"pending", "approved"}):
                raise HostedGrantUnavailable("Another account is already recorded")

    def claim(self, connection, operation, *, authorize):
        context = self.context(connection, operation, authorize=authorize)
        journal = context["journal"]
        now = self.store._now().timestamp()
        if journal is not None:
            if journal["completed"] or journal["blocked"] or journal["attempts"] >= MAX_ATTEMPTS or now < journal["retry_at"]:
                return None
            if journal["receipt_json"] is not None and context["scope"] is None:
                return None
        original = context["evidence"]["operation_id"]
        attempt = 1 if journal is None else journal["attempts"] + 1
        if journal is None:
            connection.execute("INSERT INTO onboarding_initial_replacement_recoveries VALUES (?,?,?,?,?,NULL,0,0)",
                (original, operation["operation_id"], self.origin, attempt, now + BACKOFF[attempt-1]))
        else:
            connection.execute("UPDATE onboarding_initial_replacement_recoveries SET attempts=?,retry_at=? WHERE predecessor_operation_id=?",
                (attempt, now + BACKOFF[attempt-1], original))
        return self.context(connection, operation, authorize=authorize)

    def guard(self, connection, operation, expected, *, authorize):
        current = self.context(connection, operation, authorize=authorize)
        if (current["scope"] != expected["scope"] or current["key"] != expected["key"]
                or current["evidence"] != expected["evidence"] or current["outcome"] != expected["outcome"]
                or current["journal"] is None or current["journal"]["attempts"] != expected["journal"]["attempts"]
                or current["provider_expires_at"] != expected["provider_expires_at"]
                or current["journal"]["receipt_json"] != expected["journal"]["receipt_json"]
                or current["journal"]["completed"] or current["journal"]["blocked"]):
            raise HostedGrantUnavailable("Original enrollment recovery changed")
        raw = current["journal"]["receipt_json"]
        if raw is not None:
            self._check_claims(connection, current, json.loads(raw))
        return current

    def block(self, connection, operation, expected, *, authorize):
        current = self.guard(connection, operation, expected, authorize=authorize)
        connection.execute("UPDATE onboarding_initial_replacement_recoveries SET blocked=1 WHERE predecessor_operation_id=?",
            (current["evidence"]["operation_id"],))

    def retain_receipt(self, connection, operation, expected, receipt, *, authorize):
        current = self.guard(connection, operation, expected, authorize=authorize)
        self.validate_original_receipt(current, receipt)
        self._check_claims(connection, current, receipt)
        raw = canonical_request(receipt).decode()
        prior = current["journal"]["receipt_json"]
        if prior is not None and prior != raw:
            raise HostedGrantUnavailable("Original enrollment receipt changed")
        connection.execute("UPDATE onboarding_initial_replacement_recoveries SET receipt_json=? WHERE predecessor_operation_id=?",
            (raw, current["evidence"]["operation_id"]))
        return self.context(connection, operation, authorize=authorize)

    def require_bootstrap(self, connection, context, grant_reference_ids):
        receipt = json.loads(context["journal"]["receipt_json"])
        key = context["key"]
        now = self.store._now()
        grants = []
        for reference_id in grant_reference_ids:
            row = connection.execute("SELECT * FROM verified_grant_references WHERE reference_id=?", (reference_id,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise HostedGrantUnavailable("Original bootstrap grant unavailable")
            grant = _verified_grant_reference(row)
            if (grant.installation_id != receipt["installation_id"] or grant.organization_id != receipt["organization_id"]
                    or grant.installation_key_id != key.installation_key_id or grant.installation_key_jkt != key.installation_key_jkt
                    or not grant.valid_from <= now < grant.expires_at
                    or self.store._grant_has_hosted_tombstone(connection, grant)
                    or self.store._scope_is_revoked(connection, RevocationKey(RevocationScopeType.VERIFIED_GRANT, reference_id))
                    or self.store._scope_is_revoked(connection, RevocationKey(RevocationScopeType.PRINCIPAL, grant.subject))):
                raise HostedGrantUnavailable("Original bootstrap grant changed")
            grants.append(grant)
        if (len(grants) != 2 or {grant.grant_type for grant in grants} != {"installation_grant", "membership_snapshot"}
                or len({(grant.issuer, grant.subject) for grant in grants}) != 1):
            raise HostedGrantUnavailable("Original bootstrap grants incomplete")

    @capture_authority_change
    def adopt_bootstrap(self, operation, expected, grants, *, enrolled_at, recovered_at, authorize):
        """One local commit for verified bootstrap authority and original enrollment."""
        with self.store.database.transaction() as connection:
            current = self.guard(connection, operation, expected, authorize=authorize)
            receipt = json.loads(current["journal"]["receipt_json"])
            scope = current["scope"]
            if scope is None or enrolled_at != timestamp(receipt["consumed_at"]):
                raise HostedGrantUnavailable("Original bootstrap result changed")
            claim = connection.execute("SELECT * FROM provisioning_claim_submissions WHERE claim_id=?",
                (receipt["claim_id"],)).fetchone()
            if claim is None or claim["state"] not in {"submitted", "consumed"}:
                raise HostedGrantUnavailable("Original claim unavailable")
            for grant in grants:
                self.store._validate_verified_grant(grant)
                recorded = connection.execute("SELECT * FROM verified_grant_references WHERE reference_id=?",
                    (grant.reference_id,)).fetchone()
                if recorded is None:
                    self.store._insert_verified_grant(connection, grant)
                elif (recorded["revoked_at"] is not None
                        or replace(_verified_grant_reference(recorded), verified_at=grant.verified_at) != grant):
                    raise HostedGrantUnavailable("Original bootstrap grant changed")
            grant_ids = tuple(grant.reference_id for grant in grants)
            self.require_bootstrap(connection, current, grant_ids)
            if claim["state"] == "submitted":
                connection.execute("UPDATE provisioning_claim_submissions SET state='consumed',outcome=NULL,resolved_at=?,"
                    "enrolled_at=? WHERE claim_id=? AND state='submitted'",
                    (_time_text(recovered_at), _time_text(enrolled_at), receipt["claim_id"]))
                self.store._enqueue_onboarding_progress_in_transaction(connection, "enrolled",
                    event_id=_new_uuid7(recovered_at), correlation_id=_new_uuid7(recovered_at), occurred_at=recovered_at)
            candidate = connection.execute("SELECT 1 FROM provisioning_candidates WHERE association_request_id=?",
                (scope["association_request_id"],)).fetchone()
            if candidate is None:
                connection.execute("INSERT INTO provisioning_candidates (association_request_id,installation_id,"
                    "onboarding_transaction_id,organization_id,creator_account_id,state,requested_at,resolved_at) "
                    "VALUES (?,?,?,?,?,'pending',?,NULL)", (scope["association_request_id"], scope["installation_id"],
                    scope["onboarding_transaction_id"], scope["organization_id"], scope["intended_creator_id"], _time_text(self.store._now())))
            self.store._increment_authorization_epoch(connection)
            return self.finish(connection, operation, expected, grant_ids, authorize=authorize)

    def finish(self, connection, operation, expected, grant_reference_ids, *, authorize):
        current = self.guard(connection, operation, expected, authorize=authorize)
        self.require_bootstrap(connection, current, grant_reference_ids)
        scope = current["scope"]
        receipt = self.validate_original_receipt(current, json.loads(current["journal"]["receipt_json"]))
        self._check_claims(connection, current, receipt)
        claim = connection.execute("SELECT * FROM provisioning_claim_submissions WHERE claim_id=?",
            (receipt["claim_id"],)).fetchone()
        candidate = None if scope is None else connection.execute("SELECT * FROM provisioning_candidates WHERE association_request_id=?",
            (scope["association_request_id"],)).fetchone()
        if scope is None or claim is None or claim["state"] != "consumed" or candidate is None:
            raise HostedGrantUnavailable("Original enrollment is not locally verified")
        if claim["enrolled_at"] is None or timestamp(receipt["consumed_at"]) != datetime.fromisoformat(claim["enrolled_at"]):
            raise HostedGrantUnavailable("Original enrollment time changed")
        evidence = current["evidence"]
        original, identity = evidence["operation_id"], evidence["journey_id"]
        now = self.store._now()
        # Restore a bounded *local completed view*. No hosted locator or
        # authorization deadline is renewed and no new source is created.
        deadline = min(now + timedelta(minutes=30), datetime.fromtimestamp(evidence["retain_until"], timezone.utc))
        if deadline <= now:
            raise HostedGrantUnavailable("Original enrollment evidence expired")
        encoded = canonical_request(scope).decode()
        row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (identity,)).fetchone()
        if row is None:
            connection.execute("INSERT INTO onboarding_journeys (journey_id,kind,created_at,expires_at,operation_id,"
                "installation_id,state,scope_json,prepare_json) VALUES (?,'initial-enrollment',?,?,?,?,'completed',?,?)",
                (identity, now.isoformat(), deadline.isoformat(), original, evidence["installation_id"], encoded, evidence["prepare_json"]))
        else:
            connection.execute("UPDATE onboarding_journeys SET state='completed',scope_json=?,reason=NULL,"
                "handoff_reference=NULL,expires_at=?,revision=revision+1 WHERE journey_id=?",
                (encoded, deadline.isoformat(), identity))
        retired = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?", (identity,)).fetchone()
        if retired is not None:
            document = json.loads(retired["evidence_json"])
            document["journey"].update(state="completed", scope_json=encoded)
            document.pop("terminal_read", None)
            document["unresolved_work"] = [item for item in document["unresolved_work"]
                if item not in {"journey-operation", "initial-completion"}]
            connection.execute("UPDATE onboarding_presentation_retirements SET evidence_json=? WHERE journey_id=?",
                (json.dumps(document, sort_keys=True, separators=(",", ":")), identity))
        connection.execute("UPDATE onboarding_workspace_recovery SET state='completed',scope_json=?,resolved_journey_id=NULL "
            "WHERE previous_journey_id=?", (encoded, identity))
        connection.execute("DELETE FROM onboarding_uncertain_receipts WHERE installation_id=? AND operation_id=?",
            (evidence["installation_id"], original))
        connection.execute("UPDATE onboarding_initial_replacement_recoveries SET completed=1 WHERE predecessor_operation_id=?", (original,))
        authorize(connection=connection)
        return identity
