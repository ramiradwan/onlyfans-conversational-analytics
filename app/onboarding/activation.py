"""Accepted desktop activation delegates commercial authority to existing owners."""
from __future__ import annotations

import asyncio
import json
import secrets

from app.onboarding.contract import digest
from app.onboarding.coordinator import OperationResult
from app.persistence.auth import _new_uuid7
from app.persistence.activation_reconciliation import ActivationReconciliationJournal, MAX_ATTEMPTS
from app.persistence.installation_continuation import InstallationContinuationStore
from app.provisioning.owner_effects import OwnerEffects
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.activation_reconciliation import ActivationReconciliationRefused
from app.security.initial_handoff import timestamp


class DesktopActivation:
    def __init__(self, store, *, key, registered, ceremonies, browser_tasks, redemption, reconciliation_client=None,
                 reconciliation_origin=None, replacement_client=None):
        self.store, self.key, self.registered = store, key, registered
        self.ceremonies, self.browser_tasks, self.redemption = ceremonies, browser_tasks, redemption
        self.contexts = InstallationContinuationStore(store)
        self._lock = asyncio.Lock()
        self._effects = OwnerEffects()
        self.reconciliation_client = reconciliation_client
        self.reconciliation = ActivationReconciliationJournal(store, origin=reconciliation_origin)
        self.replacement_client = replacement_client
        from app.persistence.activation_review_replacement import ActivationReviewReplacements
        self.replacements = ActivationReviewReplacements(store, origin=reconciliation_origin)

    def _source(self, operation_id):
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM desktop_activation_sources WHERE operation_id=?", (operation_id,)).fetchone()
        return None if row is None else dict(row)

    def outcome_is_known(self, operation_id):
        row = self.ceremonies.row(operation_id)
        # Before ceremony reservation no commercial send was possible. Once
        # reserved, only a verified original committed outcome resolves it.
        if row is None or row["domain_status"] == "committed":
            return True
        with self.store.database.read() as connection:
            outcome = self.reconciliation.row(connection, operation_id)
            operation = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
        return bool(operation is not None and self._delivery_already_admitted(operation)) or (
            outcome is not None and outcome["status"] == "committed"
            and operation is not None and self._reconciled_origin_matches(operation))

    def _expired_reconciliation(self, row):
        if self.reconciliation_client is None or row is None or row["domain_status"] in {"committed", "revoked"}:
            return False
        binding = json.loads(row["binding_json"])
        return self.store._now() >= timestamp(binding["source_expires_at"])

    def reconciliation_pending(self, journey_id, *, connection=None):
        if self.reconciliation_client is None:
            return False
        from contextlib import nullcontext
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            rows = current.execute("SELECT c.* FROM desktop_browser_ceremonies c "
                "JOIN desktop_setup_operations o ON o.operation_id=c.operation_id "
                "WHERE o.journey_id=? AND o.action='activation.review' "
                "AND o.state IN ('accepted','waiting','outcome_unknown')", (journey_id,)).fetchall()
            return any(self._expired_reconciliation(row) for row in rows)

    def automatic_recovery(self, operation):
        row = self.ceremonies.row(operation["operation_id"])
        if row is None:
            return False
        if not self._expired_reconciliation(row):
            return True
        if self._delivery_already_admitted(operation):
            return True
        try:
            with self.store.database.read() as connection:
                self.reconciliation.request(connection, operation)
                outcome = self.reconciliation.row(connection, operation["operation_id"])
                if outcome is None:
                    return True
                if outcome["status"] == "committed":
                    return True
                return outcome["status"] is None and outcome["attempts"] < MAX_ATTEMPTS
        except (HostedGrantUnavailable, ValueError, KeyError):
            return False

    def _read_expired_activation(self, operation):
        def authorize():
            self._proof_read_guard(operation)
        with self.store.database.transaction() as connection:
            request, _ = self.reconciliation.request(connection, operation)
            outcome, _ = self.reconciliation.exact_row(connection, operation, request, authorize=authorize)
            if outcome is not None and outcome["status"] is not None:
                return outcome["status"]
            attempt = self.reconciliation.claim(connection, operation, request, authorize=authorize)
        if attempt is None:
            return None

        def before_send():
            with self.store.database.read() as connection:
                self.reconciliation.guard(connection, operation, request, attempt, authorize=authorize)

        try:
            result = self.reconciliation_client.read(request, before_send=before_send)
        except ActivationReconciliationRefused:
            # A deterministic refusal is not evidence that the activation failed.
            with self.store.database.transaction() as connection:
                self.reconciliation.refuse(connection, operation, request, attempt, authorize=authorize)
            return "refused"
        except HostedGrantUnavailable:
            return None
        with self.store.database.transaction() as connection:
            return self.reconciliation.record(connection, operation, request, attempt, result, authorize=authorize)

    async def reconcile_registered_source(self, proposal):
        """A replacement refusal names a source, not a new activation action."""
        coords = proposal["replacement"]
        with self.store.database.read() as connection:
            rows = connection.execute("SELECT o.* FROM desktop_setup_operations o "
                "JOIN desktop_activation_sources a ON a.operation_id=o.operation_id "
                "WHERE a.source_journey_id=? AND o.action='activation.review' "
                "AND o.state IN ('accepted','waiting','outcome_unknown')",
                (proposal["source_journey_id"],)).fetchall()
            if len(rows) != 1:
                return
            operation = dict(rows[0])
            try:
                request, _ = self.reconciliation.request(connection, operation)
            except (HostedGrantUnavailable, ValueError, KeyError):
                return
            if (request["source_operation_id"] != coords["operation_id"]
                    or request["source_prepare_digest"] != coords["expected_digest"]
                    or request["scope"]["creator_account_id"] != proposal["creator_account_id"]
                    or request["scope"]["installation_id"] != coords["installation_id"]
                    or request["scope"]["association_request_id"] != proposal["association_request_id"]):
                return
        # Only the original commercial owner can redeem its committed result.
        # There is no permission to prepare or confirm an activation here.
        def cannot_prepare(**_):
            raise HostedGrantUnavailable("Original activation read cannot authorize a mutation")
        await self.execute(operation, authorize=cannot_prepare, can_prepare=False)

    def replacement_offer(self, journey_id, scope, *, connection=None):
        if self.replacement_client is None or journey_id is None:
            return None
        from contextlib import nullcontext
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            rows = current.execute("SELECT * FROM desktop_setup_operations WHERE journey_id=? AND action='activation.review' "
                "AND state IN ('accepted','waiting','outcome_unknown')", (journey_id,)).fetchall()
            if len(rows) != 1:
                return None
            try:
                return self.replacements.describe(current, dict(rows[0]), scope, authorize=lambda **_: None)
            except (HostedGrantUnavailable, ValueError, KeyError):
                return None

    def admit_replacement(self, connection, journey_id, scope, *, authorize):
        if self.replacement_client is None:
            raise HostedGrantUnavailable("Review replacement is unavailable")
        intent = scope["activation_replacement"]
        original = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=? AND journey_id=?",
            (intent["original_operation_id"], journey_id)).fetchone()
        if original is None:
            raise HostedGrantUnavailable("Original review is unavailable")
        original = dict(original)
        proposal = self.replacements.describe(connection, original, scope, authorize=authorize)
        if proposal is None or {**proposal, "successor_desktop_operation_id": intent["successor_desktop_operation_id"]} != intent:
            raise HostedGrantUnavailable("Review replacement proposal changed")
        reservation = self.replacements.reserve(connection, original, scope,
            successor_desktop_operation_id=intent["successor_desktop_operation_id"], authorize=authorize)
        self.replacements.retire_original(connection, original, scope, reservation, authorize=authorize)
        return self.replacements.successor_intent(reservation)

    def _replace_review(self, operation, *, authorize, reconcile):
        if self.replacement_client is None:
            return None
        selected = json.loads(operation["owner_scope_json"])
        intent = selected["activation_replacement"]
        def current(connection):
            original = connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
                (intent["original_operation_id"],)).fetchone()
            reservation = self.replacements.row(connection, intent["original_operation_id"])
            if (original is None or reservation is None or self.replacements.successor_intent(reservation) != intent
                    or reservation["successor_desktop_operation_id"] != operation["operation_id"]):
                raise HostedGrantUnavailable("Review replacement command changed")
            return dict(original)
        from app.security.source_replacement import SourceReplacementRefused
        # An exact fresh-proof retry is bounded by the durable send count.
        # No actor at restart can enter this method or start a new ceremony.
        for retry in (reconcile, True):
            with self.store.database.transaction() as connection:
                original = current(connection)
                claim = self.replacements.claim(connection, original, selected, authorize=authorize, reconcile=retry)
            if claim is None:
                return None
            request, attempt = claim
            def guard():
                with self.store.database.read() as connection:
                    original_now = current(connection)
                    self.replacements.guard(connection, original_now, selected, request, attempt, authorize=authorize)
            try:
                response = self.replacement_client.replace(request, before_send=guard)
            except SourceReplacementRefused as refusal:
                with self.store.database.transaction() as connection:
                    self.replacements.record_refusal(connection, current(connection), selected, request, attempt, refusal,
                        authorize=authorize)
                return None
            except HostedGrantUnavailable:
                continue
            with self.store.database.transaction() as connection:
                result = self.replacements.record_prepared(connection, current(connection), selected, request, attempt,
                    response, authorize=authorize)
                return {"operation_id": operation["operation_id"], "source_journey_id": result["successor_journey_id"],
                    "activation_operation_id": result["successor_activation_operation_id"]}
        return None

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
            if selected.get("activation_replacement") is not None:
                raise HostedGrantUnavailable("Replacement requires its exact admitted source")
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

    def _original_delivery(self, operation):
        original = self._source(operation["operation_id"])
        if original is None or self.reconciliation.origin is None:
            return None
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM capability_license_activation_deliveries "
                "WHERE activation_operation_id=?", (original["activation_operation_id"],)).fetchone()
        if (row is not None and row["provider_origin"] == self.reconciliation.origin
                and json.loads(row["binding_json"]) == self._delivery_binding(operation)):
            return row
        return None

    def _reconciled_origin_matches(self, operation):
        return self._original_delivery(operation) is not None

    def _delivery_already_admitted(self, operation):
        """Existing admitted delivery recovery does not need an expired source."""
        row = self._original_delivery(operation)
        return bool(row is not None and row["activation_committed"])

    async def execute(self, operation, *, authorize, can_prepare, reconcile=False):
        async with self._lock:
            row = self.ceremonies.row(operation["operation_id"])
            if row is None:
                if not can_prepare:
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                authorize()
                selected = json.loads(operation["owner_scope_json"])
                if self.store._now().timestamp() >= operation["expires_at"]:
                    if selected.get("activation_replacement") is not None:
                        return OperationResult("outcome_unknown", "operation_unconfirmed")
                    return OperationResult("refused", "operation_expired")
                source = self._source(operation["operation_id"])
                if selected.get("activation_replacement") is not None:
                    if source is None:
                        try:
                            source = await self._effects.run(self._replace_review, operation, authorize=authorize,
                                reconcile=reconcile, journey_id=operation["journey_id"])
                        except (HostedGrantUnavailable, ValueError, KeyError):
                            source = None
                    if source is None:
                        return OperationResult("outcome_unknown", "operation_unconfirmed")
                else:
                    source = source or self._reserve(operation, authorize)
                if not self.redemption.prepare_activation(activation_operation_id=source["activation_operation_id"],
                        binding=self._delivery_binding(operation)):
                    return OperationResult("outcome_unknown", "authority_unavailable")
                await self.registered.prepare(source["source_journey_id"], authorize=authorize)
                authorize()
                response = await self.ceremonies.prepare(operation["operation_id"], self._binding(operation, source), authorize=authorize)
            else:
                if row["domain_status"] == "committed":
                    return await self._redeem(operation, None)
                if self._expired_reconciliation(row):
                    if self._delivery_already_admitted(operation):
                        return await self._redeem(operation, None)
                    try:
                        status = await self._effects.run(self._read_expired_activation, operation,
                            journey_id=operation["journey_id"])
                    except (HostedGrantUnavailable, ValueError, KeyError):
                        status = None
                    if status == "committed":
                        return await self._redeem(operation, None)
                    # Even verified noncommit does not admit a replacement review.
                    # Keep the original journal pending for the explicit replacement owner.
                    return OperationResult("outcome_unknown", "operation_unconfirmed")
                response = await self.ceremonies.reconcile(operation["operation_id"],
                    authorize=lambda: self._proof_read_guard(operation),
                    adopt_authorize=lambda connection=None: self._proof_read_guard(operation))
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
                    if json.loads(operation["owner_scope_json"]).get("activation_replacement") is not None:
                        return OperationResult("outcome_unknown", "authority_unavailable")
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
