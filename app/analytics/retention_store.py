"""Retention-aware wrappers for disposable analytics projection storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock, Timer
from typing import Callable

from app.analytics.database import ProjectionsDatabase
from app.analytics.historical_derivation import (
    HistoricalDerivationProvenance,
    provenance_for_projection,
)
from app.analytics.identity import CanonicalIdentity
from app.analytics.projection_store import CLEAR_PIPELINE_REVISION, empty_projection
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS

BOUNDED_PIPELINE_PREFIX = "analytics.pipeline.v3+"
from app.analytics.resilient_projection_store import LazySQLiteAnalyticsProjectionStore
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.models.analytics import AnalyticsProjection, RebuildArtifact
from app.persistence.projection_activation import (
    ProjectionActivationConflict,
    ProjectionActivationRepository,
)


MAX_RETENTION_TIMER_SECONDS = 86_400.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("analytics_retention_time_timezone_required")
    return value.astimezone(timezone.utc)


def _stored_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


class RetentionBoundSQLiteAnalyticsProjectionStore(SQLiteAnalyticsProjectionStore):
    """Fail closed and replace participant-linked generations at their source-time bound."""

    def __init__(
        self,
        database: ProjectionsDatabase | str | Path,
        *,
        activation: ProjectionActivationRepository,
        canonical_identity_reader: Callable[[str], CanonicalIdentity | None],
        clock: Callable[[], datetime] = utc_now,
        **kwargs,
    ) -> None:
        self._retention_clock = clock
        self._retention_lock = RLock()
        self._expiring_accounts: set[str] = set()
        self._retention_timers: dict[str, Timer] = {}
        self._retention_closed = False
        super().__init__(
            database,
            activation=activation,
            canonical_identity_reader=canonical_identity_reader,
            **kwargs,
        )
        self._arm_existing_retention()

    def stage_artifact(self, artifact: RebuildArtifact, **kwargs) -> str:
        expired, _ = self._artifact_retention_state(
            artifact.projection,
            now=_utc(self._retention_clock()),
        )
        if expired:
            raise ProjectionActivationConflict("analytics retention window expired")
        return super().stage_artifact(artifact, **kwargs)

    def publish_generation(self, generation_id: str, **kwargs) -> bool:
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            self.discard_generation(generation_id)
            raise ProjectionActivationConflict("analytics retention projection missing")
        expired, _ = self._artifact_retention_state(
            projection,
            now=_utc(self._retention_clock()),
        )
        if expired:
            self.discard_generation(generation_id)
            raise ProjectionActivationConflict("analytics retention window expired")
        changed = super().publish_generation(generation_id, **kwargs)
        if changed:
            creator_account_id = kwargs.get("creator_account_id")
            if isinstance(creator_account_id, str) and creator_account_id:
                self._arm_retention_timer(creator_account_id, generation_id)
            self._purge_expired_retired_generations()
        return changed

    def _matching_active_generation(
        self,
        creator_account_id: str,
        partition_ref: str,
        **kwargs,
    ) -> str | None:
        generation_id = super()._matching_active_generation(
            creator_account_id,
            partition_ref,
            **kwargs,
        )
        if generation_id is None or creator_account_id in self._expiring_accounts:
            return generation_id
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            return None
        expired, _ = self._artifact_retention_state(
            projection,
            now=_utc(self._retention_clock()),
        )
        if not expired:
            self._purge_expired_retired_generations()
            return generation_id
        self._expire_active_generation(creator_account_id, generation_id)
        return None

    def close_retention_scheduler(self) -> None:
        with self._retention_lock:
            self._retention_closed = True
            timers = list(self._retention_timers.values())
            self._retention_timers.clear()
        for timer in timers:
            timer.cancel()

    def _arm_existing_retention(self) -> None:
        with self.database.read() as connection:
            generation_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT generation_id FROM projection_generations WHERE status='active'"
                )
            ]
        for generation_id in generation_ids:
            intent = self.activation.get(generation_id)
            if intent is not None and intent.creator_account_id:
                self._arm_retention_timer(intent.creator_account_id, generation_id)

    def _arm_retention_timer(
        self, creator_account_id: str, generation_id: str
    ) -> None:
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            return
        expired, due = self._artifact_retention_state(
            projection, now=_utc(self._retention_clock())
        )
        with self._retention_lock:
            previous = self._retention_timers.pop(creator_account_id, None)
            if previous is not None:
                previous.cancel()
            if self._retention_closed:
                return
        if expired:
            self._expire_active_generation(creator_account_id, generation_id)
            return
        if due is None:
            return
        remaining = max(
            0.0,
            (due - _utc(self._retention_clock())).total_seconds(),
        )
        delay = min(remaining, MAX_RETENTION_TIMER_SECONDS)
        timer = Timer(
            delay,
            self._retention_timer_fired,
            args=(creator_account_id, generation_id),
        )
        timer.daemon = True
        with self._retention_lock:
            if self._retention_closed:
                return
            self._retention_timers[creator_account_id] = timer
        timer.start()

    def _retention_timer_fired(
        self, creator_account_id: str, generation_id: str
    ) -> None:
        with self._retention_lock:
            timer = self._retention_timers.get(creator_account_id)
            if self._retention_closed or timer is None:
                return
            self._retention_timers.pop(creator_account_id, None)
        try:
            partition_ref = self._partition_ref(creator_account_id)
            active = self._active_generation_row(partition_ref)
            if active is None or str(active["generation_id"]) != generation_id:
                return
            self._arm_retention_timer(creator_account_id, generation_id)
        except Exception:
            # Every read path independently fails closed on an expired generation;
            # startup re-arms enforcement if this best-effort wake encounters I/O.
            return

    def _active_generation_for_graph(self, account_partition_ref: str, **kwargs):
        generation_id = super()._active_generation_for_graph(
            account_partition_ref, **kwargs
        )
        if generation_id is None:
            return None
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            return None
        expired, _ = self._artifact_retention_state(
            projection, now=_utc(self._retention_clock())
        )
        if not expired:
            return generation_id
        intent = self.activation.get(generation_id)
        if intent is not None and intent.creator_account_id:
            self._expire_active_generation(intent.creator_account_id, generation_id)
        return None

    def retention_due_at(self, creator_account_id: str) -> datetime | None:
        partition_ref = self._partition_ref(creator_account_id)
        generation = self._active_generation_row(partition_ref)
        if generation is None:
            return None
        projection = self._projection_for_generation(str(generation["generation_id"]))
        if projection is None:
            return None
        _, due = self._artifact_retention_state(
            projection,
            now=_utc(self._retention_clock()),
        )
        return due

    def historical_derivation(
        self, creator_account_id: str
    ) -> HistoricalDerivationProvenance | None:
        """Return durable provenance without treating processing time as retention authority."""

        partition_ref = self._partition_ref(creator_account_id)
        generation_id = self._matching_active_generation(
            creator_account_id,
            partition_ref,
        )
        if generation_id is None:
            return None
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            return None
        with self.database.read() as connection:
            generation = connection.execute(
                "SELECT started_at FROM projection_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
        if generation is None:
            return None
        return provenance_for_projection(
            projection,
            derived_at=_stored_time(str(generation["started_at"])),
        )

    def enforce_retention(self, creator_account_id: str) -> bool:
        """Replace an expired active payload with a witnessed empty generation."""
        partition_ref = self._partition_ref(creator_account_id)
        generation = self._active_generation_row(partition_ref)
        if generation is None:
            self._purge_expired_retired_generations()
            return False
        generation_id = str(generation["generation_id"])
        projection = self._projection_for_generation(generation_id)
        if projection is None:
            return False
        expired, _ = self._artifact_retention_state(
            projection,
            now=_utc(self._retention_clock()),
        )
        if not expired:
            self._purge_expired_retired_generations()
            return False
        self._expire_active_generation(creator_account_id, generation_id)
        return True

    def _expire_active_generation(
        self, creator_account_id: str, generation_id: str
    ) -> None:
        with self._retention_lock:
            partition_ref = self._partition_ref(creator_account_id)
            active = self._active_generation_row(partition_ref)
            if active is None or str(active["generation_id"]) != generation_id:
                return
            projection = self._projection_for_generation(generation_id)
            if projection is None:
                return
            expired, _ = self._artifact_retention_state(
                projection,
                now=_utc(self._retention_clock()),
            )
            if not expired:
                return
            identity = self.canonical_identity_reader(creator_account_id)
            if identity is None:
                return
            cleared = RebuildArtifact(
                projection=empty_projection(projection),
                nodes=[],
                edges=[],
            )
            self._expiring_accounts.add(creator_account_id)
            try:
                super().replace_artifact(
                    cleared,
                    creator_account_id=creator_account_id,
                    canonical_identity=identity,
                    force=True,
                )
            finally:
                self._expiring_accounts.discard(creator_account_id)
            # The new empty generation is now the witnessed active generation.
            # The expired predecessor is no longer needed even for rollback.
            with self.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM projection_generations WHERE generation_id=? AND status='retired'",
                    (generation_id,),
                )
            self._purge_expired_retired_generations()
            with self._retention_lock:
                timer = self._retention_timers.pop(creator_account_id, None)
            if timer is not None:
                timer.cancel()

    def _purge_expired_retired_generations(self) -> None:
        now = _utc(self._retention_clock())
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT g.generation_id,p.document_json
                     FROM projection_generations AS g
                     LEFT JOIN analytics_projections AS p
                     ON p.generation_id=g.generation_id
                     AND p.creator_account_id=g.creator_account_id
                   WHERE g.status='retired'"""
            ).fetchall()
        expired_ids: list[str] = []
        for row in rows:
            if row["document_json"] is None:
                expired_ids.append(str(row["generation_id"]))
                continue
            projection = AnalyticsProjection.model_validate_json(row["document_json"])
            expired, _ = self._artifact_retention_state(projection, now=now)
            if expired:
                expired_ids.append(str(row["generation_id"]))
        if not expired_ids:
            return
        with self.database.transaction() as connection:
            for generation_id in expired_ids:
                connection.execute(
                    "DELETE FROM projection_generations WHERE generation_id=? AND status='retired'",
                    (generation_id,),
                )

    def _projection_for_generation(
        self, generation_id: str
    ) -> AnalyticsProjection | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT document_json FROM analytics_projections WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
        return (
            None
            if row is None
            else AnalyticsProjection.model_validate_json(row["document_json"])
        )

    @staticmethod
    def _artifact_retention_state(
        projection: AnalyticsProjection,
        *,
        now: datetime,
    ) -> tuple[bool, datetime | None]:
        if projection.pipeline_revision == CLEAR_PIPELINE_REVISION:
            return False, None
        if not projection.pipeline_revision.startswith(BOUNDED_PIPELINE_PREFIX):
            return True, None
        source_times = [_utc(item.sent_at) for item in projection.message_enrichments]
        if not source_times:
            return False, None
        due = min(source_times) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
        return due <= now, due

    @staticmethod
    def _partition_ref(creator_account_id: str) -> str:
        from app.analytics.opaque_refs import account_ref

        return account_ref(creator_account_id)


class RetentionBoundLazySQLiteAnalyticsProjectionStore(
    LazySQLiteAnalyticsProjectionStore
):
    """Lazy disposable store that opens the retention-aware SQLite implementation."""

    def __init__(self, *args, clock: Callable[[], datetime] = utc_now, **kwargs) -> None:
        self._retention_clock = clock
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            store = self._store
        if isinstance(store, RetentionBoundSQLiteAnalyticsProjectionStore):
            store.close_retention_scheduler()
        super().close()

    def _mark_failed_unlocked(self, failed_store):
        if isinstance(failed_store, RetentionBoundSQLiteAnalyticsProjectionStore):
            failed_store.close_retention_scheduler()
        return super()._mark_failed_unlocked(failed_store)

    def _open_store(self) -> RetentionBoundSQLiteAnalyticsProjectionStore:
        database = ProjectionsDatabase(
            self.path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        return RetentionBoundSQLiteAnalyticsProjectionStore(
            database,
            activation=self.activation,
            canonical_identity_reader=self.canonical_identity_reader,
            lease_seconds=self.lease_seconds,
            rollback_retention=self.rollback_retention,
            gc_batch_size=self.gc_batch_size,
            clock=self._retention_clock,
        )
