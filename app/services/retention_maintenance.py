"""Runtime enforcement for bounded canonical and managed-recovery retention."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from app.core.config import settings
from app.persistence.database import CanonicalSQLite
from app.persistence.managed_recovery import prune_managed_recovery_files
from app.persistence.retention import CreatorVaultRetention, utc_now


LOGGER = logging.getLogger(__name__)
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class RetentionMaintenanceResult:
    accounts_checked: int
    expired_message_count: int
    removed_recovery_file_count: int


class RetentionMaintenance:
    """Apply time-based lifecycle rules without waiting for another user action."""

    def __init__(
        self,
        database: CanonicalSQLite,
        *,
        managed_recovery_roots: Iterable[str | Path] = (),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self._managed_recovery_roots = tuple(
            Path(os.path.abspath(os.fspath(root))) for root in managed_recovery_roots
        )
        self._clock = clock

    def run_once(self) -> RetentionMaintenanceResult:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("retention maintenance clock must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)

        with self.database.read() as connection:
            account_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT creator_account_id FROM account_heads ORDER BY creator_account_id"
                )
            ]

        retention = CreatorVaultRetention(
            self.database,
            clock=lambda: observed_at,
        )
        expired_message_count = 0
        for account_id in account_ids:
            expired_message_count += len(retention.enforce(account_id, now=observed_at))

        removed_recovery_file_count = 0
        seen_roots: set[Path] = set()
        for root in self._managed_recovery_roots:
            if root in seen_roots:
                continue
            seen_roots.add(root)
            removed_recovery_file_count += len(
                prune_managed_recovery_files(root, now=observed_at)
            )

        return RetentionMaintenanceResult(
            accounts_checked=len(account_ids),
            expired_message_count=expired_message_count,
            removed_recovery_file_count=removed_recovery_file_count,
        )


def _default_recovery_roots() -> tuple[Path, ...]:
    database_paths = (
        settings.canonical_database_path,
        settings.projection_database_path,
        settings.analytics_projection_database_path,
    )
    roots: list[Path] = []
    for database_path in database_paths:
        parent = Path(database_path).parent
        roots.extend((parent, parent / "backups"))
    return tuple(roots)


_DEFAULT_TASK: asyncio.Task[None] | None = None


async def start_default_retention_maintenance() -> asyncio.Task[None]:
    """Run one fail-closed sweep, then keep bounded lifecycles enforced."""

    global _DEFAULT_TASK
    from app.transport import transport_manager

    maintenance = RetentionMaintenance(
        transport_manager.canonical_database,
        managed_recovery_roots=_default_recovery_roots(),
    )

    # Startup does not become ready until current expiry obligations have been
    # applied successfully. A persistence/schema failure must not be read as
    # an empty retention authority.
    await asyncio.to_thread(maintenance.run_once)

    if _DEFAULT_TASK is not None and not _DEFAULT_TASK.done():
        return _DEFAULT_TASK

    async def run() -> None:
        while True:
            await asyncio.sleep(DEFAULT_MAINTENANCE_INTERVAL_SECONDS)
            try:
                await asyncio.to_thread(maintenance.run_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "retention_maintenance_event reason_code=periodic_enforcement_failed"
                )

    _DEFAULT_TASK = asyncio.create_task(run(), name="retention-maintenance")
    return _DEFAULT_TASK


async def shutdown_default_retention_maintenance(*, timeout: float = 5.0) -> bool:
    global _DEFAULT_TASK
    task = _DEFAULT_TASK
    _DEFAULT_TASK = None
    if task is None:
        return True
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        return True
    except asyncio.TimeoutError:
        return False
    return True
