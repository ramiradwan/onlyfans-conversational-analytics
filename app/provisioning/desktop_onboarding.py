"""Restricted desktop setup composition, with no runtime settings or analytics graph."""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from uuid import uuid4

from app.onboarding.contract import digest
from app.onboarding.coordinator import AcceptedContext, CommandRefused, DomainView, OperationResult
from app.persistence.continuation_selection import ContinuationSelectionUnavailable
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.finalize import FinalizationRefused, FinalizationRequest, verified_grant_bindings
from app.provisioning.owner_effects import OwnerEffects
from app.provisioning.progress import scoped_durable_provisioning_progress
from app.provisioning.state import local_setup_facts
from app.provisioning.setup_sources import SetupSourceLifecycle
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_key import InstallationKeyError


class ProvisioningSetupOwner:
    def __init__(self, store, *, browser_tasks, ceremonies, initial, registered, key,
                 finalize, completion_ready, completed=lambda: None):
        self.store, self.browser_tasks, self.ceremonies = store, browser_tasks, ceremonies
        self.initial, self.registered, self.key = initial, registered, key
        self.finalize, self.completion_ready, self.completed = finalize, completion_ready, completed
        self.journeys = OnboardingJourneyStore(store)
        self.contexts = InstallationContinuationStore(store)
        self.sources = SetupSourceLifecycle(store, self.journeys, self.contexts)
        self.progress = scoped_durable_provisioning_progress(lambda: store)
        self._effects = OwnerEffects()
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

    def _work(self):
        result = list(self._effects.accepted_work())
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
                return self.sources.recovery_proposal(current, source, work=self._work()).scope()
            except ContinuationSelectionUnavailable as error:
                if error.reason != "registration_required":
                    return None
                previous = current.execute("SELECT owner_scope_json FROM desktop_setup_operations WHERE journey_id=? "
                    "AND action='account.connect' AND state='refused' AND reason='operation_expired' LIMIT 129", (journey_id,)).fetchall()
                scopes = [json.loads(row["owner_scope_json"]) for row in previous]
                creators = {scope["creator_account_id"] for scope in scopes
                    if self.sources.effective(scope["source_journey_id"], connection=current) == source}
                if len(previous) > 128 or len(creators) != 1 or None in creators:
                    return None
                try:
                    return self.sources.recovery_proposal(current, source,
                        initial_creator=creators.pop(), work=self._work()).scope()
                except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable):
                    return None
            except OnboardingJourneyUnavailable:
                return None

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
        browser = self.browser_tasks.read(journey_id)
        progress, creator, selection_reason = self._selection(journey, browser)
        recovery = self._recovery(journey_id, source_journey, connection=connection) if journey is None else None
        if recovery is not None:
            reason = "none"
            creator = recovery["creator_account_id"]
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
            except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, InstallationKeyError):
                raise CommandRefused("authority_unavailable") from None
            connection.execute("UPDATE desktop_setup_journeys SET source_journey_id=? WHERE journey_id=?", (source, journey_id))
            row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (source,)).fetchone()
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
            observed = self.browser_tasks.read(operation["journey_id"])
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
            view = self.read(operation["journey_id"])
            if (view.facts["account"] == "verified" and view.facts["installation"] == "verified"
                    and view.scope["creator_account_id"] == scope["creator_account_id"]):
                return OperationResult("completed")
        return None

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
        source = self.sources.resolve(selected["source_journey_id"])
        if source is None or source.state in {"expired", "revoked", "reauthentication-required"}:
            return False
        if source.expires_at > self.store._now():
            return True
        return selected["journey_kind"] == "initial-enrollment" and (source.state in {
            "preparing", "prepare-unknown", "unknown", "completing"}
            or self.ceremonies.row(operation["operation_id"]) is not None)

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
        selected = json.loads(operation["owner_scope_json"])
        row = self.sources.current(self.sources.effective(selected["source_journey_id"], connection=connection), connection=connection)
        if selected["installation_id"] is not None and row.installation_id != selected["installation_id"]:
            raise HostedGrantUnavailable("Selected installation changed")
        expected_key = selected["key"]
        current_key = self.store.installation_key_reference()
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

    async def execute(self, operation, *, reconcile, actor=None):
        observed = self.observe(operation)
        if observed is not None:
            return observed
        if operation["action"] == "browser.access":
            self.browser_tasks.prepare(operation, "browser.access")
            return OperationResult("waiting")
        if operation["action"] == "account.connect":
            selected = json.loads(operation["owner_scope_json"])
            source = self.sources.resolve(selected["source_journey_id"])
            if (selected["journey_kind"] == "initial-enrollment" and source is not None
                    and source.expires_at <= self.store._now()
                    and self.ceremonies is not None and self.ceremonies.row(operation["operation_id"]) is not None):
                result = await self.ceremonies.reconcile(operation["operation_id"], authorize=lambda: self._read_guard(operation))
                terminal = await self._terminal_initial_read(operation, result)
                if terminal is not None:
                    return terminal
            recovered = self._recover_initial_source(operation)
            if recovered is not None:
                # The original initial owner replays only its idempotent prepare
                # or reads its accepted completion receipt. No new ceremony.
                self.initial.resume(recovered)
                return self.observe(operation) or OperationResult("outcome_unknown", "operation_unconfirmed")
            return await self._connect(operation, reconcile=reconcile)
        if operation["action"].startswith("auth.") and self.ceremonies is not None and self.ceremonies.authentication is not None:
            parent = json.loads(operation["owner_scope_json"])["authentication_intent"]["parent_operation_id"]
            result = await self.ceremonies.authentication.execute(operation, reconcile=reconcile,
                authorize=lambda: self._guard(operation), read_guard=lambda: self._read_guard({"operation_id": parent}))
            entry = self.ceremonies.browser_entry(operation["operation_id"])
            if result.state == "waiting" and entry is not None:
                self.browser_tasks.prepare(operation, "browser.authentication", deadline=datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")).timestamp())
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
        if saved is None:
            guard()
            if self.store._now().timestamp() >= operation["expires_at"]:
                return OperationResult("refused", "operation_expired")
            # Source services own their durable operation IDs and lost-send rules.
            # Retrying here retains that exact source, including on worker restart.
            await source.prepare(selected["source_journey_id"])
            guard()
            binding = self._binding(operation)
            await self.ceremonies.prepare(operation["operation_id"], binding, authorize=guard)
        else:
            result = await self.ceremonies.reconcile(operation["operation_id"], authorize=lambda: self._read_guard(operation))
            terminal = await self._terminal_initial_read(operation, result)
            if terminal is not None:
                return terminal
        source.resume(selected["source_journey_id"])
        authentication = None
        if self.ceremonies.authentication is not None:
            authentication = await self.ceremonies.authentication.refresh(operation["operation_id"],
                authorize=lambda: self._read_guard(operation))
        decision = None if self.ceremonies.decisions is None else await self.ceremonies.decisions.refresh(
            operation["operation_id"], authorize=lambda: self._read_guard(operation))
        if self.ceremonies.authentication is not None and decision is not None and decision["state"] == "awaiting-authentication":
            authentication = await self.ceremonies.authentication.ensure_initial(operation, authorize=guard,
                read_guard=lambda: self._read_guard(operation))
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

    def _read_guard(self, operation):
        """Retained proof reads may settle an expired presentation, never renew it."""
        saved = self.ceremonies.row(operation["operation_id"])
        if saved is None:
            raise HostedGrantUnavailable("Browser ceremony is unavailable")
        binding = json.loads(saved["binding_json"])
        key = self.key.reopen_existing()
        if (binding["installation_key_id"], binding["installation_key_jkt"]) != (key.installation_key_id, key.installation_key_jkt):
            raise HostedGrantUnavailable("Browser ceremony key changed")

    def _binding(self, operation):
        selected = json.loads(operation["owner_scope_json"])
        journey = self._guard(operation)
        key = self.key.reopen_existing()
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

    async def stop(self):
        self._effects.close_admission()
        await self._effects.drain()
        if self.initial is not None:
            await self.initial.stop()
        if self.registered is not None:
            await self.registered.stop()
