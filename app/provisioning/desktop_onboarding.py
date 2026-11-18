"""Restricted desktop setup composition, with no runtime settings or analytics graph."""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import nullcontext
from datetime import datetime, timedelta
from uuid import uuid4

from app.onboarding import timing
from app.onboarding.contract import digest
from app.onboarding.coordinator import AcceptedContext, CommandRefused, DomainView, OperationResult
from app.persistence.continuation_selection import ContinuationSelectionUnavailable
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.finalize import FinalizationRefused, FinalizationRequest, verified_grant_bindings
from app.provisioning.owner_effects import OwnerEffects
from app.provisioning.progress import scoped_durable_provisioning_progress
from app.provisioning.state import local_setup_facts
from app.provisioning.setup_sources import SetupSourceLifecycle, PreparedSourceRecovery
from app.security.browser_authentication import BrowserAuthenticationRefused
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_key import InstallationKeyError

# Key preparation for browser access starts once its launch returns, or after this many seconds.
KEY_AFTER_LAUNCH_SECONDS = 2.0


class ProvisioningSetupOwner:
    # Finalization consumes only an existing, verified account approval.
    automatic_finalization = True

    def __init__(self, store, *, browser_tasks, ceremonies, initial, registered, key,
                 finalize, completion_ready, completed=lambda: None, source_replacement_clients=None):
        self.store, self.browser_tasks, self.ceremonies = store, browser_tasks, ceremonies
        self.initial, self.registered, self.key = initial, registered, key
        self.finalize, self.completion_ready, self.completed = finalize, completion_ready, completed
        self.journeys = OnboardingJourneyStore(store)
        self.contexts = InstallationContinuationStore(store)
        self.sources = SetupSourceLifecycle(store, self.journeys, self.contexts)
        self.source_replacements = PreparedSourceRecovery(self.sources, source_replacement_clients or {})
        self.commercial_reconciliation = None
        self.progress = scoped_durable_provisioning_progress(lambda: store)
        self._effects = OwnerEffects()
        self._source_replacement_effects = OwnerEffects()
        self._key_task, self._key_launch, self._key_closed = None, None, False
        # Effect threads recheck their original domain context at admission.
        from threading import RLock
        gate = RLock()
        self._effect_gate = gate
        if initial is not None and hasattr(initial, "bind_effect_gate"):
            initial.bind_effect_gate(gate, initial._current_journey)
        if registered is not None and hasattr(registered, "bind_effect_gate"):
            registered.bind_effect_gate(gate, registered._current)

    def _source(self, journey_id, *, connection=None):
        from contextlib import nullcontext
        if journey_id is None:
            return None
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            row = current.execute("SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?", (journey_id,)).fetchone()
        return None if row is None else self.sources.effective(row["source_journey_id"], connection=connection)

    def _journey(self, journey_id):
        if journey_id is None:
            return None
        return self.sources.current(journey_id)

    def _selection(self, journey, browser):
        progress = {"stage": "registration_required", "association_request_id": None, "creator_account_id": None}
        if journey is not None:
            progress = self.progress(journey.journey_id)
        creator = progress["creator_account_id"]
        observed = None if browser is None else browser["creator_account_id"]
        if creator is not None and observed is not None and creator != observed:
            return progress, creator, "account_mismatch"
        return progress, creator or observed, "none"

    def _work(self, *, exclude_source_replacement=False):
        result = list(self._effects.accepted_work())
        if not exclude_source_replacement:
            result.extend(self._source_replacement_effects.accepted_work())
        for owner in (self.initial, self.registered):
            if owner is not None:
                result.extend(getattr(owner, "accepted_work", lambda: ())())
                result.extend("worker:" + identifier for identifier, task in getattr(owner, "_tasks", {}).items()
                    if not task.done())
        return tuple(sorted(result))

    def _recovery(self, journey_id, source, *, connection=None):
        from contextlib import nullcontext
        if source is None:
            return None
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            if current.execute("SELECT 1 FROM desktop_setup_operations WHERE journey_id=? "
                    "AND action<>'browser.access' AND state IN ('accepted','waiting','outcome_unknown')", (journey_id,)).fetchone():
                return None
            try:
                return self._source_recovery_proposal(current, source)
            except ContinuationSelectionUnavailable as error:
                if error.reason != "registration_required":
                    return None
                previous = current.execute("SELECT action,owner_scope_json FROM desktop_setup_operations WHERE journey_id=? "
                    "AND ((action='account.connect' AND state='refused' AND reason='operation_expired') "
                    "OR action='browser.access') LIMIT 129", (journey_id,)).fetchall()
                scopes = []
                for row in previous:
                    scope = json.loads(row["owner_scope_json"])
                    if row["action"] == "browser.access":
                        # Browser retry admission can prune the expired account
                        # receipt. Its immutable scope retains the exact initial
                        # recovery proposal already validated at that admission.
                        proposal = scope.get("source_recovery")
                        if (not isinstance(proposal, dict) or set(proposal) != {
                                "source_journey_id", "classification_generation", "creator_account_id",
                                "association_request_id", "initial"} | ({"replacement"} if "replacement" in proposal else set())
                                or proposal["initial"] is not True
                                or proposal["association_request_id"] is not None
                                or proposal.get("source_journey_id") != scope["source_journey_id"]
                                or proposal.get("creator_account_id") != scope["creator_account_id"]):
                            continue
                    elif scope.get("source_replacement") is not None:
                        original = scope["source_replacement"]
                        if original["creator_account_id"] != scope["creator_account_id"]:
                            continue
                        outcome = self.source_replacements.reservations.outcome(current, original["replacement"]["operation_id"])
                        if outcome is None or outcome["status"] != "prepared":
                            scope = {**scope, "source_journey_id": original["source_journey_id"]}
                    scopes.append(scope)
                creators = {scope["creator_account_id"] for scope in scopes
                    if self.sources.effective(scope["source_journey_id"], connection=current) == source}
                if len(previous) > 128 or len(creators) != 1 or None in creators:
                    return None
                try:
                    return self._source_recovery_proposal(current, source, initial_creator=creators.pop())
                except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, HostedGrantUnavailable):
                    return None
            except (OnboardingJourneyUnavailable, HostedGrantUnavailable):
                return None

    def _source_recovery_proposal(self, connection, source, *, initial_creator=None):
        try:
            return self.sources.recovery_proposal(connection, source, work=self._work(), initial_creator=initial_creator).scope()
        except OnboardingJourneyUnavailable:
            prior = self.sources.resolve(source, connection=connection)
            if prior is None or prior.kind not in self.source_replacements.clients:
                raise
            if initial_creator is None:
                if prior.kind != "registered-continuation" or prior.binding is None:
                    raise
                creator = prior.binding.creator_account_id
            else:
                creator = initial_creator
            proposal = self.source_replacements.proposal(connection, source, creator, work=self._work())
            reservation = connection.execute("SELECT state FROM onboarding_source_replacement_reservations "
                "WHERE predecessor_operation_id=?", (proposal["replacement"]["operation_id"],)).fetchone()
            if reservation is not None and reservation["state"] != "reserved":
                raise OnboardingJourneyUnavailable("workspace_changed")
            return proposal

    def _pending_source_replacement(self, journey_id, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            rows = current.execute("SELECT operation_id,owner_scope_json FROM desktop_setup_operations WHERE journey_id=? "
                "AND action='account.connect' AND state IN ('accepted','waiting','outcome_unknown')", (journey_id,)).fetchall()
            if len(rows) != 1:
                return None
            selected = json.loads(rows[0]["owner_scope_json"])
            proposal = selected.get("source_replacement")
            if proposal is None:
                return None
            outcome = self.source_replacements.reservations.outcome(current, proposal["replacement"]["operation_id"])
            return {**selected, "operation_id": rows[0]["operation_id"]} if outcome is None or outcome["status"] != "prepared" else None

    def read(self, journey_id, *, actor=None, connection=None):
        reason = "none" if self.browser_tasks.extension_id is not None else "browser_unavailable"
        source_journey = self._source(journey_id, connection=connection)
        try:
            journey = self._journey(source_journey)
            if journey is not None and journey.state in {"expired", "revoked"}:
                journey = None
                reason = "authority_unavailable"
        except OnboardingJourneyUnavailable:
            journey = None
            reason = "authority_unavailable"
        browser = self.browser_tasks.progress(journey_id)
        progress, creator, selection_reason = self._selection(journey, browser)
        recovery = self._recovery(journey_id, source_journey, connection=connection) if journey is None else None
        pending_replacement = self._pending_source_replacement(journey_id, connection=connection) if journey is None else None
        if recovery is not None or pending_replacement is not None:
            reason = "none"
            creator = (recovery if recovery is not None else pending_replacement)["creator_account_id"]
            if browser is not None and browser["creator_account_id"] not in {None, creator}:
                selection_reason = "account_mismatch"
        if selection_reason != "none":
            reason = selection_reason
        facts = local_setup_facts(self.store, journey=None if journey is None else
            {"kind": journey.kind, "state": journey.state}, connection=connection)
        browser_state = None if browser is None else browser["state"]
        facts["browser_access"] = ("verified" if browser_state == "ready" else "paused" if browser_state == "paused"
                                   else "missing" if browser_state is None or browser_state.startswith("needs-") else "unknown")
        facts["configuration"] = "verified" if self.completion_ready() else "missing"
        facts["account"] = "missing" if progress["stage"] in {"registration_required", "creator_confirmation_required", "creator_approval_pending"} else "unknown"
        if journey is not None and progress["stage"] == "finalization_ready" and creator is not None:
            try:
                verified_grant_bindings(store=self.store, request=FinalizationRequest(progress["association_request_id"], creator),
                    trusted_journey_id=source_journey if journey.kind == "registered-continuation" else None)
                facts["account"] = "verified"
            except (FinalizationRefused, ValueError):
                reason = "authority_unavailable"
        if progress["stage"] == "recovery_required":
            reason = "authority_unavailable"
        key = self.store.installation_key_reference()
        scope = {"source_journey_id": source_journey, "journey_kind": None if journey is None else journey.kind,
            "journey_revision": None if journey is None else journey.revision,
            "installation_id": None if journey is None else journey.installation_id,
            "association_request_id": progress["association_request_id"], "creator_account_id": creator,
            "policy_revision": self.store.build_runtime_policy().authorization_epoch.value,
            "key": None if key is None else {"installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt},
            "browser": browser, "source_recovery": recovery,
            "desktop_decision": None if self.ceremonies is None or self.ceremonies.decisions is None else
                self.ceremonies.decisions.presentation(journey_id, "account.connect", connection=connection),
            "desktop_decision_status": None if self.ceremonies is None or self.ceremonies.decisions is None else
                self.ceremonies.decisions.status(journey_id, "account.connect", connection=connection),
            "desktop_authentication": None if self.ceremonies is None or self.ceremonies.authentication is None else
                self.ceremonies.authentication.presentation(journey_id, "account.connect", connection=connection)}
        if (recovery is not None and recovery.get("replacement") is not None) or pending_replacement is not None:
            # No authentication choice or old decision can bypass prepared-source recovery.
            scope.update(desktop_authentication=None, desktop_decision=None, desktop_decision_status=None)
        if pending_replacement is not None:
            scope["desktop_authentication"] = {"operation_id": pending_replacement["operation_id"],
                "browser_operation_id": None, "state": "replacement-pending",
                "choices": dict.fromkeys(("google", "microsoft-personal", "telegram", "tiktok", "default"), False),
                "actions": dict.fromkeys(("select", "cancel", "open_browser", "switch_account"), False),
                "authentication_attempt": None}
            return DomainView(facts, scope, reason, permitted_actions=frozenset())
        if (reason == "none" and self.ceremonies is not None and journey is not None
                and facts["account"] != "verified"):
            with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
                offer = self.ceremonies.replacement_offer(journey_id, connection=current)
                if offer is not None:
                    parent = current.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                        (offer["parent_operation_id"],)).fetchone()
                    try:
                        self._guard(dict(parent), connection=current)
                        original = json.loads(parent["owner_scope_json"])
                        binding = json.loads(self.ceremonies.row(parent["operation_id"], connection=current)["binding_json"])
                        if (all(original.get(name) == scope.get(name) for name in (
                                "source_journey_id", "installation_id", "creator_account_id"))
                                and scope["key"] is not None and all(binding[name] == scope["key"][name] for name in (
                                    "installation_key_id", "installation_key_jkt"))
                                and binding["intended_creator_id"] == scope["creator_account_id"]):
                            scope["ceremony_replacement"] = offer
                    except (HostedGrantUnavailable, OnboardingJourneyUnavailable, ValueError):
                        pass
        return DomainView(facts, scope, reason)

    def admit(self, connection, journey_id, *, action, scope, actor=None):
        if action != "browser.access" and connection.execute(
                "SELECT 1 FROM onboarding_transfer_intents WHERE expires_at>? AND phase<>'continue-ready'",
                (self.store._now().timestamp(),)).fetchone():
            raise CommandRefused("authority_unavailable")
        journey_id = journey_id or str(uuid4())
        connection.execute("INSERT OR IGNORE INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)",
            (journey_id, self.store._now().timestamp()))
        accepted = dict(scope)
        if action == "account.confirm" or action.startswith("auth."):
            if action == "account.confirm" and accepted["desktop_decision"] is None:
                raise CommandRefused("action_unavailable")
            return AcceptedContext(journey_id, accepted)
        if action != "browser.access":
            try:
                recovery = scope.get("source_recovery")
                if recovery is not None:
                    if action != "account.connect" or self._recovery(journey_id,
                            self._source(journey_id, connection=connection), connection=connection) != recovery:
                        raise OnboardingJourneyUnavailable("workspace_changed")
                    if recovery.get("replacement") is not None:
                        if scope.get("authentication_intent") is not None:
                            raise OnboardingJourneyUnavailable("workspace_changed")
                        def authorize(*, connection):
                            if self._recovery(journey_id, self._source(journey_id, connection=connection),
                                    connection=connection) != recovery:
                                raise OnboardingJourneyUnavailable("workspace_changed")
                        reservation = self.source_replacements.reserve(connection, recovery, authorize=authorize, work=self._work())
                        accepted.update(source_replacement=recovery, source_journey_id=str(uuid4()),
                            journey_kind=recovery["replacement"]["kind"], journey_revision=0,
                            installation_id=recovery["replacement"]["installation_id"],
                            association_request_id=recovery["association_request_id"],
                            successor_operation_id=reservation["successor_operation_id"])
                        return AcceptedContext(journey_id, accepted)
                    if recovery.get("initial"):
                        if self.sources.recovery_proposal(connection, recovery["source_journey_id"],
                                initial_creator=recovery["creator_account_id"], work=self._work()).scope() != recovery:
                            raise OnboardingJourneyUnavailable("workspace_changed")
                        source = self.contexts.admit_initial_desktop_recovery(connection, proposal=recovery, work=self._work())
                    else:
                        source = self.contexts.admit_desktop_recovery(connection, proposal=recovery,
                            reopen_key=self.key.reopen_existing, work=self._work())
                else:
                    source = self.contexts.admit_desktop_context(connection,
                        journey_id=self._source(journey_id, connection=connection), reopen_key=self.key.reopen_existing)
            except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, InstallationKeyError, HostedGrantUnavailable):
                raise CommandRefused("authority_unavailable") from None
            connection.execute("UPDATE desktop_setup_journeys SET source_journey_id=? WHERE journey_id=?", (source, journey_id))
            row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (source,)).fetchone()
            if (action == "account.connect" and row["kind"] == "initial-enrollment" and row["state"] == "new"
                    and row["operation_id"] is None and row["prepare_json"] is None):
                # Refresh only the local draft during explicit admission.
                connection.execute("UPDATE onboarding_journeys SET expires_at=? WHERE journey_id=?",
                    ((self.store._now() + timedelta(minutes=30)).isoformat(), source))
            accepted.update(source_journey_id=source, journey_kind=row["kind"],
                journey_revision=row["revision"], installation_id=row["installation_id"])
            if row["kind"] == "registered-continuation":
                current = self.contexts.require_current(source, connection=connection)
                if current["scope"]["creator_account_id"] != scope["creator_account_id"]:
                    raise CommandRefused("account_mismatch")
                accepted["association_request_id"] = current["association_request_id"]
        return AcceptedContext(journey_id, accepted)

    def observe(self, operation):
        scope = json.loads(operation["owner_scope_json"])
        if operation["action"] == "browser.access":
            observed = self.browser_tasks.progress(operation["journey_id"])
            if observed is not None and observed["state"] in {"ready", "paused"}:
                return OperationResult("completed")
            if operation["expires_at"] <= self.store._now().timestamp():
                # This task owns browser presentation only. Its expiry cannot
                # retire or replace a protected installation/account operation.
                return OperationResult("refused", "operation_expired")
        elif operation["action"] == "setup.finalize" and self._finalized(scope):
            return OperationResult("completed")
        elif operation["action"] == "account.connect":
            source = self.sources.resolve(scope["source_journey_id"])
            if source is not None and source.installation_id == scope["installation_id"]:
                if source.state in {"expired", "revoked"}:
                    return OperationResult("refused", "operation_expired" if source.state == "expired" else "authority_unavailable")
                if source.state == "completed":
                    bound = source.binding
                    if (bound is not None and bound.creator_account_id == scope["creator_account_id"]
                            and (scope["association_request_id"] is None
                                or bound.association_request_id == scope["association_request_id"])):
                        try:
                            verified_grant_bindings(store=self.store, request=FinalizationRequest(
                                bound.association_request_id, scope["creator_account_id"]))
                        except (FinalizationRefused, ValueError, KeyError):
                            pass
                        else:
                            return OperationResult("completed")
                if (source.state == "new" and source.preparation is None
                        and source.operation_id is None and not self._work()
                        and source.expires_at <= self.store._now()):
                    return OperationResult("refused", "operation_expired")
            creator = self._verified_creator(operation["journey_id"])
            if creator is not None and creator == scope["creator_account_id"]:
                return OperationResult("completed")
        return None

    def _verified_creator(self, journey_id):
        """The creator when `read` would show a verified account and installation, else None."""
        # Only the account and installation facts of the view, so a pass during sign-in reads little.
        source_journey = self._source(journey_id)
        try:
            journey = self._journey(source_journey)
        except OnboardingJourneyUnavailable:
            return None
        if journey is None or journey.state in {"expired", "revoked"}:
            return None
        progress, creator, _ = self._selection(journey, self.browser_tasks.read(journey_id))
        if progress["stage"] != "finalization_ready" or creator is None:
            return None
        try:
            verified_grant_bindings(store=self.store, request=FinalizationRequest(progress["association_request_id"], creator),
                trusted_journey_id=source_journey if journey.kind == "registered-continuation" else None)
        except (FinalizationRefused, ValueError):
            return None
        facts = local_setup_facts(self.store, journey={"kind": journey.kind, "state": journey.state})
        return creator if facts["installation"] == "verified" else None

    def _finalized(self, scope):
        if not self.completion_ready():
            return False
        return any((binding.installation_id, binding.creator_account_id, binding.association_request_id) == (
            scope["installation_id"], scope["creator_account_id"], scope["association_request_id"])
            for binding in self.store.authorized_account_bindings())

    def automatic_recovery(self, operation):
        if operation["action"].startswith("auth."):
            return self.ceremonies is not None and self.ceremonies.authentication is not None
        if operation["action"] == "account.confirm":
            return self.ceremonies is not None and self.ceremonies.decisions is not None
        if (operation["action"] != "account.connect" or self.ceremonies is None
                or self.initial is None or self.registered is None):
            return False
        selected = json.loads(operation["owner_scope_json"])
        replacement = selected.get("source_replacement")
        if replacement is not None:
            original = replacement["replacement"]["operation_id"]
            with self.store.database.read() as connection:
                outcome = self.source_replacements.reservations.outcome(connection, original)
                if outcome is None:
                    return connection.execute("SELECT 1 FROM onboarding_source_replacement_reservations "
                        "WHERE predecessor_operation_id=? AND state='send-claimed' AND NOT EXISTS "
                        "(SELECT 1 FROM onboarding_source_replacement_reconciliations WHERE predecessor_operation_id=?)",
                        (original, original)).fetchone() is not None
                if outcome["status"] != "prepared":
                    return False
        source = self.sources.resolve(selected["source_journey_id"])
        if source is None or source.state in {"expired", "revoked", "reauthentication-required"}:
            return False
        if source.expires_at > self.store._now():
            return True
        return selected["journey_kind"] == "initial-enrollment" and (source.state in {
            "preparing", "prepare-unknown", "unknown", "completing"}
            or self.ceremonies.row(operation["operation_id"]) is not None)

    def probe_interval(self, operation):
        authentication = None if self.ceremonies is None else self.ceremonies.authentication
        return None if authentication is None else authentication.probe_interval(operation["operation_id"])

    def confirmation_deadline(self, operation):
        authentication = None if self.ceremonies is None else self.ceremonies.authentication
        return None if authentication is None else authentication.confirmation_deadline(operation["operation_id"])

    def waiting_for_sign_in(self, operation):
        authentication = None if self.ceremonies is None else self.ceremonies.authentication
        return (operation["action"] == "account.connect" and authentication is not None
                and authentication.waiting_for_sign_in(operation["operation_id"]))

    async def probe(self, operation):
        authentication = self.ceremonies.authentication
        parent = authentication.parent_operation(operation["operation_id"])
        return await authentication.probe(operation["operation_id"],
            authorize=lambda: self._read_guard({"operation_id": parent}))

    def wait(self, operation):
        authentication = self.ceremonies.authentication
        parent = authentication.parent_operation(operation["operation_id"])
        return authentication.wait(operation["operation_id"],
            authorize=lambda: self._read_guard({"operation_id": parent}))

    async def prefetch(self, operation):
        authentication = self.ceremonies.authentication
        parent = authentication.parent_operation(operation["operation_id"])
        return await authentication.prefetch(operation["operation_id"],
            authorize=lambda: self._read_guard({"operation_id": parent}))

    async def reconcile_hint(self, operation):
        def guard():
            # Runs before each step of the hinted read, so its checks share one store connection.
            with self.store.database.read() as connection:
                self._guard(operation, connection=connection)
                parent = self.ceremonies.authentication.parent_operation(operation["operation_id"], connection=connection)
                self._read_guard({"operation_id": parent}, connection)
        self.hold_reads(operation["operation_id"])
        return await self.ceremonies.authentication.reconcile_task(operation["operation_id"], authorize=guard)

    def hold_reads(self, operation_id):
        """Fetch the parent's ceremony and decision read challenges while the hinted read runs."""
        with self.store.database.read() as connection:
            parent = self.ceremonies.authentication.parent_operation(operation_id, connection=connection)
            read_guard = lambda: self._read_guard({"operation_id": parent})
            self.ceremonies.hold_read(parent, authorize=read_guard, connection=connection)
            if self.ceremonies.decisions is not None:
                self.ceremonies.decisions.hold_read(parent, authorize=read_guard, connection=connection)

    def entry_served(self, operation_id):
        if self.ceremonies is None or self.ceremonies.authentication is None or self.initial is None:
            return
        parent = self.ceremonies.authentication.parent_operation(operation_id)
        with self.store.database.read() as connection:
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=? "
                "AND action='account.connect' AND state IN ('accepted','waiting','outcome_unknown')", (parent,)).fetchone()
        if operation is None:
            return
        selected = json.loads(operation["owner_scope_json"])
        if selected["journey_kind"] == "initial-enrollment":
            with timing.phase("source", "resume"):
                self.initial.resume(selected["source_journey_id"])

    def authorize_initial_scope(self, journey_id, scope):
        """Bind initial completion to the accepted intent, including its exact recovery."""
        with self.store.database.read() as connection:
            rows = connection.execute("SELECT owner_scope_json FROM desktop_setup_operations "
                "WHERE action='account.connect' AND state IN ('accepted','waiting','outcome_unknown') LIMIT 2").fetchall()
        if len(rows) != 1:
            raise HostedGrantUnavailable("Account connection is unavailable")
        selected = json.loads(rows[0]["owner_scope_json"])
        if (self.sources.effective(selected["source_journey_id"]) != journey_id
                or selected["creator_account_id"] != scope["intended_creator_id"]):
            raise HostedGrantUnavailable("Selected account changed")

    def settled(self, operation, result):
        if result.state == "completed":
            self.browser_tasks.complete(operation["operation_id"])
            if operation["action"] == "setup.finalize":
                self.completed()

    def _guard(self, operation, *, connection=None):
        if connection is None:
            # The source checks share one store connection. Each opening re-verifies the store files.
            with self.store.database.read() as connection:
                return self._guard(operation, connection=connection)
        selected = json.loads(operation["owner_scope_json"])
        row = self.sources.current(self.sources.effective(selected["source_journey_id"], connection=connection), connection=connection)
        if selected["installation_id"] is not None and row.installation_id != selected["installation_id"]:
            raise HostedGrantUnavailable("Selected installation changed")
        expected_key = selected["key"]
        current_key = self.store.installation_key_reference(connection=connection)
        if expected_key is not None and (current_key is None or (
                expected_key["installation_key_id"], expected_key["installation_key_jkt"]) != (
                current_key.installation_key_id, current_key.installation_key_jkt)):
            raise HostedGrantUnavailable("Selected installation key changed")
        if row.kind == "registered-continuation":
            current = self.contexts.require_current(selected["source_journey_id"], connection=connection)
            if (current["scope"]["creator_account_id"] != selected["creator_account_id"]
                    or current["association_request_id"] != selected["association_request_id"]):
                raise HostedGrantUnavailable("Selected account changed")
        if row.binding is not None:
            bound = row.binding
            if (selected["creator_account_id"] is not None
                    and bound.creator_account_id != selected["creator_account_id"]):
                raise HostedGrantUnavailable("Selected account changed")
        return row

    async def _terminal_initial_read(self, operation, result):
        snapshot = result.get("snapshot", {})
        if result.get("status") != "found" or snapshot.get("domain_status") != "unavailable" or "result" in snapshot:
            return None
        selected = json.loads(operation["owner_scope_json"])
        saved = self.ceremonies.row(operation["operation_id"])
        if saved is None:
            return None
        binding = json.loads(saved["binding_json"])
        source = self.sources.resolve(selected["source_journey_id"])
        if (selected["journey_kind"] != "initial-enrollment" or binding["purpose"] != "connect-account"
                or binding["source"]["kind"] != "initial" or source is None or source.state == "completed"
                or snapshot.get("ceremony_id") != saved["ceremony_id"]
                or snapshot.get("binding_digest") != digest(binding)
                or binding["source"]["operation_id"] != source.operation_id
                or binding["installation_id"] != source.installation_id
                or binding["installation_id"] != selected["installation_id"]
                or binding["intended_creator_id"] != selected["creator_account_id"]):
            return None
        if self._work() == ("worker:" + source.journey_id,) and source.state == "waiting":
            await self.initial.pause_terminal_wait(source.journey_id, source.operation_id)
        def authorize(connection):
            self._read_guard(operation)
            current = connection.execute("SELECT owner_scope_json,state FROM desktop_setup_operations WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()
            if (current is None or current["owner_scope_json"] != operation["owner_scope_json"]
                    or current["state"] not in {"accepted", "waiting", "outcome_unknown"} or self._work()
                    or connection.execute("SELECT 1 FROM onboarding_transfer_intents WHERE journey_id=? UNION ALL "
                        "SELECT 1 FROM onboarding_transfer_proofs WHERE journey_id=?", (source.journey_id, source.journey_id)).fetchone()):
                raise OnboardingJourneyUnavailable("journey_unavailable")
        with self._effect_gate, self.store.database.transaction() as connection:
            self.journeys.record_initial_terminal(connection, journey_id=selected["source_journey_id"],
                operation_id=source.operation_id, installation_id=source.installation_id,
                proof={"ceremony_id": result["snapshot"]["ceremony_id"],
                    "binding_digest": result["snapshot"]["binding_digest"], "domain_status": "unavailable"},
                authorize=lambda: authorize(connection))
        return OperationResult("refused", "operation_expired")

    def _recover_initial_source(self, operation):
        selected = json.loads(operation["owner_scope_json"])
        if selected["journey_kind"] != "initial-enrollment" or self.initial is None:
            return None
        original = selected["source_journey_id"]
        effective = self.sources.effective(original)
        source = self.sources.resolve(original)
        if source is None:
            return None
        if effective != original:
            return effective
        if (source.state not in {"preparing", "prepare-unknown", "unknown", "completing"}
                or source.expires_at > self.store._now()):
            return None
        def authorize(connection):
            saved = connection.execute("SELECT owner_scope_json,state FROM desktop_setup_operations WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()
            if (saved is None or saved["owner_scope_json"] != operation["owner_scope_json"]
                    or saved["state"] not in {"accepted", "waiting", "outcome_unknown"}
                    or source.installation_id != selected["installation_id"] or self._work()):
                raise OnboardingJourneyUnavailable("journey_unavailable")
            key = self.key.reopen_existing()
            bound = source.binding
            if bound:
                if (bound.creator_account_id != selected["creator_account_id"]
                        or bound.installation_key_jkt != key.installation_key_jkt):
                    raise OnboardingJourneyUnavailable("journey_unavailable")
            else:
                preparation = source.preparation
                public = {name: json.loads(key.public_key_jwk).get(name) for name in ("crv", "kty", "x", "y")}
                if (preparation is None or preparation.installation_key_id != key.installation_key_id
                        or preparation.public_key_jwk != json.dumps(public, sort_keys=True)):
                    raise OnboardingJourneyUnavailable("journey_unavailable")
        with self._effect_gate, self.store.database.transaction() as connection:
            recovered = self.journeys.recover_desktop_context(connection,
                previous_journey_id=original, authorize=lambda: authorize(connection))
        return recovered["journey_id"]

    def _replacement_command_guard(self, operation, *, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            self._guard(operation, connection=current)
            selected = json.loads(operation["owner_scope_json"])
            view = self.read(operation["journey_id"], connection=current)
            if (view.reason != "none" or view.facts["account"] == "verified"
                    or any(view.scope.get(name) != selected.get(name) for name in (
                        "source_journey_id", "installation_id", "creator_account_id", "association_request_id",
                        "key", "policy_revision"))):
                raise HostedGrantUnavailable("Replacement selection changed")

    def _source_replacement_guard(self, operation, *, connection):
        selected = json.loads(operation["owner_scope_json"])
        proposal = selected["source_replacement"]
        saved = connection.execute("SELECT owner_scope_json,state FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        root = connection.execute("SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?",
            (operation["journey_id"],)).fetchone()
        reservation = connection.execute("SELECT * FROM onboarding_source_replacement_reservations WHERE predecessor_operation_id=?",
            (proposal["replacement"]["operation_id"],)).fetchone()
        outcome = self.source_replacements.reservations.outcome(connection, proposal["replacement"]["operation_id"])
        expected_source = selected["source_journey_id"] if outcome is not None and outcome["status"] == "prepared" else proposal["source_journey_id"]
        key = self.store.installation_key_reference(connection=connection)
        browser = self.browser_tasks.progress(operation["journey_id"])
        if (saved is None or saved["state"] not in {"accepted", "waiting", "outcome_unknown"}
                or saved["owner_scope_json"] != operation["owner_scope_json"] or root is None
                or root["source_journey_id"] != expected_source or reservation is None
                or reservation["successor_operation_id"] != selected["successor_operation_id"]
                or key is None or selected["key"] != {"installation_key_id": key.installation_key_id,
                    "installation_key_jkt": key.installation_key_jkt}
                or self.store.read_epoch(connection).value != selected["policy_revision"]
                or browser is not None and browser["creator_account_id"] not in {None, selected["creator_account_id"]}):
            raise OnboardingJourneyUnavailable("workspace_changed")
        if outcome is not None and outcome["status"] == "prepared" and outcome["successor_journey_id"] != expected_source:
            raise OnboardingJourneyUnavailable("workspace_changed")

    def _execute_source_replacement(self, operation, *, reconcile):
        selected = json.loads(operation["owner_scope_json"])
        proposal = selected["source_replacement"]
        def authorize(*, connection):
            self._source_replacement_guard(operation, connection=connection)
        def adopt(*, connection, successor_journey_id):
            # The reservation and verified successor are committed together with
            # the existing desktop's source pointer, never on a renderer read.
            updated = connection.execute("UPDATE desktop_setup_journeys SET source_journey_id=?,revision=revision+1 "
                "WHERE journey_id=? AND source_journey_id=?", (successor_journey_id, operation["journey_id"],
                    proposal["source_journey_id"])).rowcount
            if updated != 1:
                raise OnboardingJourneyUnavailable("workspace_changed")
        return self.source_replacements.execute(proposal, successor_journey_id=selected["source_journey_id"],
            authorize=authorize, adopt=adopt, reconcile=reconcile,
            work=lambda: self._work(exclude_source_replacement=True))

    async def execute(self, operation, *, reconcile, actor=None):
        if operation["action"] == "account.connect" and json.loads(operation["owner_scope_json"]).get("source_replacement") is not None:
            observed = self.observe(operation)
            if observed is not None:
                return observed
            selected = json.loads(operation["owner_scope_json"])
            original = selected["source_replacement"]["replacement"]["operation_id"]
            outcome = await self._source_replacement_effects.run(self._execute_source_replacement, operation,
                reconcile=reconcile, journey_id=selected["source_replacement"]["source_journey_id"])
            if outcome is None and reconcile:
                with self.store.database.read() as connection:
                    reservation = connection.execute("SELECT state FROM onboarding_source_replacement_reservations "
                        "WHERE predecessor_operation_id=?", (original,)).fetchone()
                    if reservation is not None and reservation["state"] == "reserved":
                        # No send claim was made. A new explicit click can reuse the same reservation.
                        return OperationResult("refused", "operation_expired")
            if outcome is not None and outcome["status"] == "commercial-reconciliation-required" and self.commercial_reconciliation is not None:
                await self.commercial_reconciliation(selected["source_replacement"])
                return self.observe(operation) or OperationResult("outcome_unknown", "operation_unconfirmed")
            if outcome is None or outcome["status"] != "prepared":
                # Original accepted/committed or commercial results retain
                # priority and require their respective owners; never enroll again.
                return OperationResult("outcome_unknown", "operation_unconfirmed")
            # Continue the already accepted account action only with the verified
            # successor. It will expose the ordinary provider choices.
            return self.observe(operation) or await self._connect(operation, reconcile=True)
        stale = False
        recovered = None
        # The checks that precede the first await share one store connection.
        with self.store.database.shared_reads():
            with timing.phase("worker", "observe"):
                observed = self.observe(operation)
            if observed is None and operation["action"] == "account.connect":
                selected = json.loads(operation["owner_scope_json"])
                source = self.sources.resolve(selected["source_journey_id"])
                stale = (selected["journey_kind"] == "initial-enrollment" and source is not None
                         and source.expires_at <= self.store._now()
                         and self.ceremonies is not None and self.ceremonies.row(operation["operation_id"]) is not None)
                if not stale:
                    with timing.phase("worker", "recover"):
                        recovered = self._recover_initial_source(operation)
        if observed is not None:
            return observed
        if operation["action"] == "browser.access":
            self.browser_tasks.prepare(operation, "browser.access")
            if not reconcile:
                self._prepare_missing_key(operation["operation_id"])
            return OperationResult("waiting")
        if operation["action"] == "account.connect":
            if stale:
                result = await self.ceremonies.reconcile(operation["operation_id"], authorize=lambda: self._read_guard(operation),
                    adopt_authorize=lambda connection=None: self._read_guard(operation, connection=connection))
                if result.get("status") == "unavailable" and self.ceremonies.replacement_pending(operation["operation_id"]):
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                terminal = await self._terminal_initial_read(operation, result)
                if terminal is not None:
                    return terminal
                with timing.phase("worker", "recover"):
                    recovered = self._recover_initial_source(operation)
            if recovered is not None:
                # The original initial owner replays only its idempotent prepare
                # or reads its accepted completion receipt. No new ceremony.
                self.initial.resume(recovered)
                return self.observe(operation) or OperationResult("outcome_unknown", "operation_unconfirmed")
            return await self._connect(operation, reconcile=reconcile)
        if operation["action"] == "auth.select" and json.loads(operation["owner_scope_json"]).get("ceremony_replacement") is not None:
            if self.ceremonies is None:
                return OperationResult("refused", "authority_unavailable")
            parent = json.loads(operation["owner_scope_json"])["ceremony_replacement"]["parent_operation_id"]
            return await self.ceremonies.execute_replacement(operation, reconcile=reconcile,
                authorize=lambda connection=None: self._replacement_command_guard(operation, connection=connection),
                read_guard=lambda connection=None: self._read_guard({"operation_id": parent}, connection=connection))
        if operation["action"].startswith("auth.") and self.ceremonies is not None and self.ceremonies.authentication is not None:
            intent = json.loads(operation["owner_scope_json"])["authentication_intent"]
            parent = intent["parent_operation_id"]
            early = False
            if not reconcile and intent["verb"] in {"select", "switch"}:
                with timing.phase("worker", "task"):
                    early = self.browser_tasks.prepare_early(operation)
            try:
                result = await self.ceremonies.authentication.execute(operation, reconcile=reconcile,
                    authorize=lambda: self._guard(operation), read_guard=lambda: self._read_guard({"operation_id": parent}))
            except Exception:
                if early:
                    self._withdraw_early_task(operation)
                raise
            entry = self.ceremonies.browser_entry(operation["operation_id"])
            if result.state == "waiting" and entry is not None:
                timing.entry_ready(operation["operation_id"])
                self.browser_tasks.prepare(operation, "browser.authentication", deadline=datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")).timestamp())
            elif early and result.state != "waiting":
                self._withdraw_early_task(operation)
            return result
        if operation["action"] == "account.confirm" and self.ceremonies is not None and self.ceremonies.decisions is not None:
            parent = json.loads(operation["owner_scope_json"])["desktop_decision"]["operation_id"]
            return await self.ceremonies.decisions.execute(operation, reconcile=reconcile,
                authorize=lambda: self._guard(operation),
                read_guard=lambda: self._read_guard({"operation_id": parent}))
        if operation["action"] == "setup.finalize":
            scope = json.loads(operation["owner_scope_json"])
            self._guard(operation)
            if self.store._now().timestamp() >= operation["expires_at"]:
                return OperationResult("refused", "operation_expired")
            reason = await self._effects.run(self.finalize,
                association_request_id=scope["association_request_id"],
                detected_creator_account_id=scope["creator_account_id"], reported_platform_creator_id=None,
                **({"trusted_journey_id": scope["source_journey_id"]} if scope["journey_kind"] == "registered-continuation" else {}),
                journey_id=scope["source_journey_id"])
            if reason is not None:
                return OperationResult("refused", "authority_unavailable")
            if self._finalized(scope):
                return OperationResult("completed")
            return OperationResult("outcome_unknown", "operation_unconfirmed")
        return OperationResult("refused", "authority_unavailable")

    async def _connect(self, operation, *, reconcile):
        selected = json.loads(operation["owner_scope_json"])
        if self.ceremonies is None or self.initial is None or self.registered is None:
            return OperationResult("outcome_unknown", "authority_unavailable")
        def guard(connection=None):
            return self._guard(operation, connection=connection)
        saved = self.ceremonies.row(operation["operation_id"])
        selected = json.loads(operation["owner_scope_json"])
        source = self.initial if selected["journey_kind"] == "initial-enrollment" else self.registered
        intent = selected.get("authentication_intent")
        selecting = (saved is None and not reconcile and selected["journey_kind"] == "initial-enrollment"
                     and intent is not None and intent["verb"] == "select"
                     and intent["expected_previous_attempt_id"] is None and self.ceremonies.authentication is not None)
        early = deferred = False
        try:
            if saved is None:
                with self.store.database.shared_reads():
                    guard()
                    if self.store._now().timestamp() >= operation["expires_at"]:
                        return OperationResult("refused", "operation_expired")
                if selecting:
                    with timing.phase("worker", "task"):
                        early = await asyncio.to_thread(self.browser_tasks.prepare_early, operation)
                # Source services own their durable operation IDs and lost-send rules.
                # Retrying here retains that exact source, including on worker restart.
                with timing.phase("source", "prepare"):
                    if selecting:
                        # The source wait resumes after the selection, so its store reads
                        # and requests do not run ahead of the ceremony and selection sends.
                        await source.prepare(selected["source_journey_id"], resume=False)
                        deferred = True
                    else:
                        await source.prepare(selected["source_journey_id"])
                with self.store.database.shared_reads():
                    guard()
                    with timing.phase("binding", "binding"):
                        binding = self._binding(operation)
                await self.ceremonies.prepare(operation["operation_id"], binding, authorize=guard)
            else:
                result = await self.ceremonies.reconcile(operation["operation_id"], authorize=lambda: self._read_guard(operation),
                    adopt_authorize=lambda connection=None: self._read_guard(operation, connection=connection))
                if result.get("status") == "unavailable" and self.ceremonies.replacement_pending(operation["operation_id"]):
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                terminal = await self._terminal_initial_read(operation, result)
                if terminal is not None:
                    return terminal
            if not selecting and not (selected["journey_kind"] == "initial-enrollment" and intent is not None):
                with timing.phase("source", "resume"):
                    source.resume(selected["source_journey_id"])
            if selecting:
                authentication = await self.ceremonies.authentication.ensure_initial(operation, authorize=guard,
                    read_guard=lambda: self._read_guard(operation), fresh=True)
        except Exception:
            if deferred:
                with timing.phase("source", "resume"):
                    source.resume(selected["source_journey_id"])
            if early:
                self._withdraw_early_task(operation)
            raise
        if selecting:
            # Store reads between the selection reply and the task run off the event loop.
            entry = await asyncio.to_thread(self.ceremonies.browser_entry, operation["operation_id"])
            timing.observe("authentication-select", "entry-pending" if entry is None else "entry-open", "ok", 0)
            if entry is None and authentication is not None and authentication["state"] not in {"expired", "unavailable"}:
                # A selection reply can precede its browser step; one current read can open it at once.
                try:
                    authentication = await self.ceremonies.authentication.refresh(operation["operation_id"],
                        authorize=lambda: self._read_guard(operation))
                except (HostedGrantUnavailable, BrowserAuthenticationRefused):
                    pass
                entry = await asyncio.to_thread(self.ceremonies.browser_entry, operation["operation_id"])
                timing.observe("authentication-read", "entry-pending" if entry is None else "entry-open", "ok", 0)
            if entry is not None:
                timing.entry_ready(operation["operation_id"])
                await asyncio.to_thread(self.browser_tasks.prepare, operation, "browser.authentication",
                    deadline=datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")).timestamp())
            waiting = entry is not None or authentication is not None and authentication["state"] not in {"expired", "unavailable"}
            if early and not waiting:
                # A waiting selection can receive its ceremony on a later read, so its task stays.
                self._withdraw_early_task(operation)
            return OperationResult("waiting" if waiting else "outcome_unknown",
                                   "none" if waiting else "operation_unconfirmed")
        authentication = None
        if self.ceremonies.authentication is not None:
            authentication = await self.ceremonies.authentication.refresh(operation["operation_id"],
                authorize=lambda: self._read_guard(operation))
        decision = None if self.ceremonies.decisions is None else await self.ceremonies.decisions.refresh(
            operation["operation_id"], authorize=lambda: self._read_guard(operation))
        if self.ceremonies.authentication is not None and decision is not None and decision["state"] == "awaiting-authentication":
            authentication = await self.ceremonies.authentication.ensure_initial(operation, authorize=guard,
                read_guard=lambda: self._read_guard(operation), current=authentication)
        entry = self.ceremonies.browser_entry(operation["operation_id"])
        if entry is not None and decision is not None and decision["state"] == "awaiting-authentication":
            self.browser_tasks.prepare(operation, "browser.authentication", deadline=datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")).timestamp())
        elif decision is not None and decision["state"] in {"confirmation-required", "accepted"}:
            self.browser_tasks.complete(operation["operation_id"])
        observed = self.observe(operation)
        if observed is not None:
            return observed
        waiting = entry is not None or authentication is not None and authentication["state"] not in {"expired", "unavailable"}
        return OperationResult("waiting" if waiting else "outcome_unknown",
                               "none" if waiting else "operation_unconfirmed")

    def _withdraw_early_task(self, operation):
        # Only a task that never received a ceremony is withdrawn; failures leave it to expire.
        try:
            if self.ceremonies.browser_entry(operation["operation_id"]) is None:
                self.browser_tasks.withdraw_preparing(operation["operation_id"])
        except Exception:
            pass

    def _read_guard(self, operation, connection=None):
        """Retained proof reads may settle an expired presentation, never renew it."""
        if connection is None:
            # The ceremony and key checks share one store connection.
            with self.store.database.read() as connection:
                return self._read_guard(operation, connection)
        saved = self.ceremonies.row(operation["operation_id"], connection=connection)
        if saved is None:
            raise HostedGrantUnavailable("Browser ceremony is unavailable")
        binding = json.loads(saved["binding_json"])
        # Each proof exchange reopens the provider key and verifies it against this reference.
        key = self.store.installation_key_reference(connection=connection)
        if key is None or (binding["installation_key_id"], binding["installation_key_jkt"]) != (
                key.installation_key_id, key.installation_key_jkt):
            raise HostedGrantUnavailable("Browser ceremony key changed")

    def _binding(self, operation):
        selected = json.loads(operation["owner_scope_json"])
        journey = self._guard(operation)
        # The ceremony proof reopens the provider key and refuses a binding that differs from it.
        key = self.store.installation_key_reference()
        if key is None:
            raise HostedGrantUnavailable("Installation key is unavailable")
        if selected["creator_account_id"] is None:
            raise HostedGrantUnavailable("Browser account is unavailable")
        if journey.kind == "initial-enrollment":
            preparation = journey.preparation
            if preparation is None or preparation.profile is None:
                raise HostedGrantUnavailable("Initial preparation is unavailable")
            source = {"kind": "initial", "profile": preparation.profile, "operation_id": journey.operation_id,
                "request_digest": preparation.request_digest, "reference": journey.handoff_reference}
            expiry = journey.handoff_expires_at
            organization = {}
        else:
            row = self.contexts.require_current(journey.journey_id)
            request = self.registered._request(row)
            source = {"kind": "registered", "profile": request["profile"], "operation_id": row["operation_id"],
                "request_digest": digest(request), "reference": row["reference"], "continuation_id": row["continuation_id"]}
            expiry = row["provider_expires_at"]
            organization = {"organization_id": row["scope"]["organization_id"]}
        completion = ({"mode": "extension-auth-flow", "extension_id": selected["browser"]["extension_id"]}
                      if selected["browser"] is not None else {"mode": "system-browser"})
        return {"source": source, "purpose": "connect-account", "installation_id": journey.installation_id,
            "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt,
            "intended_creator_id": selected["creator_account_id"], "attempt_id": secrets.token_urlsafe(32),
            "completion": completion, "source_expires_at": expiry, **organization}

    def _prepare_missing_key(self, operation_id):
        """Create a missing installation key after the browser launch, never replacing an active one."""
        running = self._key_task is not None and not self._key_task.done()
        if self.key is None or self._key_closed or running:
            return
        signal = getattr(self.browser_tasks, "launch_signal", None)
        launched = asyncio.Event() if signal is None else signal(operation_id)
        self._key_launch = launched
        def prepare():
            try:
                if self.store.installation_key_reference() is None:
                    self.key.ensure_ready()
                self.browser_tasks.remember_progress(operation_id)
            except Exception:  # noqa: BLE001 - the account connection prepares the key again.
                return
        async def after_launch():
            try:
                await asyncio.wait_for(launched.wait(), KEY_AFTER_LAUNCH_SECONDS)
            except TimeoutError:
                pass
            if not self._key_closed:
                await asyncio.to_thread(prepare)
        self._key_task = asyncio.create_task(after_launch())

    async def stop(self):
        self._key_closed = True
        if self._key_launch is not None:
            self._key_launch.set()
        if self._key_task is not None:
            await asyncio.gather(asyncio.shield(self._key_task), return_exceptions=True)
        self._source_replacement_effects.close_admission()
        self._effects.close_admission()
        await self._source_replacement_effects.drain()
        await self._effects.drain()
        if self.initial is not None:
            await self.initial.stop()
        if self.registered is not None:
            await self.registered.stop()
