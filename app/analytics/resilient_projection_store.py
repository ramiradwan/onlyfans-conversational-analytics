"""Lazy, disposable SQLite projection storage with scheduler-owned recovery."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from app.analytics.database import ProjectionsDatabase
from app.analytics.errors import ProjectionStorageUnavailable
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.identity import CanonicalIdentity
from app.analytics.sqlite_projection_store import (
    ProjectionValidationError,
    SQLiteAnalyticsProjectionStore,
)
from app.persistence.database import LocalSQLite, SQLiteConfigurationError
from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    reject_path_aliases,
    sync_directory,
)
from app.persistence.projection_activation import ProjectionActivationRepository


FailureCallback = Callable[[str | None], None]
FileIdentity = tuple[int, int]
StoreIdentity = tuple[int, str, str, str | None]


class LazySQLiteAnalyticsProjectionStore:
    """Contain projection failures until the scheduler repairs the disposable DB."""

    def __init__(
        self,
        path: str | Path,
        *,
        activation: ProjectionActivationRepository,
        canonical_identity_reader: Callable[[str], CanonicalIdentity | None],
        busy_timeout_ms: int = 5_000,
        lease_seconds: float = 120.0,
        rollback_retention: int = 1,
        gc_batch_size: int = 8,
        retry_backoff_seconds: float = 0.05,
    ) -> None:
        try:
            self.path = reject_path_aliases(path)
        except PrivateFileSecurityError as error:
            raise ValueError("projection_path_unsafe") from error
        self.activation = activation
        self.canonical_identity_reader = canonical_identity_reader
        self.busy_timeout_ms = busy_timeout_ms
        self.lease_seconds = lease_seconds
        self.rollback_retention = rollback_retention
        self.gc_batch_size = gc_batch_size
        self.retry_backoff_seconds = retry_backoff_seconds
        self._store: SQLiteAnalyticsProjectionStore | None = None
        self._lock = RLock()
        self._failure_callback: FailureCallback | None = None
        self._next_retry_at = 0.0
        self._failure_count = 0
        self._recovery_count = 0
        self._needs_recovery = False
        self._failure_notified = False
        self._closed = False
        self._file_identity: FileIdentity | None = None
        self._store_identity: StoreIdentity | None = None
        self.graph = _LazySQLiteGraphReader(self)

    @property
    def database(self) -> ProjectionsDatabase | None:
        with self._lock:
            return None if self._store is None else self._store.database

    @property
    def recovery_count(self) -> int:
        with self._lock:
            return self._recovery_count

    def set_failure_callback(self, callback: FailureCallback | None) -> None:
        with self._lock:
            self._failure_callback = callback

    def ensure_ready(self) -> None:
        """Scheduler-only mutation seam that opens, repairs, or recreates storage."""

        with self._lock:
            if self._closed:
                raise ProjectionStorageUnavailable()
            if (
                self._store is not None
                and self.path.exists()
                and not self._needs_recovery
            ):
                if self._identity_matches_unlocked(self._store):
                    return
                self._mark_failed_unlocked(self._store)
            if time.monotonic() < self._next_retry_at:
                raise ProjectionStorageUnavailable()
            recover_first = self._needs_recovery
            self._store = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if recover_first:
                    self._quarantine_unlocked()
                store = self._open_store()
            except Exception:
                if recover_first:
                    self._record_recovery_failure_unlocked()
                    raise ProjectionStorageUnavailable() from None
                try:
                    self._quarantine_unlocked()
                    store = self._open_store()
                except Exception:
                    self._record_recovery_failure_unlocked()
                    raise ProjectionStorageUnavailable() from None
            self._store = store
            self._file_identity = self._file_identity_for_path()
            self._store_identity = store.database.store_identity()
            self._needs_recovery = False
            self._failure_notified = False
            self._failure_count = 0
            self._next_retry_at = 0.0
            self._recovery_count += 1

    def get(self, creator_account_id: str, **kwargs):
        return self._read("get", creator_account_id, creator_account_id, **kwargs)

    def get_artifact(self, creator_account_id: str, **kwargs):
        return self._read(
            "get_artifact", creator_account_id, creator_account_id, **kwargs
        )

    def replace(self, projection, *, creator_account_id: str, **kwargs):
        return self._write(
            "replace",
            creator_account_id,
            projection,
            creator_account_id=creator_account_id,
            **kwargs,
        )

    def replace_artifact(self, artifact, **kwargs):
        account_id = kwargs.get("creator_account_id")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("canonical_account_required")
        return self._write(
            "replace_artifact",
            account_id,
            artifact,
            **kwargs,
        )

    def stage_artifact(self, artifact, **kwargs):
        account_id = kwargs.get("creator_account_id")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("canonical_account_required")
        return self._write(
            "stage_artifact",
            account_id,
            artifact,
            **kwargs,
        )

    def publish_generation(self, generation_id: str, **kwargs):
        return self._write("publish_generation", None, generation_id, **kwargs)

    def discard_generation(self, generation_id: str) -> None:
        self._write("discard_generation", None, generation_id)

    def next_projection_generation(self, creator_account_id: str) -> int:
        return self._read(
            "next_projection_generation", creator_account_id, creator_account_id
        )

    def open_publication_epoch(
        self,
        scheduler_owner_id: str,
        capability_secret: str,
        *,
        retain_fence_connection: bool = False,
    ) -> str:
        return self._write(
            "open_publication_epoch",
            None,
            scheduler_owner_id,
            capability_secret,
            retain_fence_connection=retain_fence_connection,
        )

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_secret: str,
    ) -> None:
        self._write(
            "revoke_publication_epoch",
            None,
            publication_epoch,
            scheduler_owner_id,
            capability_secret,
        )

    def fence_publication_epoch(self, publication_epoch: str) -> None:
        with self._lock:
            store = self._store
        if store is not None:
            store.fence_publication_epoch(publication_epoch)

    def clear(self, creator_account_id: str) -> None:
        self._write("clear", creator_account_id, creator_account_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._store = None
            self._file_identity = None
            self._store_identity = None

    def _read(self, method: str, account_id: str | None, *args, **kwargs):
        store = self._available_store(account_id)
        try:
            return getattr(store, method)(*args, **kwargs)
        except _STORAGE_FAILURES:
            self._mark_failed(store, account_id)
            raise ProjectionStorageUnavailable() from None

    def _write(self, method: str, account_id: str | None, *args, **kwargs):
        store = self._available_store(account_id)
        try:
            return getattr(store, method)(*args, **kwargs)
        except _STORAGE_FAILURES:
            self._mark_failed(store, account_id)
            raise ProjectionStorageUnavailable() from None

    def _available_store(
        self, account_id: str | None
    ) -> SQLiteAnalyticsProjectionStore:
        with self._lock:
            store = self._store
            available = (
                not self._closed
                and store is not None
                and self.path.exists()
                and self._identity_matches_unlocked(store)
            )
        if not available:
            self._mark_failed(store, account_id)
            raise ProjectionStorageUnavailable()
        assert store is not None
        return store

    def _mark_failed(
        self,
        failed_store: SQLiteAnalyticsProjectionStore | None,
        account_id: str | None,
    ) -> None:
        callback: FailureCallback | None = None
        with self._lock:
            if self._store is not failed_store:
                return
            callback = self._mark_failed_unlocked(failed_store)
        if callback is not None:
            callback(account_id)

    def _mark_failed_unlocked(
        self, failed_store: SQLiteAnalyticsProjectionStore | None
    ) -> FailureCallback | None:
        if self._store is not failed_store:
            return None
        self._store = None
        self._file_identity = None
        self._store_identity = None
        self._needs_recovery = True
        if self._failure_notified:
            return None
        self._failure_notified = True
        return self._failure_callback

    def _identity_matches_unlocked(
        self, store: SQLiteAnalyticsProjectionStore
    ) -> bool:
        try:
            file_identity = self._file_identity_for_path()
            observed = store.database.store_identity()
        except Exception:
            return False
        expected = self._store_identity
        if (
            self._file_identity != file_identity
            or expected is None
            or observed[:3] != expected[:3]
        ):
            return False
        # The witness is deliberately retained as part of the cached identity.
        # It may advance through this store's own concurrent publications, while
        # the immutable store id and OS file id distinguish file replacement.
        self._store_identity = observed
        return True

    def _file_identity_for_path(self) -> FileIdentity:
        metadata = self.path.stat()
        return int(metadata.st_dev), int(metadata.st_ino)

    def _record_recovery_failure_unlocked(self) -> None:
        self._failure_count += 1
        delay = min(
            2.0,
            self.retry_backoff_seconds
            * (2 ** min(self._failure_count - 1, 6)),
        )
        self._next_retry_at = time.monotonic() + delay
        self._store = None
        self._file_identity = None
        self._store_identity = None
        self._needs_recovery = True

    def _open_store(self) -> SQLiteAnalyticsProjectionStore:
        database = ProjectionsDatabase(
            self.path,
            busy_timeout_ms=self.busy_timeout_ms,
        )
        return SQLiteAnalyticsProjectionStore(
            database,
            activation=self.activation,
            canonical_identity_reader=self.canonical_identity_reader,
            lease_seconds=self.lease_seconds,
            rollback_retention=self.rollback_retention,
            gc_batch_size=self.gc_batch_size,
        )

    def _quarantine_unlocked(self) -> None:
        self._store = None
        with LocalSQLite.exclusive_lifecycle(self.path):
            if LocalSQLite.open_connection_count(self.path):
                raise SQLiteConfigurationError("projection_storage_in_use")
            nonce = uuid4().hex
            moved = False
            for suffix in ("", "-wal", "-shm"):
                source = Path(f"{self.path}{suffix}")
                if not source.exists():
                    continue
                quarantine = self.path.with_name(
                    f".{self.path.name}.{nonce}.quarantine{suffix}"
                )
                os.replace(source, quarantine)
                apply_private_file_security(quarantine)
                moved = True
            if moved:
                sync_directory(self.path.parent)

class _LazySQLiteGraphReader:
    def __init__(self, owner: LazySQLiteAnalyticsProjectionStore) -> None:
        self._owner = owner

    def partition_revision(self, partition_key: str):
        return self._graph_read(
            "partition_revision", partition_key, partition_key
        )

    def get_node(self, partition_key: str, node_id: str):
        return self._graph_read("get_node", partition_key, partition_key, node_id)

    def get_edge(self, partition_key: str, edge_id: str):
        return self._graph_read("get_edge", partition_key, partition_key, edge_id)

    def nodes(self, partition_key: str):
        return self._graph_read("nodes", partition_key, partition_key)

    def edges(self, partition_key: str):
        return self._graph_read("edges", partition_key, partition_key)

    def neighborhood(self, partition_key: str, node_id: str, **kwargs):
        return self._graph_read(
            "neighborhood", partition_key, partition_key, node_id, **kwargs
        )

    def find_paths(self, partition_key: str, source_id: str, target_id: str, **kwargs):
        return self._graph_read(
            "find_paths",
            partition_key,
            partition_key,
            source_id,
            target_id,
            **kwargs,
        )

    def degree(self, partition_key: str, node_id: str, **kwargs):
        return self._graph_read(
            "degree", partition_key, partition_key, node_id, **kwargs
        )

    def compute_centrality(self, partition_key: str, **kwargs):
        return self._graph_read(
            "compute_centrality", partition_key, partition_key, **kwargs
        )

    def detect_communities(self, partition_key: str, **kwargs):
        return self._graph_read(
            "detect_communities", partition_key, partition_key, **kwargs
        )

    def _graph_read(self, method: str, account_id: str, *args, **kwargs):
        store = self._owner._available_store(account_id)
        try:
            return getattr(store.graph, method)(*args, **kwargs)
        except _STORAGE_FAILURES:
            self._owner._mark_failed(store, account_id)
            raise ProjectionStorageUnavailable() from None


_STORAGE_FAILURES = (
    sqlite3.DatabaseError,
    OSError,
    PrivateFileSecurityError,
    SQLiteConfigurationError,
    ProjectionValidationError,
    GraphReferentialIntegrityError,
    ValidationError,
)
