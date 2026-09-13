"""Bounded installation-proof refresh within one Brain process."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator

from app.persistence.auth import AuthenticationStore, ProvisioningCandidateState, VerifiedGrantReference
from app.security.grant_types import LICENSE_ENTITLEMENT
from app.security.hosted_grants import (
    GrantRefresh, HostedGrantClient, HostedTransport, InstallationProofAuthority,
    grant_offline_grace_seconds,
)

logger = logging.getLogger(__name__)
MAX_REFRESH_REFERENCES = 128
ATTEMPT_SECONDS = 25.0
HOSTED_ORIGIN_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HOSTED_ORIGIN"


@dataclass(frozen=True, slots=True)
class RefreshLease:
    generation: int
    deadline: float


class GrantRefreshLifecycle:
    """One active outbound attempt, with deadline and shutdown commit fences."""

    def __init__(
        self,
        open_store: Callable[[], AuthenticationStore],
        client_factory: Callable[[AuthenticationStore], HostedGrantClient],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        include_legacy_license_entitlement: bool = True,
    ) -> None:
        self._open_store = open_store
        self._client_factory = client_factory
        self._client: HostedGrantClient | None = None
        self._clock, self._monotonic, self._jitter = clock, monotonic, jitter
        self._fence = threading.RLock()
        self._attempt = threading.Lock()
        self._stopped = threading.Event()
        self._generation = 0
        self._worker: threading.Thread | None = None
        self._retries: dict[str, tuple[int, float]] = {}
        self._include_legacy_license_entitlement = include_legacy_license_entitlement

    def lease(self) -> RefreshLease:
        with self._fence:
            return RefreshLease(self._generation, self._monotonic() + ATTEMPT_SECONDS)

    @contextmanager
    def guard(self, lease: RefreshLease) -> Iterator[bool]:
        with self._fence:
            yield (
                not self._stopped.is_set()
                and lease.generation == self._generation
                and self._monotonic() < lease.deadline
            )

    def references(self) -> tuple[VerifiedGrantReference, ...]:
        store = self._open_store()
        key = store.installation_key_reference()
        if key is None:
            return ()
        # The store returns only unrevoked references. Never infer an installation
        # from a request or choose among conflicting durable installation tuples.
        grants = store.verified_grants(limit=MAX_REFRESH_REFERENCES + 1)
        if len(grants) > MAX_REFRESH_REFERENCES:
            return ()
        selected = tuple(
            grant for grant in grants
            if (
                self._include_legacy_license_entitlement
                or grant.grant_type != LICENSE_ENTITLEMENT
            )
            and grant.installation_key_id == key.installation_key_id
            and grant.installation_key_jkt == key.installation_key_jkt
        )
        identities = {(grant.organization_id, grant.installation_id) for grant in selected}
        if len(identities) != 1 or any(not value for identity in identities for value in identity):
            return ()
        return selected

    def refresh(self, reference_id: str, *, lease: RefreshLease | None = None) -> GrantRefresh:
        lease = lease or self.lease()
        unknown = GrantRefresh("unknown", (reference_id,), None)
        if not self._attempt.acquire(blocking=False):
            return unknown
        try:
            with self.guard(lease) as active:
                if not active:
                    return unknown
            if reference_id not in {grant.reference_id for grant in self.references()}:
                return unknown
            if self._client is None:
                self._client = self._client_factory(self._open_store())
            return self._client.refresh_reference(
                reference_id, commit_guard=lambda: self.guard(lease)
            )
        except Exception:
            # Transport, provider and storage exceptions can contain sensitive
            # context. Only the fixed result crosses the lifecycle boundary.
            logger.warning("grant_refresh_event reason_code=refresh_unavailable")
            return unknown
        finally:
            if self._stopped.is_set():
                self._close_client()
            self._attempt.release()

    def prepare_finalization(self, association_id: str, account_id: str) -> RefreshLease | None:
        """Refresh only membership selected by an already-approved local tuple."""
        lease = self.lease()
        store = self._open_store()
        candidate = store.provisioning_candidate(association_id)
        if (candidate is None or candidate.state != ProvisioningCandidateState.APPROVED
                or candidate.creator_account_id != account_id):
            return None
        grants = self.references()
        members = tuple(grant for grant in grants if grant.grant_type == "membership_snapshot"
                        and grant.installation_id == candidate.installation_id
                        and grant.organization_id == candidate.organization_id)
        bindings = tuple(grant for grant in grants if grant.grant_type == "creator_account_binding"
                         and grant.creator_account_id == account_id
                         and grant.installation_id == candidate.installation_id
                         and grant.organization_id == candidate.organization_id)
        if (len(members) != 1 or len(bindings) != 1
                or (members[0].issuer, members[0].subject) != (bindings[0].issuer, bindings[0].subject)):
            return None
        if account_id not in (members[0].allowed_creator_account_ids or ()):
            if self.refresh(members[0].reference_id, lease=lease).state != "updated":
                return None
        # Finalization still independently verifies the complete current tuple.
        return lease

    def tick(self) -> float:
        """Attempt at most one due reference, returning a bounded wait interval."""
        grants = self.references()
        active_ids = {grant.reference_id for grant in grants}
        self._retries = {key: value for key, value in self._retries.items() if key in active_ids}
        now, monotonic = self._clock(), self._monotonic()
        due: list[tuple[float, str]] = []
        for grant in grants:
            if not grant.valid_from <= now < grant.expires_at:
                continue
            signed_expiry = grant.expires_at.timestamp() - grant_offline_grace_seconds(grant.grant_type)
            lifetime = signed_expiry - grant.valid_from.timestamp()
            if lifetime <= 0:
                continue
            scheduled = grant.valid_from.timestamp() + 0.4 * lifetime
            wait = max(0.0, scheduled - now.timestamp())
            retry = self._retries.get(grant.reference_id)
            if retry is not None:
                wait = max(wait, retry[1] - monotonic)
            due.append((wait, grant.reference_id))
        if not due:
            return 30.0
        wait, reference_id = min(due)
        if wait > 0:
            return min(wait, 30.0)
        result = self.refresh(reference_id)
        if result.state == "unknown":
            failures = min(self._retries.get(reference_id, (0, 0.0))[0] + 1, 5)
            delay = min(300.0, 15.0 * 2 ** failures) * (0.8 + 0.4 * self._jitter())
            self._retries[reference_id] = (failures, self._monotonic() + delay)
        else:
            self._retries.pop(reference_id, None)
        return 0.1

    def start(self) -> None:
        with self._fence:
            if self._stopped.is_set() or self._worker is not None:
                return
            self._worker = threading.Thread(target=self._run, name="grant-refresh", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    delay = self.tick()
                except Exception:
                    logger.warning("grant_refresh_event reason_code=refresh_unavailable")
                    delay = 30.0
                self._stopped.wait(delay)
        finally:
            self._close_client()

    def _close_client(self) -> None:
        with self._fence:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("grant_refresh_event reason_code=refresh_unavailable")

    def close(self) -> None:
        with self._fence:
            self._stopped.set()
            self._generation += 1
        if self._attempt.acquire(blocking=False):
            try:
                self._close_client()
            finally:
                self._attempt.release()

    async def stop(self) -> None:
        # An entered commit linearizes before shutdown. All later commits fail
        # the closed guard, including a response from an abandoned HTTP attempt.
        await asyncio.to_thread(self.close)
        if self._worker is not None:
            await asyncio.to_thread(self._worker.join, 1.0)


def configured_grant_refresh(
    open_store: Callable[[], AuthenticationStore],
    *,
    hosted_origin: str,
    transport_factory: Callable[[str], HostedTransport],
    proof_authority_factory: Callable[[AuthenticationStore], InstallationProofAuthority],
) -> GrantRefreshLifecycle:
    def client(store: AuthenticationStore) -> HostedGrantClient:
        transport = transport_factory(hosted_origin)
        try:
            return HostedGrantClient(transport, proof_authority_factory(store), store)
        except BaseException:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
            raise
    return GrantRefreshLifecycle(
        open_store, client, include_legacy_license_entitlement=False
    )
