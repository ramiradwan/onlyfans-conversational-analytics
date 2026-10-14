"""Read-only presentation reconciliation constrained by accepted local work."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app.persistence.installation_continuation import _generation, _scope
from app.persistence.onboarding import OnboardingJourneyUnavailable, _digest, require_journey, presentation_evidence


_PROTECTED = {"preparing", "prepare-unknown", "completing", "unknown"}
_TERMINAL = {"new", "completed", "expired", "revoked"}
_TABLES = ("onboarding_journeys", "installation_continuation_contexts",
    "onboarding_workspace_recovery", "onboarding_uncertain_receipts",
    "onboarding_transfer_intents", "onboarding_transfer_proofs", "onboarding_transfer_relays",
    "provisioning_claim_submissions", "capability_license_pending_deliveries", "first_enrollment_contexts",
    "capability_license_redemption_journal", "capability_license_activation_deliveries",
    "onboarding_presentation_retirements", "provisioning_browser_sessions",
    "onboarding_native_selection_receipts", "onboarding_presentation_sessions")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SavedWorkspaceStore:
    def __init__(self, continuations):
        self.continuations = continuations
        self.authentication = continuations.authentication

    def presentation(self, identifier, *, connection=None):
        if connection is None:
            with self.authentication.database.read() as current:
                return self.presentation(identifier, connection=current)
        row = connection.execute("SELECT * FROM onboarding_presentation_sessions WHERE session_digest=?",
            (_digest(identifier),)).fetchone()
        if row is None or row["expires_at"] <= self.authentication._now().timestamp():
            return None
        return dict(row)

    def inspect(self, target, record, candidate, *, connection=None, selected=None, evidence=None, work=(),
                retained_identifier=None):
        require_journey(record); require_journey(candidate)
        if connection is None:
            with self.authentication.database.transaction(immediate=False) as current:
                return self.inspect(target, record, candidate, connection=current,
                    selected=selected, evidence=evidence, work=work, retained_identifier=retained_identifier)
        self.continuations.selection.require_current(connection, target)
        rows = {name: sorted([dict(row) for row in connection.execute("SELECT * FROM " + name).fetchall()], key=canonical)
            for name in _TABLES}
        journeys = {row["journey_id"]: row for row in rows["onboarding_journeys"]}
        contexts = {row["journey_id"]: row for row in rows["installation_continuation_contexts"]}
        moment = self.authentication._now(); now = moment.timestamp()
        if selected is None and retained_identifier is not None:
            digest = _digest(retained_identifier)
            retained = next((row for row in rows["onboarding_presentation_sessions"]
                if row["session_digest"] == digest and row["expires_at"] > now
                and row["record_journey_id"] == record and row["candidate_journey_id"] == candidate), None)
            session = next((row for row in rows["provisioning_browser_sessions"]
                if row["session_digest"] == digest and row["expires_at"] > now), None)
            if retained is not None and session is not None:
                current = self.continuations.require_current(session["journey_id"], connection=connection)
                if _generation(current["target"]) != _generation(target):
                    raise OnboardingJourneyUnavailable("workspace_changed")
                selected = session["journey_id"]
                evidence = json.loads(retained["candidate_evidence_json"])
        classification = "indeterminate"
        item = journeys.get(candidate)
        retirement = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements "
            "WHERE journey_id=? AND expires_at>?", (candidate, now)).fetchone()
        if item is None and retirement is not None:
            evidence = json.loads(retirement["evidence_json"])
        if item is None and evidence is not None:
            item = evidence.get("journey")
            if evidence.get("context"):
                contexts[candidate] = evidence["context"]
        if item is None and target.finalized and target.binding_revoked_at is None \
                and target.candidate.state.value == "approved":
            # Exact finalized enrollment is terminal aggregate evidence. All
            # pending/uncertain families below must independently permit it.
            classification = "inactive"
        matches = False
        candidate_live = False
        candidate_uncertain = False
        retained_record = False
        record_detail = contexts.get(record)
        record_current = journeys.get(record)
        if record_current is not None and record_detail is not None:
            retained_record = record_current["kind"] == "registered-continuation" \
                and datetime.fromisoformat(record_current["expires_at"]).timestamp() > now \
                and record_detail["selection_json"] == _generation(target) \
                and record_detail["scope_json"] == canonical(_scope(target))
        if item is not None:
            detail = contexts.get(candidate)
            matches = (item["kind"] == "registered-continuation" and detail is not None
                and detail["selection_json"] == _generation(target)
                and detail["scope_json"] == canonical(_scope(target)))
            live = datetime.fromisoformat(item["expires_at"]).timestamp() > now
            candidate_live = live
            candidate_uncertain = item["state"] in _PROTECTED or item["state"] == "waiting" and not (
                detail is not None and detail["provider_state"] in {"expired", "revoked"})
            if live:
                classification = "same_target" if matches else "conflict"
            elif item["state"] in _TERMINAL and (item["state"] != "new" or item["operation_id"] is None
                    or item["kind"] == "registered-continuation" and detail and detail["continuation_id"] is None):
                classification = "inactive"
        for retirement_row in rows["onboarding_presentation_retirements"]:
            retained = json.loads(retirement_row["evidence_json"])
            retired_journey = retained.get("journey")
            retired_context = retained.get("context")
            if retained.get("unresolved_work") or retired_journey is not None and (
                    retired_journey["state"] in _PROTECTED or retired_journey["state"] == "waiting") and not (
                    retired_context is not None and retired_context["provider_state"] in {"expired", "revoked"}):
                classification = "indeterminate"
        for row in rows["onboarding_journeys"]:
            identity = row["journey_id"]
            live = datetime.fromisoformat(row["expires_at"]).timestamp() > now
            detail = contexts.get(identity)
            same = row["kind"] == "registered-continuation" and detail is not None \
                and detail["selection_json"] == _generation(target) and detail["scope_json"] == canonical(_scope(target))
            if identity == selected:
                if not live or not same:
                    classification = "conflict"
                continue
            if identity == record and retained_record:
                continue
            if identity == candidate and classification == "same_target":
                continue
            if row["state"] in _PROTECTED or row["kind"] == "registered-continuation" and row["state"] == "waiting" \
                    and (detail is None or detail["provider_state"] not in {"expired", "revoked"}):
                classification = "indeterminate"
            elif live and identity != candidate:
                classification = "conflict"
        # Expiry does not erase accepted-work uncertainty. Cleanup may later
        # retire a receipt; its presence must still be considered here.
        if rows["onboarding_uncertain_receipts"] or any(row["state"] in _PROTECTED or row["state"] == "transfer"
                for row in rows["onboarding_workspace_recovery"]):
            classification = "indeterminate"
        unresolved_transfer = any(row["phase"] != "continue-ready" or row["result_json"] is None
            for row in rows["onboarding_transfer_intents"])
        if unresolved_transfer or any(row["consumed_at"] is None for row in rows["onboarding_transfer_relays"]) \
                or rows["capability_license_pending_deliveries"] \
                or any(row["completed_reference_id"] is None and row["terminal_json"] is None
                    for row in rows["capability_license_redemption_journal"]) \
                or any(row["activation_committed"] and row["completed_reference_id"] is None
                    for row in rows["capability_license_activation_deliveries"]):
            classification = "indeterminate"
        if rows["onboarding_transfer_proofs"] and not rows["onboarding_transfer_intents"]:
            classification = "indeterminate"
        if any(row["state"] == "submitted" for row in rows["provisioning_claim_submissions"]):
            classification = "indeterminate"
        if any(row["consumed_at"] is None and datetime.fromisoformat(row["expires_at"]).timestamp() > now
                for row in rows["first_enrollment_contexts"]):
            classification = "indeterminate"
        if any(not (classification == "same_target" and name == "worker:"+candidate
                or retained_record and name == "worker:"+record
                or selected is not None and name == "read:"+selected) for name in work):
            classification = "indeterminate"
        # A known differently scoped record never imports its navigation or
        # draft into the proposal. Current/protected records above still block.
        record_row = journeys.get(record)
        if record_row is not None and record != candidate and record != selected and not retained_record:
            if classification != "indeterminate" and datetime.fromisoformat(record_row["expires_at"]).timestamp() > now:
                classification = "conflict"
        snapshot = {"record": record, "candidate": candidate, "target": _generation(target),
            "classification": classification, "rows": rows, "work": list(work)}
        return {"classification": classification,
            "classification_generation": hashlib.sha256(canonical(snapshot).encode()).hexdigest(),
            "focus_eligible": classification == "conflict" and candidate_live and not candidate_uncertain and not work,
            "evidence": presentation_evidence(item, contexts.get(candidate))}

    def inspect_initial(self, connection, source, creator, *, work=()):
        """Classify a terminal unregistered source before explicit fresh admission."""
        from app.persistence.continuation_selection import ContinuationSelectionUnavailable
        require_journey(source)
        try:
            self.continuations.selection.select_in_transaction(connection)
        except ContinuationSelectionUnavailable as error:
            if error.reason != "registration_required":
                raise OnboardingJourneyUnavailable("workspace_changed") from None
        else:
            raise OnboardingJourneyUnavailable("workspace_changed")
        rows = {name: sorted([dict(row) for row in connection.execute("SELECT * FROM " + name)], key=canonical)
            for name in _TABLES}
        now = self.authentication._now()
        item = next((row for row in rows["onboarding_journeys"] if row["journey_id"] == source), None)
        if item is None:
            retired = next((row for row in rows["onboarding_presentation_retirements"]
                if row["journey_id"] == source and row["expires_at"] > now.timestamp()), None)
            item = None if retired is None else json.loads(retired["evidence_json"]).get("journey")
        if (item is None or item["kind"] != "initial-enrollment"
                or not (item["state"] == "expired" or item["state"] == "new"
                    and item["operation_id"] is None and datetime.fromisoformat(item["expires_at"]) <= now)):
            raise OnboardingJourneyUnavailable("workspace_changed")
        scope = json.loads(item.get("scope_json") or "{}")
        if scope.get("intended_creator_id", creator) != creator:
            raise OnboardingJourneyUnavailable("workspace_changed")
        if work or any(row["state"] in _PROTECTED | {"waiting", "transfer"}
                or datetime.fromisoformat(row["expires_at"]) > now and row["state"] not in {"expired", "revoked"}
                for row in rows["onboarding_journeys"]):
            raise OnboardingJourneyUnavailable("workspace_changed")
        if (rows["onboarding_uncertain_receipts"] or rows["onboarding_transfer_intents"]
                or rows["onboarding_transfer_relays"] or rows["onboarding_transfer_proofs"]
                or rows["capability_license_pending_deliveries"] or rows["first_enrollment_contexts"]
                or any(row["state"] in _PROTECTED | {"waiting", "transfer"} for row in rows["onboarding_workspace_recovery"])
                or any(json.loads(row["evidence_json"]).get("unresolved_work")
                    or (json.loads(row["evidence_json"]).get("journey") or {}).get("state") in _PROTECTED | {"waiting", "transfer"}
                    for row in rows["onboarding_presentation_retirements"])
                or any(row["completed_reference_id"] is None and row["terminal_json"] is None for row in rows["capability_license_redemption_journal"])
                or any(row["activation_committed"] and row["completed_reference_id"] is None for row in rows["capability_license_activation_deliveries"])):
            raise OnboardingJourneyUnavailable("workspace_changed")
        return {"source_journey_id": source, "creator_account_id": creator, "association_request_id": None,
            "initial": True, "classification_generation": hashlib.sha256(canonical({"source": source,
                "creator": creator, "rows": rows, "work": list(work)}).encode()).hexdigest()}

    def require_selection(self, connection, target, record, candidate, expected, *, work=(), retained_identifier=None):
        result = self.inspect(target, record, candidate, connection=connection, work=work,
            retained_identifier=retained_identifier)
        if result["classification"] not in {"same_target", "inactive"} or result["classification_generation"] != expected:
            raise OnboardingJourneyUnavailable("workspace_changed")
        return result

    def require_presentation(self, identifier, target, *, work=()):
        with self.authentication.database.transaction(immediate=False) as connection:
            row = self.presentation(identifier, connection=connection)
            if row is None:
                raise OnboardingJourneyUnavailable("workspace_unavailable")
            session = connection.execute("SELECT journey_id FROM provisioning_browser_sessions WHERE session_digest=?",
                (_digest(identifier),)).fetchone()
            if session is None:
                raise OnboardingJourneyUnavailable("workspace_unavailable")
            current = self.continuations.require_current(session["journey_id"], connection=connection)
            if _generation(current["target"]) != _generation(target):
                raise OnboardingJourneyUnavailable("workspace_changed")
            result = self.inspect(current["target"], row["record_journey_id"], row["candidate_journey_id"],
                connection=connection, selected=session["journey_id"], evidence=json.loads(row["candidate_evidence_json"]), work=work)
            if result["classification"] not in {"same_target", "inactive"}:
                raise OnboardingJourneyUnavailable("workspace_changed")
            return row


def public_supersession(row):
    return {name: row[name] for name in ("entry_id", "record_journey_id", "candidate_journey_id",
        "classification", "classification_generation", "presentation_generation")}
