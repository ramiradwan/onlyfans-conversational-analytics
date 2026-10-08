"""Durable initial installation enrollment driven by hosted owner events."""
from __future__ import annotations

import asyncio
import json
import platform
from datetime import datetime, timezone

from app.persistence.auth import ClaimSubmission, SQLiteAuthenticationStore, ProvisioningCandidate, ProvisioningCandidateState
from app.persistence.onboarding import OnboardingJourneyStore
from app.provisioning.claim_submission import PRODUCT_VERSION, installation_proof_authority, hosted_transport
from app.provisioning.events import events
from app.security.hosted_grants import HostedGrantClient, HostedGrantUnavailable
from app.security.initial_handoff import InitialHandoffClient, InitialHandoffStream, PROFILE, InitialHandoffRefused


class InitialInstallationEnrollment:
    def __init__(self, store: SQLiteAuthenticationStore, *, hosted_origin: str,
                 hosted_start_url: str, client=None, stream=None, grants=None, continuity=None, continuity_stream=None) -> None:
        self.store = store
        self.journeys = OnboardingJourneyStore(store)
        key = installation_proof_authority(store) if client is None else client.key
        transport = hosted_transport(hosted_origin) if client is None else client.transport
        self.client = client or InitialHandoffClient(transport, key)
        self.stream = stream or InitialHandoffStream(hosted_origin)
        self.grants = grants or HostedGrantClient(transport, key, store)
        from app.security.onboarding_continuity import HostedOnboardingContinuity
        self.continuity = continuity or HostedOnboardingContinuity(transport, key)
        self.continuity_stream = continuity_stream or InitialHandoffStream(hosted_origin, continuity=True)
        self.hosted_start_url = hosted_start_url
        self._tasks = {}
        self._lock = asyncio.Lock()

    async def prepare(self, journey_id: str) -> dict:
        async with self._lock:
            row = self.journeys.open(journey_id)
            if datetime.fromisoformat(row["expires_at"]) <= self.store._now():
                raise InitialHandoffRefused("journey_expired")
            if row["state"] in {"completing", "unknown", "completed"}:
                self.resume(journey_id)
                return {"state": row["state"], "journey_id": journey_id}
            if row["state"] == "waiting" and row["handoff_reference"]:
                self.resume(journey_id)
                return self._browser_entry(row)
            if row["state"] not in {"new", "preparing", "prepare-unknown"}:
                raise InitialHandoffRefused("handoff_unconfirmed")
            operation = row["operation_id"] or self.journeys.new_operation()
            key = await asyncio.to_thread(self.client.key.ensure_ready)
            public = json.loads(key.public_key_jwk)
            request = {"profile": PROFILE, "operation_id": operation, "destination": {
                "installation_id": row["installation_id"],
                "installation_key": {"alg": "ES256", "kid": key.installation_key_id,
                                     "jwk": {name: public[name] for name in ("crv", "kty", "x", "y")}},
                "device": {"platform": "windows", "product_version": PRODUCT_VERSION,
                           "display_name": (platform.node() or "This computer")[:128]},
            }}
            if row["prepare_json"] is not None:
                request = json.loads(row["prepare_json"])
            self.journeys.update(journey_id, state="preparing", operation_id=operation,
                prepare_json=json.dumps(request, separators=(",", ":")))
            try:
                for attempt in range(3):
                    try:
                        prepared = await asyncio.to_thread(self.client.prepare, request)
                        break
                    except HostedGrantUnavailable:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.25 * 2 ** attempt)
            except Exception:
                self.journeys.update(journey_id, state="prepare-unknown", reason=None)
                events.publish()
                raise
            row = self.journeys.update(journey_id, state="waiting", handoff_reference=prepared["reference"],
                                       handoff_expires_at=prepared["expires_at"], reason=None)
            events.publish()
            self.resume(journey_id)
            return self._browser_entry(row)

    def _browser_entry(self, row: dict) -> dict:
        return {"state": "waiting", "journey_id": row["journey_id"],
                "handoff_reference": row["handoff_reference"], "hosted_start_url": self.hosted_start_url}

    def resume(self, journey_id: str) -> None:
        row = self.journeys.get(journey_id)
        if row is None:
            return
        with self.store.database.read() as connection:
            receiving = connection.execute("SELECT 1 FROM onboarding_transfer_intents WHERE journey_id=? AND continuation_json IS NULL AND expires_at>?",
                                           (journey_id,self.store._now().timestamp())).fetchone()
        if receiving is not None:
            return
        existing = self._tasks.get(journey_id)
        if existing is None or existing.done():
            self._tasks[journey_id] = asyncio.create_task(self._run(journey_id))

    async def pause_for_transfer(self, journey_id: str) -> None:
        async with self._lock:
            row = self.journeys.get(journey_id)
            if row is None or row["scope_json"] is not None or row["state"] not in {"new", "preparing", "prepare-unknown", "waiting", "transfer"}:
                raise InitialHandoffRefused("handoff_unconfirmed")
            existing = self._tasks.get(journey_id)
            if existing is not None and not existing.done():
                existing.cancel()
                await asyncio.gather(existing, return_exceptions=True)

    async def _run(self, journey_id: str) -> None:
        row = self.journeys.get(journey_id)
        if row is None or row["state"] in {"new", "expired", "revoked", "reauthentication-required"}:
            return
        if row["state"] in {"preparing", "prepare-unknown"}:
            try:
                await self.prepare(journey_id)
            except Exception:
                return
            row = self.journeys.get(journey_id)
        if row["state"] == "completed":
            await self._continuity(row)
            return
        if row["state"] in {"unknown", "completing"}:
            # An interrupted completion is reconciled using a fresh proof, never
            # reissued. No cached bootstrap response is retained.
            try:
                await self._recover(row)
                await self._continuity(self.journeys.get(journey_id))
            except Exception:
                self.journeys.update(journey_id, state="unknown", reason=None)
                events.publish()
            return
        if row["state"] != "waiting" or not row["handoff_reference"]:
            return
        last_revision = -1
        for attempt in range(3):
            try:
                request = {"profile": PROFILE, "operation_id": row["operation_id"], "reference": row["handoff_reference"]}
                envelope = await asyncio.to_thread(self.client.envelope, "wait", request)
                async for event in self.stream.events(envelope):
                    if event["revision"] <= last_revision:
                        continue
                    last_revision = event["revision"]
                    status = event["status"]
                    if status == "authorized":
                        scope = event["scope"]
                        key = self.client.key.ensure_ready()
                        if scope["installation_id"] != row["installation_id"] or scope["installation_key_jkt"] != key.installation_key_jkt:
                            raise HostedGrantUnavailable("Initial scope mismatch")
                        row = self.journeys.update(journey_id, state="completing", scope_json=json.dumps(scope, separators=(",", ":")))
                        events.publish()
                        result = await asyncio.to_thread(self.client.complete, {**request, "scope": scope})
                        if result["status"] == "completed":
                            bootstrap = result["bootstrap"]
                            # Save only nonauthorizing coordinates before grant
                            # verification so interruption remains recoverable.
                            consumed = await asyncio.to_thread(self.grants.accept_initial_handoff_bootstrap, bootstrap, scope=scope)
                            self._record_claim(row, bootstrap)
                            self.store.resolve_claim_submission(bootstrap["claim_id"], outcome=None,
                                resolved_at=self.store._now(), enrolled_at=consumed.enrolled_at)
                            del bootstrap, result
                        else:
                            await self._recover(row)
                        row = self.journeys.update(journey_id, state="completed", reason=None)
                        self._candidate(row)
                        events.publish()
                        await self._continuity(row)
                        return
                    if status == "recovery-required":
                        await self._recover(row)
                        await self._continuity(self.journeys.get(journey_id))
                        return
                    if status in {"expired", "revoked", "reauthentication-required"}:
                        self.journeys.update(journey_id, state=status, reason=status)
                        events.publish()
                        return
                raise HostedGrantUnavailable("Initial wait disconnected")
            except asyncio.CancelledError:
                raise
            except Exception:
                current = self.journeys.get(journey_id)
                if current["state"] == "completing":
                    self.journeys.update(journey_id, state="unknown", reason=None)
                    events.publish()
                    try:
                        await self._recover(current)
                        await self._continuity(self.journeys.get(journey_id))
                    except Exception:
                        pass
                    return
                if attempt < 2:
                    await asyncio.sleep(0.25 * 2 ** attempt)
        # A lost unregistered wait stream is not a completion attempt. Retain
        # its key-bound locator and reconnect that stream, never a registered
        # receipt endpoint that this key is not yet eligible to use.
        self.journeys.update(journey_id, state="waiting", reason=None)
        events.publish()

    def _record_claim(self, row: dict, value: dict) -> None:
        key = self.client.key.ensure_ready()
        scope = json.loads(row["scope_json"]) if row["scope_json"] else None
        if (value.get("installation_id") != row["installation_id"]
                or value.get("installation_key_id") != key.installation_key_id
                or value.get("installation_key_jkt") != key.installation_key_jkt
                or scope is None
                or any(value.get(name) != scope[name] for name in ("organization_id", "onboarding_transaction_id"))):
            raise HostedGrantUnavailable("Initial receipt binding mismatch")
        existing = self.store.claim_submission(value["claim_id"])
        if existing is None:
            self.store.record_claim_submission(ClaimSubmission(
                claim_id=value["claim_id"], onboarding_transaction_id=value["onboarding_transaction_id"],
                organization_id=value["organization_id"], installation_id=row["installation_id"],
                submitted_at=self.store._now(), claim_profile="urn:bridge-clean:installation-claim:v2"))

    async def _recover(self, row: dict) -> None:
        key = await asyncio.to_thread(self.client.key.ensure_ready)
        receipt = await asyncio.to_thread(self.client.receipt, {"profile": PROFILE,
            "operation_id": row["operation_id"], "installation_id": row["installation_id"],
            "installation_key_id": key.installation_key_id})
        if receipt["operation_id"] != row["operation_id"]:
            raise HostedGrantUnavailable("Initial operation mismatch")
        self._record_claim(row, receipt)
        await asyncio.to_thread(self.grants.recover_bootstrap_v2, claim_id=receipt["claim_id"],
                                recovery_request_id=self.journeys.new_operation())
        row = self.journeys.update(row["journey_id"], state="completed", reason=None)
        self._candidate(row)
        events.publish()

    def _candidate(self, row: dict) -> None:
        if row["scope_json"] is None:
            return
        scope = json.loads(row["scope_json"])
        if self.store.provisioning_candidate(scope["association_request_id"]) is None:
            self.store.record_provisioning_candidate(ProvisioningCandidate(
                association_request_id=scope["association_request_id"], installation_id=scope["installation_id"],
                onboarding_transaction_id=scope["onboarding_transaction_id"], organization_id=scope["organization_id"],
                creator_account_id=scope["intended_creator_id"], state=ProvisioningCandidateState.PENDING,
                requested_at=self.store._now()))

    async def _continuity(self, row: dict) -> None:
        # Only a locally verified completed enrollment reaches this registered-key
        # stream. Its projection triggers signed grant acquisition, never approval
        # or local readiness by itself.
        from app.security.onboarding_continuity import PROFILE as HOSTED_PROFILE, validated_event
        from app.provisioning.binding_acquisition import acquire_creator_account_binding
        if not row["scope_json"] or not self.store.consumed_claim_submissions():
            return
        self._candidate(row)
        scope = json.loads(row["scope_json"])
        cursor = None
        reconciled_closure = False
        for attempt in range(3):
            request = {"profile": HOSTED_PROFILE, "onboarding_transaction_id": scope["onboarding_transaction_id"],
                       "organization_id": scope["organization_id"], "installation_id": scope["installation_id"]}
            if cursor is not None:
                request["cursor"] = cursor
            received = False
            try:
                envelope = await asyncio.to_thread(self.continuity.envelope, request)
                initial = True
                async for event in self.continuity_stream.events(envelope):
                    value = validated_event(event, transaction_id=scope["onboarding_transaction_id"])
                    if initial and event["kind"] != "snapshot":
                        raise HostedGrantUnavailable("Continuity snapshot required")
                    if not initial and cursor is not None:
                        if value["epoch"] != cursor["epoch"] or value["revision"] > cursor["revision"] + 1:
                            cursor = None
                            raise HostedGrantUnavailable("Continuity reconciliation required")
                        if value["revision"] <= cursor["revision"]:
                            continue
                    initial = False
                    received = True
                    cursor = {"epoch": value["epoch"], "revision": value["revision"]}
                    facts = value["facts"]
                    if facts["authorization"] != "current":
                        await self._refresh_current_grants(scope)
                        events.publish()
                        return
                    if facts["association"] == "none":
                        from app.security.hosted_grants import CreatorAssociationRequest, CreatorAssociationPending
                        try:
                            await asyncio.to_thread(self.grants.request_creator_association, CreatorAssociationRequest(
                                association_request_id=scope["association_request_id"],
                                onboarding_transaction_id=scope["onboarding_transaction_id"],
                                organization_id=scope["organization_id"], installation_id=scope["installation_id"],
                                creator_account_id=scope["intended_creator_id"]))
                        except CreatorAssociationPending:
                            pass
                    elif facts["association"] == "approved":
                        candidate = self.store.provisioning_candidate(scope["association_request_id"])
                        if candidate is not None and candidate.state is ProvisioningCandidateState.PENDING:
                            await asyncio.to_thread(acquire_creator_account_binding, store=self.store, client=self.grants,
                                                    now=self.store._now)
                        events.publish()
                    elif facts["association"] in {"revoked", "rejected", "expired"}:
                        # The projection is a wake signal. Signed grant refresh
                        # performs revocation; this view makes no authority claim.
                        await self._refresh_current_grants(scope)
                        events.publish()
                if received and not reconciled_closure:
                    reconciled_closure = True
                    await self._reconcile_closed_stream(scope)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                from app.security.initial_handoff import OnboardingStreamAdmissionRefused
                if not reconciled_closure and (received or isinstance(error, OnboardingStreamAdmissionRefused)):
                    reconciled_closure = True
                    await self._reconcile_closed_stream(scope)
                if attempt < 2:
                    await asyncio.sleep(0.25 * 2 ** attempt)
        events.publish()

    async def _reconcile_closed_stream(self, scope: dict) -> None:
        # A closed/refused stream conveys no revocation authority. One signed
        # refresh pass reconciles the owner; unavailable results remain unknown.
        try:
            await self._refresh_current_grants(scope)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        events.publish()

    async def _refresh_current_grants(self, scope: dict) -> None:
        for grant in self.store.verified_grants():
            if (grant.installation_id == scope["installation_id"]
                    and grant.organization_id == scope["organization_id"]
                    and grant.valid_from <= self.store._now() < grant.expires_at):
                await asyncio.to_thread(self.grants.refresh_reference, grant.reference_id)

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.grants.close()
