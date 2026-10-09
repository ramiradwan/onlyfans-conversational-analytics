"""Resume one saved installation through pushed current-owner projections."""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urlsplit

from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.provisioning.claim_submission import hosted_transport, installation_proof_authority
from app.provisioning.events import events
from app.security.hosted_grants import CreatorAssociationRequest, HostedGrantClient, HostedGrantUnavailable
from app.security.installation_continuation import (
    PROFILE, InstallationContinuationClient,
    InstallationContinuationStream, validated_snapshot,
)


class RegisteredInstallationContinuation:
    def __init__(self, store, *, hosted_origin: str, hosted_start_url: str | None = None,
                 client=None, stream=None, grants=None) -> None:
        self.store = store
        self.contexts = InstallationContinuationStore(store)
        key = installation_proof_authority(store) if client is None else client.key
        transport = hosted_transport(hosted_origin) if client is None else client.transport
        self.client = client or InstallationContinuationClient(transport, key, clock=store._now)
        self.stream = stream or InstallationContinuationStream(hosted_origin)
        self.grants = grants or HostedGrantClient(transport, key, store)
        self.hosted_start_url = hosted_start_url or hosted_origin.rstrip("/") + "/public/onboarding/installation-continue"
        browser = urlsplit(self.hosted_start_url)
        if (browser.scheme != "https" or not browser.hostname or browser.query or browser.fragment
                or browser.username or browser.password or browser.path != "/public/onboarding/installation-continue"):
            raise ValueError("Invalid continuation browser destination")
        self._tasks = {}
        self._bindings = {}
        self._generation = {}
        self._lock = asyncio.Lock()
        self._stopped = False

    def _current(self, journey_id, generation=None):
        if self._stopped or (generation is not None and self._generation.get(journey_id) != generation):
            raise OnboardingJourneyUnavailable("journey_unavailable")
        row = self.contexts.require_current(journey_id)
        if row["provider_expires_at"] and self._deadline(row) <= self.store._now():
            raise OnboardingJourneyUnavailable("journey_expired")
        return row

    @staticmethod
    def _request(row, *, known=False):
        request = {"profile": PROFILE, "operation_id": row["operation_id"], "scope": row["scope"]}
        if known:
            request["continuation_id"] = row["continuation_id"]
        return request

    @staticmethod
    def _deadline(row):
        deadlines = [datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))]
        if row["provider_expires_at"]:
            deadlines.append(datetime.fromisoformat(row["provider_expires_at"].replace("Z", "+00:00")))
        return min(deadlines)

    def _browser_entry(self, row):
        if row["reference"] is None or row["state"] in {"expired", "revoked"}:
            return {"state": "unknown", "journey_id": row["journey_id"]}
        return {"state": "waiting", "journey_id": row["journey_id"],
                "continuation_reference": row["reference"], "hosted_start_url": self.hosted_start_url}

    async def prepare(self, journey_id):
        async with self._lock:
            self._current(journey_id)
            claimed = self.contexts.begin_prepare(journey_id)
            if claimed is not None:
                events.publish()
                # The operation and uncertain state are durable before this call.
                # Any lost result is resolved by read; preparation is never replayed.
                result = await asyncio.to_thread(self.client.prepare, self._request(claimed),
                    before_send=lambda: self._current(journey_id))
                self._current(journey_id)
                row = self.contexts.bind_result(journey_id, continuation_id=result["continuation_id"],
                    expires_at=result["expires_at"], reference=result["reference"])
                events.publish()
            else:
                row = self._current(journey_id)
                if row["reference"] is None:
                    row = await self._read(journey_id)
            self.resume(journey_id)
            return self._browser_entry(row)

    async def read_browser_entry(self, journey_id):
        async with self._lock:
            row = self._current(journey_id)
            if row["state"] == "new":
                return {"state": "unknown", "journey_id": journey_id}
            if row["reference"] is None:
                row = await self._read(journey_id)
            self.resume(journey_id)
            return self._browser_entry(row)

    async def _read(self, journey_id, generation=None):
        row = self._current(journey_id, generation)
        result = await asyncio.to_thread(self.client.read, self._request(row, known=True),
            before_send=lambda: self._current(journey_id, generation))
        self._current(journey_id, generation)
        if result["result"] != "found":
            row = self.contexts.mark_state(journey_id,
                "expired" if result["result"] == "expired" else "unknown")
            events.publish()
            return row
        return self._snapshot(journey_id, result["snapshot"], reference=result["reference"], generation=generation)

    def _snapshot(self, journey_id, value, *, reference=None, generation=None):
        row = self._current(journey_id, generation)
        value = validated_snapshot(value, continuation_id=row["continuation_id"] or value["continuation_id"],
                                   expires_at=row["provider_expires_at"])
        state = value["state"]
        local_state = state if state in {"expired", "revoked"} else "waiting"
        if row["state"] == "completed" and state not in {"expired", "revoked"}:
            local_state = "completed"
        updated = self.contexts.bind_result(journey_id, continuation_id=value["continuation_id"],
            expires_at=value["expires_at"], reference=reference, epoch=value["epoch"], revision=value["revision"],
            provider_state=state, state=local_state)
        events.publish()
        return updated

    def resume(self, journey_id):
        try:
            row = self._current(journey_id)
        except (ValueError, OnboardingJourneyUnavailable):
            return
        if row["state"] in {"new", "expired", "revoked"}:
            return
        task = self._tasks.get(journey_id)
        if task is None or task.done():
            generation = self._generation.get(journey_id, 0) + 1
            self._generation[journey_id] = generation
            self._tasks[journey_id] = asyncio.create_task(self._run(journey_id, generation))

    async def _run(self, journey_id, generation):
        try:
            row = self._current(journey_id, generation)
            remaining = (self._deadline(row) - self.store._now()).total_seconds()
            async with asyncio.timeout(remaining):
                if row["continuation_id"] is None:
                    row = await self._read(journey_id, generation)
                    if row["continuation_id"] is None or row["state"] in {"expired", "revoked"}:
                        return
                await self._observe(journey_id, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A closed stream or unconfirmed request is not revocation evidence.
            try:
                row = self._current(journey_id, generation)
                if row["state"] not in {"completed", "expired", "revoked"}:
                    self.contexts.mark_state(journey_id, "unknown")
            except (ValueError, OnboardingJourneyUnavailable):
                pass
            events.publish()

    async def _observe(self, journey_id, generation):
        for attempt in range(3):
            row = self._current(journey_id, generation)
            request = self._request(row, known=True)
            request["cursor"] = (None if row["epoch"] is None else
                                 {"epoch": row["epoch"], "revision": row["provider_revision"]})
            try:
                envelope = await asyncio.to_thread(self.client.envelope, "stream", request,
                    before_send=lambda: self._current(journey_id, generation))
                self._current(journey_id, generation)
                async for event in self.stream.events(envelope, expires_at=row["provider_expires_at"]):
                    row = self._snapshot(journey_id, event["snapshot"], generation=generation)
                    if row["provider_state"] != "approved":
                        binding = self._bindings.get(journey_id)
                        if binding is not None and not binding.done():
                            binding.cancel()
                    if row["provider_state"] in {"expired", "revoked"}:
                        await self._refresh_grants(row)
                        return
                    if row["provider_state"] == "approved" and row["state"] != "completed":
                        binding = self._bindings.get(journey_id)
                        if binding is None or binding.done():
                            self._bindings[journey_id] = asyncio.create_task(self._finish_binding(journey_id, generation))
                raise HostedGrantUnavailable("Continuation stream closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.25 * 2 ** attempt)

    async def _finish_binding(self, journey_id, generation):
        try:
            await self._binding(journey_id, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                row = self._current(journey_id, generation)
                if row["state"] == "completing":
                    self.contexts.mark_state(journey_id, "unknown")
            except (ValueError, OnboardingJourneyUnavailable):
                pass
            events.publish()

    async def _binding(self, journey_id, generation):
        row = self._current(journey_id, generation)
        if row["provider_state"] != "approved":
            return
        row = self.contexts.mark_state(journey_id, "completing")
        events.publish()
        result = await asyncio.to_thread(self.client.binding, self._request(row, known=True),
            before_send=lambda: self._current(journey_id, generation))
        row = self._current(journey_id, generation)
        if row["provider_state"] != "approved":
            return
        memberships = [grant for grant in self.store.verified_grants()
            if grant.grant_type == "membership_snapshot" and grant.organization_id == row["scope"]["organization_id"]
            and grant.installation_id == row["scope"]["installation_id"]
            and grant.installation_key_id == row["scope"]["installation_key_id"]]
        if len(memberships) != 1:
            raise HostedGrantUnavailable("Saved membership is unavailable")
        candidate = row["target"].candidate
        association = CreatorAssociationRequest(candidate.association_request_id, candidate.onboarding_transaction_id,
            candidate.organization_id, candidate.installation_id, candidate.creator_account_id)
        grant = await asyncio.to_thread(self.grants.accept_continuation_binding, result["grant"], association,
                                       membership_reference_id=memberships[0].reference_id)
        self._current(journey_id, generation)
        self.contexts.complete_binding(journey_id, grant, membership_reference_id=memberships[0].reference_id)
        events.publish()

    async def _refresh_grants(self, row):
        for grant in self.store.verified_grants():
            if (grant.organization_id == row["scope"]["organization_id"]
                    and grant.installation_id == row["scope"]["installation_id"]):
                try:
                    await asyncio.to_thread(self.grants.refresh_reference, grant.reference_id)
                except Exception:
                    continue
        events.publish()

    async def stop(self):
        self._stopped = True
        tasks = tuple(self._tasks.values()) + tuple(self._bindings.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.grants.close()
