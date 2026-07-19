"""Canonical projection activation intents and completed publication witnesses."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Literal, Protocol, runtime_checkable
from uuid import uuid4

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.identity import CanonicalIdentity, canonical_identity
from app.analytics.ownership import BuildOwner
from app.analytics.opaque_refs import account_ref as analytics_account_ref
from app.persistence.database import CanonicalSQLite
from app.persistence.history import HistoryRepository
from app.transport.ingestion import AccountReadModel


ActivationState = Literal["reserved", "completed", "cancelled"]
CanonicalIdentityReader = Callable[[str], CanonicalIdentity | None]


class ProjectionActivationConflict(RuntimeError):
    """Raised when publication no longer matches authoritative canonical state."""


@dataclass(frozen=True, slots=True)
class ProjectionActivationIntent:
    intent_id: str
    creator_account_id: str
    account_ref: str | None
    generation_id: str
    canonical_revision: int
    canonical_content_digest: str
    projection_digest: str
    graph_digest: str
    pipeline_revision: str
    pipeline_config_digest: str
    pipeline_identity_digest: str
    expected_previous_generation_id: str | None
    expected_previous_revision: int | None
    publication_epoch: str
    writer_owner_id: str | None
    writer_owner_pid: int | None
    writer_process_started_at: str | None
    writer_instance_nonce: str | None
    writer_capability_digest: str | None
    publication_capability_digest: str | None
    witness_sequence: int
    state: ActivationState
    reserved_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None

    @property
    def view_revision(self) -> int:
        """Compatibility name for the monotonic witness sequence."""

        return self.witness_sequence


@runtime_checkable
class ProjectionActivationRepository(Protocol):
    def register_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None: ...

    def prepare_publication_epoch_fence(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None: ...

    def release_publication_epoch_fence(self, publication_epoch: str) -> None: ...

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None: ...

    def publication_epoch_is_open(
        self, publication_epoch: str, capability_digest: str
    ) -> bool: ...

    def reserve(
        self,
        *,
        creator_account_id: str,
        account_ref: str,
        generation_id: str,
        canonical_identity: CanonicalIdentity,
        projection_digest: str,
        graph_digest: str,
        pipeline_revision: str,
        pipeline_config_digest: str,
        pipeline_identity_digest: str,
        expected_previous_generation_id: str | None,
        expected_previous_revision: int | None,
        publication_epoch: str,
        writer_owner: BuildOwner,
        publication_capability_digest: str,
    ) -> ProjectionActivationIntent: ...

    def get(self, generation_id: str) -> ProjectionActivationIntent | None: ...

    def pending(self) -> list[ProjectionActivationIntent]: ...

    def complete(
        self, expected: ProjectionActivationIntent
    ) -> ProjectionActivationIntent: ...

    def cancel(self, intent_id: str) -> ProjectionActivationIntent: ...

    def reconcile_completed(
        self, expected: ProjectionActivationIntent
    ) -> ProjectionActivationIntent: ...


class InMemoryProjectionActivationRepository:
    """Reference witness ledger with both reservation and completion CAS checks."""

    def __init__(self, identity: CanonicalIdentityReader) -> None:
        self._identity = identity
        self._intents: dict[str, ProjectionActivationIntent] = {}
        self._by_generation: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._epochs: dict[str, tuple[str, str, Literal["open", "revoked"]]] = {}
        self._lock = RLock()

    def register_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        with self._lock:
            existing = self._epochs.get(publication_epoch)
            expected = (scheduler_owner_id, capability_digest, "open")
            if existing is None:
                self._epochs[publication_epoch] = expected
            elif existing != expected:
                raise ProjectionActivationConflict("publication epoch unavailable")

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        with self._lock:
            existing = self._epochs.get(publication_epoch)
            if (
                existing is None
                or existing[0] != scheduler_owner_id
                or existing[1] != capability_digest
            ):
                raise ProjectionActivationConflict("publication epoch unavailable")
            self._epochs[publication_epoch] = (
                scheduler_owner_id,
                capability_digest,
                "revoked",
            )

    def prepare_publication_epoch_fence(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        with self._lock:
            if self._epochs.get(publication_epoch) != (
                scheduler_owner_id,
                capability_digest,
                "open",
            ):
                raise ProjectionActivationConflict("publication epoch unavailable")

    def release_publication_epoch_fence(self, publication_epoch: str) -> None:
        del publication_epoch

    def publication_epoch_is_open(
        self, publication_epoch: str, capability_digest: str
    ) -> bool:
        with self._lock:
            epoch = self._epochs.get(publication_epoch)
            return bool(
                epoch is not None
                and epoch[1] == capability_digest
                and epoch[2] == "open"
            )

    def reserve(
        self,
        *,
        creator_account_id: str,
        account_ref: str,
        generation_id: str,
        canonical_identity: CanonicalIdentity,
        projection_digest: str,
        graph_digest: str,
        pipeline_revision: str,
        pipeline_config_digest: str,
        pipeline_identity_digest: str,
        expected_previous_generation_id: str | None,
        expected_previous_revision: int | None,
        publication_epoch: str,
        writer_owner: BuildOwner,
        publication_capability_digest: str,
    ) -> ProjectionActivationIntent:
        with self._lock:
            expected = _identity_tuple(
                creator_account_id=creator_account_id,
                account_ref=account_ref,
                generation_id=generation_id,
                canonical_identity=canonical_identity,
                projection_digest=projection_digest,
                graph_digest=graph_digest,
                pipeline_revision=pipeline_revision,
                pipeline_config_digest=pipeline_config_digest,
                pipeline_identity_digest=pipeline_identity_digest,
                expected_previous_generation_id=expected_previous_generation_id,
                expected_previous_revision=expected_previous_revision,
                publication_epoch=publication_epoch,
                writer_owner=writer_owner,
                publication_capability_digest=publication_capability_digest,
            )
            existing_id = self._by_generation.get(generation_id)
            if existing_id is not None:
                existing = self._intents[existing_id]
                _require_intent_identity(existing, expected)
                if existing.state != "completed" and not self._epoch_is_open_locked(
                    publication_epoch, publication_capability_digest
                ):
                    raise ProjectionActivationConflict("publication epoch revoked")
                return existing
            if not self._epoch_is_open_locked(
                publication_epoch, publication_capability_digest
            ):
                raise ProjectionActivationConflict("publication epoch revoked")
            if self._identity(creator_account_id) != canonical_identity:
                raise ProjectionActivationConflict("canonical identity changed")
            if any(
                item.creator_account_id == creator_account_id
                and item.state == "reserved"
                for item in self._intents.values()
            ):
                raise ProjectionActivationConflict(
                    "another activation is already reserved"
                )
            sequence = self._sequences.get(creator_account_id, 0) + 1
            self._sequences[creator_account_id] = sequence
            intent = ProjectionActivationIntent(
                intent_id=str(uuid4()),
                creator_account_id=creator_account_id,
                account_ref=account_ref,
                generation_id=generation_id,
                canonical_revision=canonical_identity.revision,
                canonical_content_digest=canonical_identity.content_digest,
                projection_digest=projection_digest,
                graph_digest=graph_digest,
                pipeline_revision=pipeline_revision,
                pipeline_config_digest=pipeline_config_digest,
                pipeline_identity_digest=pipeline_identity_digest,
                expected_previous_generation_id=expected_previous_generation_id,
                expected_previous_revision=expected_previous_revision,
                publication_epoch=publication_epoch,
                writer_owner_id=writer_owner.owner_id,
                writer_owner_pid=writer_owner.pid,
                writer_process_started_at=writer_owner.process_started_at,
                writer_instance_nonce=writer_owner.instance_nonce,
                writer_capability_digest=writer_owner.capability_digest,
                publication_capability_digest=publication_capability_digest,
                witness_sequence=sequence,
                state="reserved",
                reserved_at=_now(),
            )
            self._intents[intent.intent_id] = intent
            self._by_generation[generation_id] = intent.intent_id
            return intent

    def get(self, generation_id: str) -> ProjectionActivationIntent | None:
        with self._lock:
            intent_id = self._by_generation.get(generation_id)
            return None if intent_id is None else self._intents[intent_id]

    def pending(self) -> list[ProjectionActivationIntent]:
        with self._lock:
            return sorted(
                (item for item in self._intents.values() if item.state == "reserved"),
                key=lambda item: (item.creator_account_id, item.witness_sequence),
            )

    def complete(
        self, expected_intent: ProjectionActivationIntent
    ) -> ProjectionActivationIntent:
        with self._lock:
            current = self._required(expected_intent.intent_id)
            _require_completion_identity(current, expected_intent)
            if current.state == "completed":
                return current
            if current.state != "reserved":
                raise ProjectionActivationConflict("activation is terminal")
            if not self._epoch_is_open_locked(
                current.publication_epoch,
                current.publication_capability_digest or "",
            ):
                cancelled = replace(
                    current,
                    state="cancelled",
                    cancelled_at=_now(),
                )
                self._intents[current.intent_id] = cancelled
                raise ProjectionActivationConflict("publication epoch revoked")
            observed = self._identity(current.creator_account_id)
            expected = CanonicalIdentity(
                current.canonical_revision, current.canonical_content_digest
            )
            if observed != expected:
                cancelled = replace(
                    current,
                    state="cancelled",
                    cancelled_at=_now(),
                )
                self._intents[current.intent_id] = cancelled
                raise ProjectionActivationConflict("canonical identity changed")
            completed = replace(current, state="completed", completed_at=_now())
            self._intents[current.intent_id] = completed
            return completed

    def cancel(self, intent_id: str) -> ProjectionActivationIntent:
        with self._lock:
            current = self._required(intent_id)
            if current.state == "cancelled":
                return current
            if current.state != "reserved":
                raise ProjectionActivationConflict("completed witness is immutable")
            cancelled = replace(current, state="cancelled", cancelled_at=_now())
            self._intents[intent_id] = cancelled
            return cancelled

    def reconcile_completed(
        self, expected: ProjectionActivationIntent
    ) -> ProjectionActivationIntent:
        with self._lock:
            current = self._required(expected.intent_id)
            _require_completion_identity(current, expected)
            if current.state == "cancelled":
                return current
            if current.state != "completed":
                raise ProjectionActivationConflict("activation is not completed")
            reconciled = replace(current, state="cancelled", cancelled_at=_now())
            self._intents[current.intent_id] = reconciled
            return reconciled

    def _required(self, intent_id: str) -> ProjectionActivationIntent:
        current = self._intents.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        return current

    def _epoch_is_open_locked(
        self, publication_epoch: str, capability_digest: str
    ) -> bool:
        epoch = self._epochs.get(publication_epoch)
        return bool(
            epoch is not None
            and epoch[1] == capability_digest
            and epoch[2] == "open"
        )


@dataclass(slots=True)
class _SQLitePublicationFence:
    scheduler_owner_id: str
    capability_digest: str
    connection: sqlite3.Connection | None
    revoked: bool = False
    lock: RLock = field(default_factory=RLock)


class SQLiteProjectionActivationRepository:
    """Witness ledger committed wholly inside authoritative canonical SQLite."""

    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database
        self._publication_fences: dict[str, _SQLitePublicationFence] = {}
        self._publication_fences_lock = RLock()

    def register_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT scheduler_owner_id,scheduler_capability_digest,state
                FROM analytics_projection_publication_epochs WHERE publication_epoch=?
                """,
                (publication_epoch,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO analytics_projection_publication_epochs (
                        publication_epoch,scheduler_owner_id,
                        scheduler_capability_digest,state,opened_at
                    ) VALUES (?, ?, ?, 'open', ?)
                    """,
                    (
                        publication_epoch,
                        scheduler_owner_id,
                        capability_digest,
                        _now().isoformat(),
                    ),
                )
            elif (
                existing["scheduler_owner_id"] != scheduler_owner_id
                or existing["scheduler_capability_digest"] != capability_digest
                or existing["state"] != "open"
            ):
                raise ProjectionActivationConflict("publication epoch unavailable")

    def prepare_publication_epoch_fence(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        """Warm one scheduler-owned connection for its bounded close CAS."""

        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT scheduler_owner_id,scheduler_capability_digest,state
                FROM analytics_projection_publication_epochs WHERE publication_epoch=?
                """,
                (publication_epoch,),
            ).fetchone()
            if (
                row is None
                or row["scheduler_owner_id"] != scheduler_owner_id
                or row["scheduler_capability_digest"] != capability_digest
                or row["state"] != "open"
            ):
                raise ProjectionActivationConflict("publication epoch unavailable")
            with self._publication_fences_lock:
                existing = self._publication_fences.get(publication_epoch)
                if existing is None:
                    self._publication_fences[publication_epoch] = (
                        _SQLitePublicationFence(
                            scheduler_owner_id=scheduler_owner_id,
                            capability_digest=capability_digest,
                            connection=connection,
                        )
                    )
                    connection = None
                elif (
                    existing.scheduler_owner_id != scheduler_owner_id
                    or existing.capability_digest != capability_digest
                    or existing.revoked
                ):
                    raise ProjectionActivationConflict(
                        "publication epoch unavailable"
                    )
        finally:
            if connection is not None:
                connection.close()

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        with self._publication_fences_lock:
            fence = self._publication_fences.get(publication_epoch)
        if fence is not None:
            if (
                fence.scheduler_owner_id != scheduler_owner_id
                or fence.capability_digest != capability_digest
            ):
                raise ProjectionActivationConflict("publication epoch unavailable")
            with fence.lock:
                if fence.revoked:
                    return
                connection = fence.connection
                if connection is not None:
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        self._revoke_publication_epoch_on_connection(
                            connection,
                            publication_epoch,
                            scheduler_owner_id,
                            capability_digest,
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    fence.revoked = True
                    fence.connection = None
                    connection.close()
                    return
        with self.database.transaction() as connection:
            self._revoke_publication_epoch_on_connection(
                connection,
                publication_epoch,
                scheduler_owner_id,
                capability_digest,
            )
        if fence is not None:
            with fence.lock:
                fence.revoked = True

    def release_publication_epoch_fence(self, publication_epoch: str) -> None:
        with self._publication_fences_lock:
            fence = self._publication_fences.pop(publication_epoch, None)
        if fence is None:
            return
        with fence.lock:
            connection = fence.connection
            fence.connection = None
            if connection is not None:
                connection.close()

    @staticmethod
    def _revoke_publication_epoch_on_connection(
        connection: sqlite3.Connection,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_digest: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE analytics_projection_publication_epochs
            SET state='revoked',revoked_at=?
            WHERE publication_epoch=? AND scheduler_owner_id=?
              AND scheduler_capability_digest=? AND state='open'
            """,
            (
                _now().isoformat(),
                publication_epoch,
                scheduler_owner_id,
                capability_digest,
            ),
        )
        if updated.rowcount == 1:
            return
        existing = connection.execute(
            """
            SELECT state FROM analytics_projection_publication_epochs
            WHERE publication_epoch=? AND scheduler_owner_id=?
              AND scheduler_capability_digest=?
            """,
            (publication_epoch, scheduler_owner_id, capability_digest),
        ).fetchone()
        if existing is None or existing["state"] != "revoked":
            raise ProjectionActivationConflict("publication epoch unavailable")

    def publication_epoch_is_open(
        self, publication_epoch: str, capability_digest: str
    ) -> bool:
        with self.database.read() as connection:
            return _sqlite_publication_epoch_open(
                connection, publication_epoch, capability_digest
            )

    def reserve(
        self,
        *,
        creator_account_id: str,
        account_ref: str,
        generation_id: str,
        canonical_identity: CanonicalIdentity,
        projection_digest: str,
        graph_digest: str,
        pipeline_revision: str,
        pipeline_config_digest: str,
        pipeline_identity_digest: str,
        expected_previous_generation_id: str | None,
        expected_previous_revision: int | None,
        publication_epoch: str,
        writer_owner: BuildOwner,
        publication_capability_digest: str,
    ) -> ProjectionActivationIntent:
        expected = _identity_tuple(
            creator_account_id=creator_account_id,
            account_ref=account_ref,
            generation_id=generation_id,
            canonical_identity=canonical_identity,
            projection_digest=projection_digest,
            graph_digest=graph_digest,
            pipeline_revision=pipeline_revision,
            pipeline_config_digest=pipeline_config_digest,
            pipeline_identity_digest=pipeline_identity_digest,
            expected_previous_generation_id=expected_previous_generation_id,
            expected_previous_revision=expected_previous_revision,
            publication_epoch=publication_epoch,
            writer_owner=writer_owner,
            publication_capability_digest=publication_capability_digest,
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM analytics_projection_activation_intents WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                result = _intent(existing)
                _require_intent_identity(result, expected)
                if result.state != "completed" and not _sqlite_publication_epoch_open(
                    connection, publication_epoch, publication_capability_digest
                ):
                    raise ProjectionActivationConflict("publication epoch revoked")
                return result
            if not _sqlite_publication_epoch_open(
                connection, publication_epoch, publication_capability_digest
            ):
                raise ProjectionActivationConflict("publication epoch revoked")
            if _sqlite_identity(connection, creator_account_id) != canonical_identity:
                raise ProjectionActivationConflict("canonical identity changed")
            pending = connection.execute(
                """
                SELECT 1 FROM analytics_projection_activation_intents
                WHERE creator_account_id = ? AND state = 'reserved'
                """,
                (creator_account_id,),
            ).fetchone()
            if pending is not None:
                raise ProjectionActivationConflict(
                    "another activation is already reserved"
                )
            sequence = int(
                connection.execute(
                    """
                    INSERT INTO analytics_projection_witness_sequences (
                        creator_account_id, last_witness_sequence
                    ) VALUES (?, 1)
                    ON CONFLICT (creator_account_id) DO UPDATE SET
                        last_witness_sequence = last_witness_sequence + 1
                    RETURNING last_witness_sequence
                    """,
                    (creator_account_id,),
                ).fetchone()[0]
            )
            now = _now()
            intent_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO analytics_projection_activation_intents (
                    intent_id, creator_account_id, generation_id,
                    account_ref,
                    canonical_revision, canonical_content_digest,
                    projection_digest, graph_digest, pipeline_revision,
                    pipeline_config_digest, pipeline_identity_digest,
                    expected_previous_generation_id, expected_previous_revision,
                    publication_epoch, witness_sequence, state, reserved_at,
                    writer_owner_id, writer_owner_pid,
                    writer_process_started_at, writer_instance_nonce,
                    writer_capability_digest, publication_capability_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    creator_account_id,
                    generation_id,
                    account_ref,
                    canonical_identity.revision,
                    canonical_identity.content_digest,
                    projection_digest,
                    graph_digest,
                    pipeline_revision,
                    pipeline_config_digest,
                    pipeline_identity_digest,
                    expected_previous_generation_id,
                    expected_previous_revision,
                    publication_epoch,
                    sequence,
                    now.isoformat(),
                    writer_owner.owner_id,
                    writer_owner.pid,
                    writer_owner.process_started_at,
                    writer_owner.instance_nonce,
                    writer_owner.capability_digest,
                    publication_capability_digest,
                ),
            )
            return ProjectionActivationIntent(
                intent_id=intent_id,
                creator_account_id=creator_account_id,
                account_ref=account_ref,
                generation_id=generation_id,
                canonical_revision=canonical_identity.revision,
                canonical_content_digest=canonical_identity.content_digest,
                projection_digest=projection_digest,
                graph_digest=graph_digest,
                pipeline_revision=pipeline_revision,
                pipeline_config_digest=pipeline_config_digest,
                pipeline_identity_digest=pipeline_identity_digest,
                expected_previous_generation_id=expected_previous_generation_id,
                expected_previous_revision=expected_previous_revision,
                publication_epoch=publication_epoch,
                writer_owner_id=writer_owner.owner_id,
                writer_owner_pid=writer_owner.pid,
                writer_process_started_at=writer_owner.process_started_at,
                writer_instance_nonce=writer_owner.instance_nonce,
                writer_capability_digest=writer_owner.capability_digest,
                publication_capability_digest=publication_capability_digest,
                witness_sequence=sequence,
                state="reserved",
                reserved_at=now,
            )

    def get(self, generation_id: str) -> ProjectionActivationIntent | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM analytics_projection_activation_intents WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            return None if row is None else _intent(row)

    def pending(self) -> list[ProjectionActivationIntent]:
        with self.database.read() as connection:
            return [
                _intent(row)
                for row in connection.execute(
                    """
                    SELECT * FROM analytics_projection_activation_intents
                    WHERE state = 'reserved'
                    ORDER BY creator_account_id, witness_sequence
                    """
                )
            ]

    def complete(
        self, expected_intent: ProjectionActivationIntent
    ) -> ProjectionActivationIntent:
        failure: str | None = None
        result: ProjectionActivationIntent | None = None
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM analytics_projection_activation_intents WHERE intent_id = ?",
                (expected_intent.intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError("projection_activation_missing")
            current = _intent(row)
            _require_completion_identity(current, expected_intent)
            if current.state == "completed":
                return current
            if current.state != "reserved":
                raise ProjectionActivationConflict("activation is terminal")
            expected = CanonicalIdentity(
                current.canonical_revision, current.canonical_content_digest
            )
            now = _now()
            identity_changed = (
                _sqlite_identity(connection, current.creator_account_id) != expected
            )
            epoch_revoked = not _sqlite_publication_epoch_open(
                connection,
                current.publication_epoch,
                current.publication_capability_digest or "",
            )
            if identity_changed or epoch_revoked:
                connection.execute(
                    """
                    UPDATE analytics_projection_activation_intents
                    SET state = 'cancelled', cancelled_at = ?
                    WHERE intent_id = ? AND state = 'reserved'
                    """,
                    (now.isoformat(), current.intent_id),
                )
                failure = (
                    "canonical identity changed"
                    if identity_changed
                    else "publication epoch revoked"
                )
                result = replace(current, state="cancelled", cancelled_at=now)
            else:
                updated = connection.execute(
                    """
                    UPDATE analytics_projection_activation_intents
                    SET state = 'completed', completed_at = ?
                    WHERE intent_id = ? AND state = 'reserved'
                      AND creator_account_id=? AND generation_id=?
                      AND canonical_revision=? AND canonical_content_digest=?
                      AND projection_digest=? AND graph_digest=?
                      AND pipeline_revision=? AND pipeline_config_digest=?
                      AND pipeline_identity_digest=? AND witness_sequence=?
                      AND expected_previous_generation_id IS ?
                      AND expected_previous_revision IS ?
                      AND publication_epoch=?
                      AND account_ref=? AND writer_owner_id=?
                      AND writer_owner_pid=? AND writer_process_started_at=?
                      AND writer_instance_nonce=? AND writer_capability_digest=?
                      AND publication_capability_digest=?
                    """,
                    (
                        now.isoformat(),
                        current.intent_id,
                        current.creator_account_id,
                        current.generation_id,
                        current.canonical_revision,
                        current.canonical_content_digest,
                        current.projection_digest,
                        current.graph_digest,
                        current.pipeline_revision,
                        current.pipeline_config_digest,
                        current.pipeline_identity_digest,
                        current.witness_sequence,
                        current.expected_previous_generation_id,
                        current.expected_previous_revision,
                        current.publication_epoch,
                        current.account_ref,
                        current.writer_owner_id,
                        current.writer_owner_pid,
                        current.writer_process_started_at,
                        current.writer_instance_nonce,
                        current.writer_capability_digest,
                        current.publication_capability_digest,
                    ),
                )
                if updated.rowcount != 1:
                    raise ProjectionActivationConflict("activation CAS failed")
                result = replace(current, state="completed", completed_at=now)
        if failure is not None:
            raise ProjectionActivationConflict(failure)
        assert result is not None
        return result

    def cancel(self, intent_id: str) -> ProjectionActivationIntent:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM analytics_projection_activation_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            current = _intent(row)
            if current.state == "cancelled":
                return current
            if current.state != "reserved":
                raise ProjectionActivationConflict("completed witness is immutable")
            now = _now()
            connection.execute(
                """
                UPDATE analytics_projection_activation_intents
                SET state = 'cancelled', cancelled_at = ?
                WHERE intent_id = ? AND state = 'reserved'
                """,
                (now.isoformat(), intent_id),
            )
            return replace(current, state="cancelled", cancelled_at=now)

    def reconcile_completed(
        self, expected: ProjectionActivationIntent
    ) -> ProjectionActivationIntent:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM analytics_projection_activation_intents WHERE intent_id=?",
                (expected.intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError("projection_activation_missing")
            current = _intent(row)
            _require_completion_identity(current, expected)
            if current.state == "cancelled":
                return current
            if current.state != "completed":
                raise ProjectionActivationConflict("activation is not completed")
            now = _now()
            updated = connection.execute(
                """
                UPDATE analytics_projection_activation_intents
                SET state='cancelled', cancelled_at=?
                WHERE intent_id=? AND state='completed'
                """,
                (now.isoformat(), current.intent_id),
            )
            if updated.rowcount != 1:
                raise ProjectionActivationConflict("activation reconciliation CAS failed")
            return replace(current, state="cancelled", cancelled_at=now)


def _sqlite_identity(
    connection: sqlite3.Connection, creator_account_id: str
) -> CanonicalIdentity | None:
    row = connection.execute(
        "SELECT canonical_revision FROM account_heads WHERE creator_account_id = ?",
        (creator_account_id,),
    ).fetchone()
    if row is None:
        return None
    source = HistoryAnalyticsSource(
        HistoryRepository.__new__(HistoryRepository),
        connection=connection,
    )
    account = source.account_read_model(creator_account_id)
    return canonical_identity(account)


def _sqlite_publication_epoch_open(
    connection: sqlite3.Connection,
    publication_epoch: str,
    capability_digest: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM analytics_projection_publication_epochs
        WHERE publication_epoch=? AND scheduler_capability_digest=?
          AND state='open'
        """,
        (publication_epoch, capability_digest),
    ).fetchone()
    return row is not None


def _identity_tuple(
    *,
    creator_account_id: str,
    account_ref: str,
    generation_id: str,
    canonical_identity: CanonicalIdentity,
    projection_digest: str,
    graph_digest: str,
    pipeline_revision: str,
    pipeline_config_digest: str,
    pipeline_identity_digest: str,
    expected_previous_generation_id: str | None,
    expected_previous_revision: int | None,
    publication_epoch: str,
    writer_owner: BuildOwner,
    publication_capability_digest: str,
) -> tuple[object, ...]:
    if account_ref != analytics_account_ref(creator_account_id):
        raise ProjectionActivationConflict("activation account reference differs")
    return (
        creator_account_id,
        account_ref,
        generation_id,
        canonical_identity.revision,
        canonical_identity.content_digest,
        projection_digest,
        graph_digest,
        pipeline_revision,
        pipeline_config_digest,
        pipeline_identity_digest,
        expected_previous_generation_id,
        expected_previous_revision,
        publication_epoch,
        writer_owner.owner_id,
        writer_owner.pid,
        writer_owner.process_started_at,
        writer_owner.instance_nonce,
        writer_owner.capability_digest,
        publication_capability_digest,
    )


def _require_intent_identity(
    intent: ProjectionActivationIntent, expected: tuple[object, ...]
) -> None:
    observed = (
        intent.creator_account_id,
        intent.account_ref,
        intent.generation_id,
        intent.canonical_revision,
        intent.canonical_content_digest,
        intent.projection_digest,
        intent.graph_digest,
        intent.pipeline_revision,
        intent.pipeline_config_digest,
        intent.pipeline_identity_digest,
        intent.expected_previous_generation_id,
        intent.expected_previous_revision,
        intent.publication_epoch,
        intent.writer_owner_id,
        intent.writer_owner_pid,
        intent.writer_process_started_at,
        intent.writer_instance_nonce,
        intent.writer_capability_digest,
        intent.publication_capability_digest,
    )
    if observed != expected:
        raise ProjectionActivationConflict("activation identity differs")


def _require_completion_identity(
    observed: ProjectionActivationIntent, expected: ProjectionActivationIntent
) -> None:
    if (
        observed.intent_id,
        observed.creator_account_id,
        observed.account_ref,
        observed.generation_id,
        observed.canonical_revision,
        observed.canonical_content_digest,
        observed.projection_digest,
        observed.graph_digest,
        observed.pipeline_revision,
        observed.pipeline_config_digest,
        observed.pipeline_identity_digest,
        observed.expected_previous_generation_id,
        observed.expected_previous_revision,
        observed.publication_epoch,
        observed.writer_owner_id,
        observed.writer_owner_pid,
        observed.writer_process_started_at,
        observed.writer_instance_nonce,
        observed.writer_capability_digest,
        observed.publication_capability_digest,
        observed.witness_sequence,
        observed.reserved_at,
    ) != (
        expected.intent_id,
        expected.creator_account_id,
        expected.account_ref,
        expected.generation_id,
        expected.canonical_revision,
        expected.canonical_content_digest,
        expected.projection_digest,
        expected.graph_digest,
        expected.pipeline_revision,
        expected.pipeline_config_digest,
        expected.pipeline_identity_digest,
        expected.expected_previous_generation_id,
        expected.expected_previous_revision,
        expected.publication_epoch,
        expected.writer_owner_id,
        expected.writer_owner_pid,
        expected.writer_process_started_at,
        expected.writer_instance_nonce,
        expected.writer_capability_digest,
        expected.publication_capability_digest,
        expected.witness_sequence,
        expected.reserved_at,
    ):
        raise ProjectionActivationConflict("activation completion identity differs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _intent(row: sqlite3.Row) -> ProjectionActivationIntent:
    return ProjectionActivationIntent(
        intent_id=row["intent_id"],
        creator_account_id=row["creator_account_id"],
        account_ref=row["account_ref"],
        generation_id=row["generation_id"],
        canonical_revision=int(row["canonical_revision"]),
        canonical_content_digest=row["canonical_content_digest"],
        projection_digest=row["projection_digest"],
        graph_digest=row["graph_digest"],
        pipeline_revision=row["pipeline_revision"],
        pipeline_config_digest=row["pipeline_config_digest"],
        pipeline_identity_digest=row["pipeline_identity_digest"],
        expected_previous_generation_id=row["expected_previous_generation_id"],
        expected_previous_revision=(
            None
            if row["expected_previous_revision"] is None
            else int(row["expected_previous_revision"])
        ),
        publication_epoch=row["publication_epoch"],
        writer_owner_id=row["writer_owner_id"],
        writer_owner_pid=(
            None if row["writer_owner_pid"] is None else int(row["writer_owner_pid"])
        ),
        writer_process_started_at=row["writer_process_started_at"],
        writer_instance_nonce=row["writer_instance_nonce"],
        writer_capability_digest=row["writer_capability_digest"],
        publication_capability_digest=row["publication_capability_digest"],
        witness_sequence=int(row["witness_sequence"]),
        state=row["state"],
        reserved_at=datetime.fromisoformat(row["reserved_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"])
            if row["completed_at"] is not None
            else None
        ),
        cancelled_at=(
            datetime.fromisoformat(row["cancelled_at"])
            if row["cancelled_at"] is not None
            else None
        ),
    )
