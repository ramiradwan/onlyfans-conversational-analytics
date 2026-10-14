"""Configured setup adapter; native possession never supplies operator identity."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from app.onboarding.coordinator import AcceptedContext, DomainView, OperationResult
from app.onboarding.readiness import creator_readiness
from app.persistence.onboarding import require_journey
from app.provisioning.state import local_setup_facts
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.analysis_authorization import current_analysis_readiness
from app.security.runtime_policy import AuthContext, RuntimePolicy
from app.security.webauthn import WebAuthnAuthorityPort, WebAuthnAuthorityResult


@dataclass(frozen=True)
class RuntimeActor:
    # This callback retains request authority in memory, never in the journal.
    policy: Callable[[], RuntimePolicy]


class RuntimeSetupOwner:
    def __init__(self, store, *, browser_tasks, activation, pairing=None, setup_owner=None):
        self.store, self.browser_tasks = store, browser_tasks
        self.activation, self.pairing = activation, pairing
        self.setup_owner = setup_owner
        self.authority = WebAuthnAuthorityPort(store, clock=store._now)

    def _local_authority(self, policy, *, connection=None):
        decision = self.authority.session_authority(policy, connection=connection)
        if decision.result is WebAuthnAuthorityResult.CREDENTIAL_MISSING:
            decision = self.authority.registration_authority(policy, connection=connection)
        if decision.result is not WebAuthnAuthorityResult.AUTHORIZED:
            return None
        return decision.authority

    def read(self, journey_id, *, actor=None, connection=None):
        policy = self.store.build_runtime_policy() if actor is None else actor.policy()
        local = self._local_authority(policy, connection=connection)
        # This projection cannot create a session or authorize a new command.
        identity = None if local is None else AuthContext(local.principal_id, local.creator_account_id, local.role)
        browser = self.browser_tasks.read(journey_id)
        readiness = None if identity is None else current_analysis_readiness(self.store, AuthContext(
            identity.principal_id, identity.creator_account_id, "agent"))
        facts = local_setup_facts(self.store, identity=identity, connection=connection, readiness=readiness)
        bindings = self.store.authorized_account_bindings()
        selected = next((binding for binding in bindings if identity is not None
            and binding.creator_account_id == identity.creator_account_id and binding.revoked_at is None), None)
        facts["configuration"] = "verified"
        facts["account"] = "verified" if selected is not None else "unknown"
        facts["browser_access"] = ("verified" if browser is not None and browser["state"] == "ready"
            else "paused" if browser is not None and browser["state"] == "paused" else "unknown")
        key = self.store.installation_key_reference()
        authority = (None if local is None else self.store.build_runtime_policy_from_grants(
            identity, local.grant_reference_ids).identity_authority)
        from contextlib import nullcontext
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            setup = current.execute("SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?", (journey_id,)).fetchone()
        scope = {"source_journey_id": None if setup is None else setup["source_journey_id"],
            "creator_account_id": None if selected is None else selected.creator_account_id,
            "association_request_id": None if selected is None else selected.association_request_id,
            "installation_id": None if selected is None else selected.installation_id,
            "key": None if key is None else {"installation_key_id": key.installation_key_id,
                "installation_key_jkt": key.installation_key_jkt},
            "policy_revision": policy.authorization_epoch.value, "browser": browser,
            "actor": None if policy.identity is None else {"principal_id": policy.identity.principal_id,
                "creator_account_id": policy.identity.creator_account_id, "role": policy.identity.role},
            "organization_id": None if authority is None else authority.organization_id,
            "desktop_decision": None if self.activation is None else self.activation.decision(journey_id, connection=connection),
            "desktop_decision_status": None if self.activation is None else self.activation.decision_status(journey_id, connection=connection),
            "desktop_authentication": None if self.activation is None else self.activation.authentication(journey_id, connection=connection)}
        actions = set()
        if (identity is not None and policy.identity is not None and selected is not None
                and policy.identity.creator_account_id == selected.creator_account_id):
            if policy.identity.role in {"creator", "operator"}:
                actions.add("browser.access")
            if self.pairing is not None and policy.identity.role in {"creator", "operator"}:
                actions.add("browser.pair")
            if self.activation is not None and policy.identity.role == "creator":
                actions.add("activation.review")
                actions.add("activation.confirm")
                actions.update(("auth.select", "auth.cancel", "auth.switch"))
        return DomainView(facts, scope, reason="authority_unavailable" if local is None else "none",
            permitted_actions=frozenset(actions), analysis_readiness=creator_readiness(policy, identity, readiness))

    def admit(self, connection, journey_id, *, action, scope, actor=None):
        # Configuration must retain the setup journal across the controlled
        # restart. This path cannot manufacture an initial installation journey.
        require_journey(journey_id)
        if connection.execute("SELECT 1 FROM desktop_setup_journeys WHERE journey_id=?", (journey_id,)).fetchone() is None:
            raise HostedGrantUnavailable("Saved setup is unavailable")
        if actor is None or actor.policy().identity is None:
            raise HostedGrantUnavailable("Operator authentication is required")
        return AcceptedContext(journey_id, dict(scope))

    def authorize(self, operation, actor, *, connection=None):
        if actor is None:
            raise HostedGrantUnavailable("Operator authentication is required")
        expected = json.loads(operation["owner_scope_json"])
        view = self.read(operation["journey_id"], actor=actor, connection=connection)
        for name in ("actor", "creator_account_id", "association_request_id", "installation_id", "key", "organization_id", "source_journey_id"):
            if view.scope[name] != expected[name]:
                raise HostedGrantUnavailable("Setup authority changed")
        if operation["action"] not in view.permitted_actions:
            raise HostedGrantUnavailable("Setup authority is unavailable")
        return actor.policy()

    def observe(self, operation):
        if self._runtime_access(operation):
            selected = json.loads(operation["owner_scope_json"])
            browser = self.browser_tasks.read(operation["journey_id"], operation_id=operation["operation_id"])
            if browser is not None and browser["state"] in {"ready", "paused"}:
                return OperationResult("completed" if browser["creator_account_id"] == selected["creator_account_id"] else "refused",
                    "none" if browser["creator_account_id"] == selected["creator_account_id"] else "operation_refused")
            if operation["expires_at"] <= self.store._now().timestamp():
                return OperationResult("refused", "operation_expired")
            return None
        if self.setup_owner is not None and operation["action"] in {"browser.access", "account.connect", "account.confirm", "setup.finalize"}:
            return self.setup_owner.observe(operation)
        if operation["action"] == "browser.pair" and self.pairing is not None:
            return self.pairing.observe(operation)
        selected = json.loads(operation["owner_scope_json"])
        view = self.read(operation["journey_id"])
        if view.scope["creator_account_id"] != selected["creator_account_id"]:
            return None
        if operation["action"] == "setup.finalize":
            if (view.facts["account"] == "verified" and view.scope["installation_id"] == selected["installation_id"]
                    and view.scope["association_request_id"] == selected["association_request_id"]):
                return OperationResult("completed")
        if (operation["action"] == "activation.review" and self.activation is not None
                and view.facts["activation"] == "verified" and self.activation.outcome_is_known(operation["operation_id"])):
            return OperationResult("completed")
        return None

    async def execute(self, operation, *, reconcile, actor=None):
        observed = self.observe(operation)
        if observed is not None:
            return observed
        if self._runtime_access(operation):
            if actor is None:
                return OperationResult("outcome_unknown", "operation_unconfirmed")
            self.authorize(operation, actor)
            self.browser_tasks.prepare(operation, "browser.access")
            return OperationResult("waiting")
        if operation["action"].startswith("auth."):
            if "actor" not in json.loads(operation["owner_scope_json"]) and self.setup_owner is not None:
                return await self.setup_owner.execute(operation, reconcile=reconcile)
            if self.activation is not None:
                return await self.activation.authenticate(operation, reconcile=reconcile,
                    authorize=lambda: self.authorize(operation, actor))
        if self.setup_owner is not None and operation["action"] in {"browser.access", "account.connect", "account.confirm", "setup.finalize"}:
            return await self.setup_owner.execute(operation, reconcile=reconcile)
        if operation["action"] == "activation.review" and self.activation is not None:
            return await self.activation.execute(operation,
                authorize=lambda connection=None: self.authorize(operation, actor, connection=connection), can_prepare=actor is not None)
        if operation["action"] == "activation.confirm" and self.activation is not None:
            return await self.activation.confirm(operation, reconcile=reconcile,
                authorize=lambda: self.authorize(operation, actor))
        if operation["action"] == "browser.pair" and self.pairing is not None:
            return await self.pairing.execute(operation,
                authorize=lambda connection=None: self.authorize(operation, actor, connection=connection), can_prepare=actor is not None)
        return OperationResult("outcome_unknown", "operation_unconfirmed")

    def automatic_recovery(self, operation):
        if operation["action"].startswith("auth."):
            return True
        if self.activation is not None and operation["action"] in {"activation.review", "activation.confirm"}:
            return (operation["action"] == "activation.confirm"
                or self.activation.ceremonies.row(operation["operation_id"]) is not None)
        return (self.setup_owner is not None and operation["action"] in {"account.connect", "account.confirm"}
                and self.setup_owner.automatic_recovery(operation))

    @staticmethod
    def _runtime_access(operation):
        # An initial setup operation retains its original owner across restart.
        # Runtime access was accepted with an authenticated actor in its scope.
        return operation["action"] == "browser.access" and "actor" in json.loads(operation["owner_scope_json"])

    def settled(self, operation, result):
        if result.state == "completed":
            self.browser_tasks.complete(operation["operation_id"])

    async def stop(self):
        if self.setup_owner is not None:
            await self.setup_owner.stop()
        if self.activation is not None:
            await self.activation.stop()
        if self.pairing is not None:
            await self.pairing.stop()
