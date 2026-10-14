"""Accepted desktop activation delegates commercial authority to existing owners."""
from __future__ import annotations

import asyncio
import json
import secrets

from app.onboarding.contract import digest
from app.onboarding.coordinator import OperationResult
from app.persistence.auth import _new_uuid7
from app.persistence.installation_continuation import InstallationContinuationStore
from app.provisioning.owner_effects import OwnerEffects
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp


class DesktopActivation:
    def __init__(self, store, *, key, registered, ceremonies, browser_tasks, redemption):
        self.store, self.key, self.registered = store, key, registered
        self.ceremonies, self.browser_tasks, self.redemption = ceremonies, browser_tasks, redemption
        self.contexts = InstallationContinuationStore(store)
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()

    def _source(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?", (operation_id,)).fetchone()
        return None if row is None else dict(row)

    def outcome_is_known(self, operation_id):
        row = self.ceremonies.row(operation_id)
        # Before ceremony reservation no commercial send was possible. Once
        # reserved, only this ceremony's verified committed status resolves it.
        return row is None or row["domain_status"] == "committed"

    def decision(self, journey_id, *, connection=None):
        return None if self.ceremonies.decisions is None else self.ceremonies.decisions.presentation(
            journey_id, "activation.review", connection=connection)

    def decision_status(self, journey_id, *, connection=None):
        return None if self.ceremonies.decisions is None else self.ceremonies.decisions.status(
            journey_id, "activation.review", connection=connection)

    def authentication(self, journey_id, *, connection=None):
        return None if self.ceremonies.authentication is None else self.ceremonies.authentication.presentation(
            journey_id, "activation.review", connection=connection)

    async def authenticate(self, operation, *, authorize, reconcile):
        if self.ceremonies.authentication is None:
            return OperationResult("refused", "authority_unavailable")
        result = await self.ceremonies.authentication.execute(operation, authorize=authorize,
            read_guard=lambda: self._proof_read_guard(operation), reconcile=reconcile)
        entry = self.ceremonies.browser_entry(operation["operation_id"])
        if result.state == "waiting" and entry is not None:
            self.browser_tasks.prepare(operation, "browser.authentication", deadline=timestamp(entry["expires_at"]).timestamp())
        return result

    async def confirm(self, operation, *, authorize, reconcile):
        if self.ceremonies.decisions is None:
            return OperationResult("refused", "authority_unavailable")
        return await self.ceremonies.decisions.execute(operation, authorize=authorize,
            read_guard=lambda: self._proof_read_guard(operation), reconcile=reconcile)

    def _reserve(self, operation, authorize):
        selected = json.loads(operation["owner_scope_json"])
        key = self.key.reopen_existing()
        with self.store.database.transaction() as connection:
            authorize(connection=connection)
            existing = connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?", (operation["operation_id"],)).fetchone()
            if existing is not None:
                return dict(existing)
            if self.store._now().timestamp() >= operation["expires_at"]:
                raise HostedGrantUnavailable("Activation admission expired")
            row = self.contexts.runtime_desktop_context(
                creator_account_id=selected["creator_account_id"], authorize=lambda: authorize(connection=connection),
                reopened_key=key, connection=connection)
            connection.execute("INSERT INTO desktop_activation_sources VALUES (?,?,?)",
                (operation["operation_id"], row["journey_id"], _new_uuid7(self.store._now())))
            return dict(connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?", (operation["operation_id"],)).fetchone())

    def _binding(self, operation, source):
        selected = json.loads(operation["owner_scope_json"])
        row = self.contexts.require_current(source["source_journey_id"])
        request = self.registered._request(row)
        key = self.key.reopen_existing()
        if (row["scope"]["creator_account_id"] != selected["creator_account_id"]
                or row["scope"]["installation_id"] != selected["installation_id"]
                or row["scope"]["organization_id"] != selected["organization_id"]
                or (key.installation_key_id, key.installation_key_jkt) != (
                    selected["key"]["installation_key_id"], selected["key"]["installation_key_jkt"])):
            raise HostedGrantUnavailable("Activation scope changed")
        completion = ({"mode": "system-browser"} if self.browser_tasks.extension_id is None else
            {"mode": "extension-auth-flow", "extension_id": self.browser_tasks.extension_id})
        return {"source": {"kind": "registered", "profile": request["profile"], "operation_id": row["operation_id"],
                "request_digest": digest(request), "reference": row["reference"], "continuation_id": row["continuation_id"]},
            "purpose": "activate-complimentary", "activation_operation_id": source["activation_operation_id"],
            "installation_id": selected["installation_id"], "organization_id": selected["organization_id"],
            "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt,
            "intended_creator_id": selected["creator_account_id"], "attempt_id": secrets.token_urlsafe(32),
            "completion": completion, "source_expires_at": row["provider_expires_at"]}

    def _proof_read_guard(self, operation):
        # A retained installation proof can read the accepted operation after
        # presentation/session expiry; it cannot create another commercial action.
        selected = json.loads(operation["owner_scope_json"])
        key = self.key.reopen_existing()
        if (key.installation_key_id, key.installation_key_jkt) != (
                selected["key"]["installation_key_id"], selected["key"]["installation_key_jkt"]):
            raise HostedGrantUnavailable("Activation key changed")

    def _delivery_binding(self, operation):
        selected = json.loads(operation["owner_scope_json"])
        return {"organization_id": selected["organization_id"], "installation_id": selected["installation_id"],
            "installation_key_id": selected["key"]["installation_key_id"],
            "installation_key_jkt": selected["key"]["installation_key_jkt"],
            "creator_account_id": selected["creator_account_id"]}

    async def execute(self, operation, *, authorize, can_prepare):
        async with self._lock:
            row = self.ceremonies.row(operation["operation_id"])
            if row is None:
                if not can_prepare:
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                authorize()
                if self.store._now().timestamp() >= operation["expires_at"]:
                    return OperationResult("refused", "operation_expired")
                source = self._source(operation["operation_id"]) or self._reserve(operation, authorize)
                if not self.redemption.prepare_activation(activation_operation_id=source["activation_operation_id"],
                        binding=self._delivery_binding(operation)):
                    return OperationResult("outcome_unknown", "authority_unavailable")
                await self.registered.prepare(source["source_journey_id"], authorize=authorize)
                authorize()
                response = await self.ceremonies.prepare(operation["operation_id"], self._binding(operation, source), authorize=authorize)
            else:
                if row["domain_status"] == "committed":
                    return await self._redeem(operation, None)
                response = await self.ceremonies.reconcile(operation["operation_id"],
                    authorize=lambda: self._proof_read_guard(operation))
                if response.get("status") != "found":
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
            if response.get("status") == "found":
                snapshot = response["snapshot"]
                result = snapshot.get("result")
                if result is not None and result["kind"] == "activation-redemption":
                    return await self._redeem(operation, result)
                if snapshot["domain_status"] == "committed":
                    return await self._redeem(operation, None)
                if snapshot["domain_status"] == "revoked":
                    return OperationResult("refused", "operation_refused")
            authentication = None
            if self.ceremonies.authentication is not None:
                authentication = await self.ceremonies.authentication.refresh(operation["operation_id"],
                    authorize=lambda: self._proof_read_guard(operation))
            decision = None if self.ceremonies.decisions is None else await self.ceremonies.decisions.refresh(
                operation["operation_id"], authorize=lambda: self._proof_read_guard(operation))
            if decision is not None and decision["state"] in {"confirmation-required", "accepted"}:
                self.browser_tasks.complete(operation["operation_id"])
                if decision["state"] == "accepted":
                    return await self._redeem(operation, None)
                return OperationResult("waiting")
            if decision is not None and decision["state"] == "unavailable":
                # Unavailable does not establish eligibility, refusal or rollback.
                # Keep reading this accepted operation so an existing offer or
                # permitted preparation retry can become visible automatically.
                row = self.ceremonies.row(operation["operation_id"])
                deadlines = [operation["expires_at"]]
                if row is not None:
                    binding = json.loads(row["binding_json"])
                    deadlines.append(timestamp(binding["source_expires_at"]).timestamp())
                    if row["provider_expires_at"] is not None:
                        deadlines.append(timestamp(row["provider_expires_at"]).timestamp())
                if self.store._now().timestamp() >= min(deadlines):
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                return OperationResult("waiting", "activation_unavailable")
            if authentication is not None:
                return OperationResult("waiting")
            entry = self.ceremonies.browser_entry(operation["operation_id"])
            if entry is not None and self.store._now().timestamp() < operation["expires_at"]:
                self.browser_tasks.prepare(operation, "browser.authentication", deadline=timestamp(entry["expires_at"]).timestamp())
                return OperationResult("waiting")
            return OperationResult("outcome_unknown", "operation_unconfirmed")

    async def _redeem(self, operation, result):
        original = self._source(operation["operation_id"])
        if original is None or result is not None and result["activation_operation_id"] != original["activation_operation_id"]:
            raise HostedGrantUnavailable("Activation result changed")
        self._proof_read_guard(operation)
        receipt = await self._effects.run(self.redemption.resume_activation,
            activation_operation_id=original["activation_operation_id"], binding=self._delivery_binding(operation),
            initial_redemption=None if result is None else result["redemption"],
            journey_id=operation["journey_id"])
        # The delivery owner verifies before persistence. The coordinator's next
        # observation independently derives readiness from durable authority.
        if isinstance(receipt, CapabilityLicenseDeliveryReceipt):
            return OperationResult("waiting")
        return OperationResult("outcome_unknown", "operation_unconfirmed")

    async def stop(self):
        self._effects.close_admission()
        await self._effects.drain()
        await self.registered.stop()
        await self.ceremonies.stop()
