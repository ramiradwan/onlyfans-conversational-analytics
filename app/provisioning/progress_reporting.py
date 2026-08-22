"""Closed, durable reporting of four coarse onboarding milestones."""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.core.config import settings
from app.persistence.auth import (
    AuthenticationStore,
    OnboardingMilestone,
    SQLiteAuthenticationStore,
)
from app.security.hosted_grants import (
    HTTPXHostedTransport,
    HostedGrantClient,
    ProgressDelivery,
)
from app.security.installation_key import (
    InstallationKeyAuthority,
    WindowsCNGInstallationKeyProvider,
)


HOSTED_ORIGIN_ENVIRONMENT_VARIABLE = "LOCAL_PROVISIONING_HOSTED_ORIGIN"
_RETRY_INTERVAL_SECONDS = 30
_MAX_RETRY_DELAY_SECONDS = 3_600


class ProgressClient(Protocol):
    def report_onboarding_progress(self, event: object) -> ProgressDelivery: ...


class OnboardingProgressCoordinator:
    """Own the outbox lifecycle without participating in local authorization."""

    def __init__(
        self,
        open_store: Callable[[], AuthenticationStore],
        open_client: Callable[[AuthenticationStore], ProgressClient],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._open_store = open_store
        self._open_client = open_client
        self._clock = clock
        self._client: ProgressClient | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self._flush_lock = threading.Lock()

    def mark(self, milestone: OnboardingMilestone) -> None:
        """Queue one milestone; reporting failures never escape to its caller."""

        try:
            now = self._now()
            self._open_store().enqueue_onboarding_progress(
                milestone,
                event_id=_new_uuid7(now),
                correlation_id=_new_uuid7(now),
                occurred_at=now,
            )
        except Exception:
            # The local lifecycle fact has already committed. Reporting is an
            # independent best-effort delivery concern and cannot roll it back.
            return

    def mark_and_flush(self, *milestones: OnboardingMilestone) -> None:
        for milestone in milestones:
            self.mark(milestone)
        try:
            self.flush()
        except Exception:
            return

    def flush(self) -> None:
        """Attempt every due event once, preserving fixed retry semantics."""

        if not self._flush_lock.acquire(blocking=False):
            return
        try:
            self._flush_due()
        finally:
            self._flush_lock.release()

    def _flush_due(self) -> None:
        """Run one serialized pass over the currently due events."""

        try:
            store = self._open_store()
            now = self._now()
            due = store.due_onboarding_progress(now=now)
        except Exception:
            return
        for event in due:
            try:
                if self._client is None:
                    self._client = self._open_client(store)
                outcome = self._client.report_onboarding_progress(event)
            except Exception:
                outcome = "retry"
                self._client = None
            try:
                instant = self._now()
                if outcome == "delivered":
                    store.deliver_onboarding_progress(
                        event.event_id, delivered_at=instant
                    )
                elif outcome == "refused":
                    store.refuse_onboarding_progress(
                        event.event_id, refused_at=instant
                    )
                else:
                    delay = min(
                        _RETRY_INTERVAL_SECONDS * (2 ** min(event.attempts, 7)),
                        _MAX_RETRY_DELAY_SECONDS,
                    )
                    store.retry_onboarding_progress(
                        event.event_id,
                        next_attempt_at=instant + timedelta(seconds=delay),
                    )
            except Exception:
                # A failed outbox update leaves the event pending. It cannot
                # affect the already committed local lifecycle transition.
                continue

    async def start(self) -> None:
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(
                self._retry_loop(), name="onboarding-progress-outbox"
            )

    async def stop(self) -> None:
        task = self._retry_task
        self._retry_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.flush)
            await asyncio.sleep(_RETRY_INTERVAL_SECONDS)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Onboarding progress clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def durable_onboarding_progress(
    open_store: Callable[[], AuthenticationStore], *, hosted_origin: str
) -> OnboardingProgressCoordinator:
    """Compose the coordinator over the existing proof and transport chokepoint."""

    def open_client(store: AuthenticationStore) -> ProgressClient:
        return HostedGrantClient(
            HTTPXHostedTransport(hosted_origin),
            InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider()),
            store,
        )

    return OnboardingProgressCoordinator(open_store, open_client)


@lru_cache(maxsize=1)
def configured_runtime_onboarding_progress() -> OnboardingProgressCoordinator:
    opened: AuthenticationStore | None = None

    def open_store() -> AuthenticationStore:
        nonlocal opened
        if opened is None:
            opened = SQLiteAuthenticationStore(Path(settings.auth_database_path))
        return opened

    return durable_onboarding_progress(
        open_store,
        hosted_origin=os.environ.get(HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, ""),
    )


def _new_uuid7(now: datetime) -> str:
    milliseconds = int(now.timestamp() * 1_000)
    if not 0 <= milliseconds < 1 << 48:
        raise ValueError("Onboarding progress time is outside UUIDv7 range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (milliseconds << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(UUID(int=value))
