"""Witnessed immutable SQLite generations for projections and graph rows."""

from __future__ import annotations

import json
import secrets
from app.persistence import sqlite_api as sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.database import ProjectionsDatabase, generation_verification_cache
from app.analytics.compact_graph import CompactArtifact, write_compact_graph
from app.analytics.graph_privacy import safe_graph_records
from app.analytics.projection_encoding import projection_document
from app.analytics.graph_store import (
    GraphDeadlineExceeded,
    GraphReferentialIntegrityError,
)
from app.analytics.identity import (
    CanonicalIdentity,
    pipeline_identity_digest,
)
from app.analytics.opaque_refs import (
    AccountPartitionRef,
    account_ref,
    validated_account_ref,
)
from app.analytics.ownership import (
    capability_digest,
    current_build_owner,
    process_is_definitely_dead,
)
from app.analytics.projection_store import (
    CLEAR_PIPELINE_REVISION,
    ProjectionRevisionConflict,
    empty_projection,
    projection_content_digest,
)
from app.analytics.sqlite_graph_store import (
    SQLiteGraphGenerationWriter,
    SQLiteGraphReader,
    _edge,
    _edge_parameters,
    _graph_digest,
    _json,
    _node,
    _node_parameters,
)
from app.models.analytics import AnalyticsProjection, RebuildArtifact
from app.persistence.projection_activation import (
    ProjectionActivationConflict,
    ProjectionActivationIntent,
    ProjectionActivationRepository,
)


PROJECTION_BUILD_VERSION = "analytics.projection.v3"
CrashHook = Callable[[str, str], None]
CanonicalIdentityReader = Callable[[str], CanonicalIdentity | None]


class ProjectionValidationError(RuntimeError):
    """Raised when persisted rows do not reproduce their validated metadata."""


class ProjectionReconciliationError(RuntimeError):
    """Raised when durable activation records contradict each other."""


class SQLiteAnalyticsProjectionStore:
    """Only exposes generations backed by an exact completed canonical witness."""

    def __init__(
        self,
        database: ProjectionsDatabase | str | Path,
        *,
        activation: ProjectionActivationRepository,
        canonical_identity_reader: CanonicalIdentityReader,
        crash_hook: CrashHook | None = None,
        reconcile: bool = True,
        owner_id: str | None = None,
        lease_seconds: float = 120.0,
        rollback_retention: int = 1,
        gc_batch_size: int = 8,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if rollback_retention < 0 or gc_batch_size <= 0:
            raise ValueError("generation retention and GC batch must be bounded")
        self.database = (
            database
            if isinstance(database, ProjectionsDatabase)
            else ProjectionsDatabase(database)
        )
        self.activation = activation
        self.canonical_identity_reader = canonical_identity_reader
        self.crash_hook = crash_hook
        self.build_owner = current_build_owner(owner_id)
        self.owner_id = self.build_owner.owner_id
        self._direct_publication_secret = secrets.token_hex(32)
        self._locally_fenced_epochs: set[str] = set()
        self.lease_seconds = lease_seconds
        self.rollback_retention = rollback_retention
        self.gc_batch_size = gc_batch_size
        self.graph = SQLiteGraphReader(
            self.database,
            active_generation_resolver=self._active_generation_for_graph,
        )
        if reconcile:
            self.reconcile_startup()

    def generation_references_supported(self) -> bool:
        with self.database.read() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 7

    def check_generation_reference(self, account_id, reference):
        from app.analytics.generation_reference import check_reference

        check_reference(self, account_id, reference)

    def read_generation_artifact(self, account_id, reference):
        from app.analytics.generation_reference import read_referenced_artifact

        return read_referenced_artifact(self, account_id, reference)

    def open_conversation_fragments(self, account_id):
        from app.analytics.conversation_sql import fragment_reader

        return fragment_reader(self, account_id)

    def load_conversation_fragment(self, account_id, conversation, input_digest, config_digest, **kwargs):
        from app.analytics.conversation_sql import load_fragment

        return load_fragment(self, account_id, conversation, input_digest, config_digest, **kwargs)

    def load_enrichment_entries(self, account_id, keys, **kwargs):
        from app.analytics.enrichment_sql import load_entries

        return load_entries(self, account_id, keys, **kwargs)

    def question_pricing(self, account_id, snapshot, references, budget):
        from app.analytics.query_publication import published_pricing

        return published_pricing(self, account_id, snapshot, references, budget)

    def question_snapshot(self, account_id, canonical_identity, budget):
        """Return witnessed metadata for a bounded live-source question read."""

        from app.analytics.query_publication import published_snapshot

        return published_snapshot(self, account_id, canonical_identity, budget)

    def get(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> AnalyticsProjection | None:
        partition_ref = account_ref(creator_account_id)
        generation_id = self._matching_active_generation(
            creator_account_id,
            partition_ref,
            canonical_identity=canonical_identity,
        )
        if generation_id is None:
            return None
        values = self._validate_persisted_generation(generation_id)
        return values["projection"]

    def get_artifact(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> RebuildArtifact | None:
        partition_ref = account_ref(creator_account_id)
        generation_id = self._matching_active_generation(
            creator_account_id,
            partition_ref,
            canonical_identity=canonical_identity,
        )
        if generation_id is None:
            return None
        values = self._validate_persisted_generation(generation_id, materialize_graph=True)
        return RebuildArtifact(projection=values["projection"],
                               nodes=values["nodes"], edges=values["edges"])

    def replace(
        self,
        projection: AnalyticsProjection,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> bool:
        if projection.graph.node_count or projection.graph.edge_count:
            raise ValueError("graph content must be replaced as one artifact")
        if projection.account_ref != account_ref(creator_account_id):
            raise ProjectionActivationConflict("canonical account is unavailable")
        identity = canonical_identity or self.canonical_identity_reader(
            creator_account_id
        )
        if identity is None:
            raise ProjectionActivationConflict("canonical account is unavailable")
        return self.replace_artifact(
            RebuildArtifact(projection=projection, nodes=[], edges=[]),
            creator_account_id=creator_account_id,
            canonical_identity=identity,
        )

    def next_projection_generation(self, creator_account_id: str) -> int:
        partition_ref = account_ref(creator_account_id)
        with self.database.read() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 7:
                rows = connection.execute(
                    "SELECT document_json FROM analytics_projections WHERE creator_account_id=?",
                    (partition_ref,),
                )
                return max((AnalyticsProjection.model_validate_json(row[0]).projection_generation
                            for row in rows), default=0) + 1
            row = connection.execute(
                """SELECT MAX(q.projection_generation), COUNT(*), COUNT(q.generation_id)
                   FROM analytics_projections p
                   JOIN projection_generations g USING (generation_id,creator_account_id)
                   LEFT JOIN projection_query_metadata q USING (generation_id,creator_account_id)
                   WHERE g.creator_account_id=?""", (partition_ref,),
            ).fetchone()
        if row[1] != row[2]:
            raise ProjectionValidationError("projection_sequence_metadata_missing")
        return int(row[0] or 0) + 1

    def replace_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
        force: bool = False,
        cancellation_check: CancellationCheck | None = None,
    ) -> bool:
        if artifact.projection.account_ref != account_ref(creator_account_id):
            raise ProjectionActivationConflict("canonical account is unavailable")
        identity = canonical_identity or self.canonical_identity_reader(
            creator_account_id
        )
        if identity is None:
            raise ProjectionActivationConflict("canonical account is unavailable")
        if not force and self._artifact_matches_active(
            creator_account_id, artifact, identity
        ):
            return False
        generation_id = self.stage_artifact(
            artifact,
            creator_account_id=creator_account_id,
            canonical_identity=identity,
            cancellation_check=cancellation_check,
        )
        return self.publish_generation(
            generation_id,
            creator_account_id=creator_account_id,
            canonical_identity=identity,
            cancellation_check=cancellation_check,
        )

    def stage_built_artifact(self, artifact, **kwargs):
        """Validate a privately owned build without cloning its complete graph."""

        return self.stage_artifact(artifact, _copy_graph=False, **kwargs)

    def stage_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        publication_epoch: str | None = None,
        cancellation_check: CancellationCheck | None = None,
        enrichment_entries: tuple[bytes, ...] = (),
        conversation_fragments: tuple[bytes, ...] = (),
        conversation_pages: tuple = (),
        _copy_graph: bool = True,
    ) -> str:
        """Persist and validate one inactive generation from one canonical snapshot."""

        from app.analytics.enrichment_cache import validate_entries
        from app.analytics.enrichment_sql import insert_entries

        from app.analytics.conversation_reuse import iter_validated_fragments
        from app.analytics.conversation_sql import insert_fragments

        from app.analytics.conversation_pages import validate_page_sets
        from app.analytics.conversation_page_sql import insert_page_sets
        from app.analytics.conversation_reuse import MAX_FRAGMENTS, MAX_FRAGMENT_TOTAL_BYTES
        if (len(conversation_fragments) + len(conversation_pages) > MAX_FRAGMENTS
                or sum(map(len, conversation_fragments)) + sum(p.retained_bytes for p in conversation_pages)
                    > MAX_FRAGMENT_TOTAL_BYTES):
            raise ValueError('conversation_fragment_budget_invalid')
        check = lambda: check_cancelled(cancellation_check)
        validate_page_sets(artifact, conversation_pages, check=check)
        fragments = iter_validated_fragments(artifact, conversation_fragments)
        cached = validate_entries(artifact, enrichment_entries)
        check_cancelled(cancellation_check)
        projection = artifact.projection
        partition_ref = account_ref(creator_account_id)
        if projection.account_ref != partition_ref:
            raise ProjectionValidationError("projection account reference differs")
        if canonical_identity.revision != projection.source_revision:
            raise ProjectionValidationError("canonical and projection revisions differ")
        if projection.canonical_content_digest != canonical_identity.content_digest:
            raise ProjectionValidationError("canonical projection identity differs")
        if self.canonical_identity_reader(creator_account_id) != canonical_identity:
            raise ProjectionActivationConflict("canonical identity changed")
        compact = isinstance(artifact, CompactArtifact)
        if compact:
            if _copy_graph:
                raise ProjectionValidationError("compact_graph_requires_owned_build")
            safe_artifact = artifact
            safe_nodes, safe_edges = artifact.graph.nodes, artifact.graph.edges
            if (artifact.graph.account_ref != partition_ref
                    or artifact.graph.summary(projection.source_revision) != projection.graph
                    or artifact.graph.digest(check=lambda: check_cancelled(cancellation_check)) != projection.graph_digest
                    or _projection_digest(projection) != projection.projection_digest):
                raise ProjectionValidationError("compact_graph_identity_invalid")
            current_projection = self.get(creator_account_id, canonical_identity=canonical_identity)
            same_content = current_projection == projection
        else:
            safe_nodes, safe_edges = (safe_graph_records(artifact.nodes, artifact.edges)
                                      if _copy_graph else (artifact.nodes, artifact.edges))
            safe_artifact = RebuildArtifact(projection=projection, nodes=safe_nodes, edges=safe_edges)
            self._validate_artifact_shape(safe_artifact, owned_graph=_copy_graph)
            current = self.get_artifact(creator_account_id, canonical_identity=canonical_identity)
            current_projection = current.projection if current is not None else None
            same_content = current == safe_artifact
        if (current_projection is not None
                and current_projection.pipeline_revision == projection.pipeline_revision
                and current_projection.pipeline_config_digest == projection.pipeline_config_digest
                and not same_content):
            raise ProjectionRevisionConflict(
                "the same canonical and pipeline identity produced different content")
        # Artifact validation already checked this exact privately owned graph.
        graph_digest = projection.graph_digest
        pipeline_digest = pipeline_identity_digest(projection)
        if pipeline_digest != projection.pipeline_identity_digest:
            raise ProjectionValidationError("pipeline identity differs")
        if publication_epoch is None:
            publication_epoch = self.open_publication_epoch(
                self.owner_id, self._direct_publication_secret
            )
        generation_id = str(uuid4())
        now = _now()
        lease_expires = now + timedelta(seconds=self.lease_seconds)
        from app.persistence.json_header import json_validation_scope

        with self.database.transaction() as connection, json_validation_scope(
            lambda: check_cancelled(cancellation_check)
        ):
            active = connection.execute(
                """
                SELECT generation_id, canonical_revision
                FROM projection_generations
                WHERE creator_account_id=? AND status='active'
                """,
                (partition_ref,),
            ).fetchone()
            if active is not None and int(active["canonical_revision"]) > (
                projection.source_revision
            ):
                raise ProjectionRevisionConflict("projection revision moved backwards")
            expected_id = None if active is None else active["generation_id"]
            expected_revision = (
                None if active is None else int(active["canonical_revision"])
            )
            connection.execute(
                """
                INSERT INTO projection_generations (
                    generation_id, creator_account_id, status, schema_version,
                    build_version, canonical_revision, canonical_content_digest,
                    canonical_high_water_json, pipeline_revision,
                    pipeline_config_digest, pipeline_identity_digest,
                    expected_active_generation_id, expected_active_revision,
                    publication_epoch, owner_id, owner_pid,
                    owner_process_started_at, owner_instance_nonce,
                    owner_capability_digest,
                    lease_expires_at, started_at
                ) VALUES (?, ?, 'building', 3, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    partition_ref,
                    PROJECTION_BUILD_VERSION,
                    canonical_identity.revision,
                    canonical_identity.content_digest,
                    _json(
                        {
                            "view_revision": canonical_identity.revision,
                            "content_digest": canonical_identity.content_digest,
                        }
                    ),
                    projection.pipeline_revision,
                    projection.pipeline_config_digest,
                    pipeline_digest,
                    expected_id,
                    expected_revision,
                    publication_epoch,
                    self.owner_id,
                    self.build_owner.pid,
                    self.build_owner.process_started_at,
                    self.build_owner.instance_nonce,
                    self.build_owner.capability_digest,
                    _timestamp(lease_expires),
                    _timestamp(now),
                ),
            )
            connection.execute(
                """
                INSERT INTO analytics_projections (
                    generation_id, creator_account_id, source_revision,
                    pipeline_revision, pipeline_config_digest,
                    content_digest, document_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    partition_ref,
                    projection.source_revision,
                    projection.pipeline_revision,
                    projection.pipeline_config_digest,
                    projection.projection_digest,
                    projection_document(projection, check=lambda: check_cancelled(cancellation_check)),
                ),
            )
            insert_entries(connection, generation_id, cached)
            insert_fragments(connection, generation_id, fragments, conversation_fragments)
            insert_page_sets(connection, generation_id, conversation_pages, check=check)
        writer = SQLiteGraphGenerationWriter(
            self.database,
            generation_id=generation_id,
            partition_key=partition_ref,
            owner=self.build_owner,
            lease_seconds=self.lease_seconds,
        )
        with writer.lease_session():
            if compact:
                write_compact_graph(writer, artifact.graph, check=lambda: check_cancelled(cancellation_check), store=self)
            else:
                writer._replace_validated_records(safe_nodes, safe_edges)
            writer.write_stats(
                source_revision=projection.source_revision,
                node_count=len(safe_nodes),
                edge_count=len(safe_edges),
                graph_digest=graph_digest,
            )
            self._checkpoint("built", generation_id)
            check_cancelled(cancellation_check)
            writer.validate()
        self._checkpoint("validated", generation_id)
        check_cancelled(cancellation_check)
        return generation_id

    def open_publication_epoch(
        self,
        scheduler_owner_id: str,
        capability_secret: str,
        *,
        retain_fence_connection: bool = False,
    ) -> str:
        epoch = str(uuid4())
        digest = capability_digest(capability_secret)
        self.activation.register_publication_epoch(
            epoch, scheduler_owner_id, digest
        )
        try:
            if retain_fence_connection:
                self.activation.prepare_publication_epoch_fence(
                    epoch, scheduler_owner_id, digest
                )
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO projection_publication_epochs (
                        publication_epoch, scheduler_owner_id,
                        scheduler_capability_digest, state, opened_at
                    ) VALUES (?, ?, ?, 'open', ?)
                    """,
                    (
                        epoch,
                        scheduler_owner_id,
                        digest,
                        _timestamp(_now()),
                    ),
                )
        except BaseException:
            self.activation.revoke_publication_epoch(
                epoch, scheduler_owner_id, digest
            )
            raise
        return epoch

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_secret: str,
    ) -> None:
        self.fence_publication_epoch(publication_epoch)
        digest = capability_digest(capability_secret)
        # Canonical authority is fenced first. A subsequent disposable-store
        # failure cannot leave this epoch eligible for restart activation.
        self.activation.revoke_publication_epoch(
            publication_epoch, scheduler_owner_id, digest
        )
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE projection_publication_epochs
                SET state='revoked', revoked_at=?
                WHERE publication_epoch=? AND scheduler_owner_id=?
                  AND scheduler_capability_digest=? AND state='open'
                """,
                (_timestamp(_now()), publication_epoch, scheduler_owner_id, digest),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT state FROM projection_publication_epochs
                    WHERE publication_epoch=? AND scheduler_owner_id=?
                      AND scheduler_capability_digest=?
                    """,
                    (publication_epoch, scheduler_owner_id, digest),
                ).fetchone()
                if row is None or row["state"] != "revoked":
                    raise ProjectionActivationConflict("publication epoch unavailable")
        self.activation.release_publication_epoch_fence(publication_epoch)

    def fence_publication_epoch(self, publication_epoch: str) -> None:
        self._locally_fenced_epochs.add(publication_epoch)

    def publish_generation(
        self,
        generation_id: str,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        cancellation_check: CancellationCheck | None = None,
    ) -> bool:
        """Reserve, complete, then locally activate after observing the witness."""

        check_cancelled(cancellation_check)
        generation = self._generation(generation_id)
        if generation is None:
            raise KeyError("projection_generation_missing")
        if generation["status"] == "active":
            existing_intent = self.activation.get(generation_id)
            if existing_intent is not None and existing_intent.state == "reserved":
                self.activation.cancel(existing_intent.intent_id)
                self._retire(generation_id, allow_active=True)
                raise ProjectionActivationConflict(
                    "active generation lacks completed witness"
                )
            return False
        partition_ref = account_ref(creator_account_id)
        if generation["creator_account_id"] != partition_ref:
            raise ProjectionActivationConflict("projection account differs")
        self._require_live_owner(generation)
        self._require_generation_identity(generation, canonical_identity)
        # The final activation gate verifies stored content before changing visibility.
        if self.canonical_identity_reader(creator_account_id) != (
            canonical_identity
        ):
            raise ProjectionActivationConflict("canonical identity changed")
        with self.database.read() as connection:
            epoch = connection.execute(
                """
                SELECT scheduler_capability_digest
                FROM projection_publication_epochs
                WHERE publication_epoch=? AND state='open'
                """,
                (generation["publication_epoch"],),
            ).fetchone()
        if epoch is None:
            raise ProjectionActivationConflict("publication epoch revoked")
        intent = self.activation.reserve(
            creator_account_id=creator_account_id,
            account_ref=partition_ref,
            generation_id=generation_id,
            canonical_identity=canonical_identity,
            projection_digest=generation["projection_digest"],
            graph_digest=generation["graph_digest"],
            pipeline_revision=generation["pipeline_revision"],
            pipeline_config_digest=generation["pipeline_config_digest"],
            pipeline_identity_digest=generation["pipeline_identity_digest"],
            expected_previous_generation_id=generation[
                "expected_active_generation_id"
            ],
            expected_previous_revision=(
                None
                if generation["expected_active_revision"] is None
                else int(generation["expected_active_revision"])
            ),
            publication_epoch=generation["publication_epoch"],
            writer_owner=self.build_owner,
            publication_capability_digest=epoch["scheduler_capability_digest"],
        )
        self._checkpoint("canonical_intent_reserved", generation_id)
        pending_now = _now()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE projection_generations
                SET status='activation_pending', activation_intent_id=?,
                    witness_sequence=?, lease_expires_at=?
                WHERE generation_id=? AND status='validated'
                  AND owner_id=? AND owner_pid=?
                  AND owner_process_started_at=? AND owner_instance_nonce=?
                  AND owner_capability_digest=?
                """,
                (
                    intent.intent_id,
                    intent.witness_sequence,
                    _timestamp(pending_now + timedelta(seconds=self.lease_seconds)),
                    generation_id,
                    self.build_owner.owner_id,
                    self.build_owner.pid,
                    self.build_owner.process_started_at,
                    self.build_owner.instance_nonce,
                    self.build_owner.capability_digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionActivationConflict("generation ownership differs")
        self._checkpoint("intent_reserved", generation_id)
        check_cancelled(cancellation_check)
        observed_generation = self._generation(generation_id)
        if not self._intent_matches(observed_generation, intent):
            self.activation.cancel(intent.intent_id)
            self._retire(generation_id)
            raise ProjectionActivationConflict("local activation identity changed")
        completed = self.activation.complete(intent)
        if completed.state != "completed":
            raise ProjectionActivationConflict("canonical completion failed")
        self._checkpoint("canonical_completed", generation_id)
        check_cancelled(cancellation_check)
        try:
            changed = self._activate_completed_generation(
                generation_id,
                creator_account_id=creator_account_id,
                canonical_identity=canonical_identity,
            )
        except BaseException:
            try:
                self.activation.reconcile_completed(completed)
            finally:
                self._retire(generation_id, allow_active=True)
            raise
        self._checkpoint("activated", generation_id)
        self.collect_garbage(partition_ref)
        return changed

    def discard_generation(self, generation_id: str) -> None:
        generation = self._generation(generation_id)
        if generation is None:
            return
        if generation["status"] == "retired":
            return
        now = _now()
        owned = self._owner_matches(generation)
        if generation["status"] == "active" or not owned:
            raise ProjectionActivationConflict("generation discard ownership differs")
        intent = self.activation.get(generation_id)
        if intent is not None and intent.state == "reserved":
            try:
                self.activation.cancel(intent.intent_id)
            except ProjectionActivationConflict:
                pass
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE projection_generations
                SET status='retired', retired_at=?
                WHERE generation_id=?
                  AND status IN ('building','validated','activation_pending')
                  AND owner_id=? AND owner_pid=?
                  AND owner_process_started_at=? AND owner_instance_nonce=?
                  AND owner_capability_digest=?
                """,
                (
                    _timestamp(now),
                    generation_id,
                    self.build_owner.owner_id,
                    self.build_owner.pid,
                    self.build_owner.process_started_at,
                    self.build_owner.instance_nonce,
                    self.build_owner.capability_digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionActivationConflict("generation discard ownership differs")
            row = connection.execute(
                "SELECT creator_account_id FROM projection_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()
        if row is not None:
            self.collect_garbage(validated_account_ref(row[0]))

    def clear(self, creator_account_id: str) -> None:
        identity = self.canonical_identity_reader(creator_account_id)
        if identity is None:
            return
        current = self.get_artifact(
            creator_account_id,
            canonical_identity=identity,
        )
        if (
            current is None
            or current.projection.pipeline_revision == CLEAR_PIPELINE_REVISION
        ):
            return
        self.replace_artifact(
            RebuildArtifact(
                projection=empty_projection(current.projection),
                nodes=[],
                edges=[],
            ),
            creator_account_id=creator_account_id,
            canonical_identity=identity,
            force=True,
        )

    def reconcile_startup(self) -> dict[str, int]:
        """Quarantine unwitnessed active rows and recover only exact identities."""

        with self.database.read() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if len(integrity) != 1 or integrity[0][0] != "ok":
                raise ProjectionValidationError("projection_integrity_invalid")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise GraphReferentialIntegrityError("projection_foreign_key_invalid")
        counts = {"retired": 0, "activated": 0, "completed": 0, "cancelled": 0}
        now = _now()
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM projection_generations
                ORDER BY creator_account_id, canonical_revision, started_at, generation_id
                """
            ).fetchall()

        # Active rows are never trusted merely because their local status says active.
        for generation in [row for row in rows if row["status"] == "active"]:
            identity = CanonicalIdentity(
                int(generation["canonical_revision"]),
                generation["canonical_content_digest"],
            )
            intent = self.activation.get(generation["generation_id"])
            valid = (
                self._intent_matches(generation, intent, require_completed=True)
                and intent is not None
                and self.canonical_identity_reader(intent.creator_account_id)
                == identity
            )
            if valid:
                try:
                    self._validate_persisted_generation(generation["generation_id"])
                except (ProjectionValidationError, GraphReferentialIntegrityError, ValueError):
                    valid = False
            if not valid:
                if intent is not None and intent.state == "reserved":
                    try:
                        self.activation.cancel(intent.intent_id)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                elif intent is not None and intent.state == "completed":
                    try:
                        self.activation.reconcile_completed(intent)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                self._retire(generation["generation_id"], allow_active=True)
                counts["retired"] += 1

        for generation in [row for row in rows if row["status"] != "active"]:
            status = generation["status"]
            identity = CanonicalIdentity(
                int(generation["canonical_revision"]),
                generation["canonical_content_digest"],
            )
            intent = self.activation.get(generation["generation_id"])
            lease_expired = datetime.fromisoformat(generation["lease_expires_at"]) <= now
            owner_dead = process_is_definitely_dead(generation["owner_pid"])
            if status == "building":
                if lease_expired or owner_dead:
                    retired = self._retire_if_still_stale(
                        generation,
                        owner_dead=owner_dead,
                    )
                    counts["retired"] += int(retired)
                continue
            if status == "retired":
                continue
            if intent is None:
                if lease_expired or owner_dead:
                    self._retire(generation["generation_id"])
                    counts["retired"] += 1
                continue
            if not self._intent_matches(generation, intent):
                if intent.state == "reserved":
                    self.activation.cancel(intent.intent_id)
                    counts["cancelled"] += 1
                elif intent.state == "completed":
                    self.activation.reconcile_completed(intent)
                    counts["cancelled"] += 1
                self._retire(generation["generation_id"])
                counts["retired"] += 1
                continue
            if self.canonical_identity_reader(intent.creator_account_id) != identity:
                if intent.state == "reserved":
                    try:
                        self.activation.cancel(intent.intent_id)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                elif intent.state == "completed":
                    try:
                        self.activation.reconcile_completed(intent)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                self._retire(generation["generation_id"])
                counts["retired"] += 1
                continue
            with self.database.read() as connection:
                epoch_open = connection.execute(
                    """
                    SELECT 1 FROM projection_publication_epochs
                    WHERE publication_epoch=? AND state='open'
                    """,
                    (generation["publication_epoch"],),
                ).fetchone()
            canonical_epoch_open = bool(
                intent.publication_capability_digest
                and self.activation.publication_epoch_is_open(
                    generation["publication_epoch"],
                    intent.publication_capability_digest,
                )
            )
            if epoch_open is None or not canonical_epoch_open:
                if intent.state == "reserved":
                    try:
                        self.activation.cancel(intent.intent_id)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                elif intent.state == "completed":
                    try:
                        self.activation.reconcile_completed(intent)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                self._retire(generation["generation_id"])
                counts["retired"] += 1
                continue
            current_owner_live = self._owner_matches(generation) and not lease_expired
            if not current_owner_live and not lease_expired and not owner_dead:
                # A second store in the same live process cannot take over by
                # copying the persisted owner fields. The original capability
                # or a positively reclaimable lease remains authoritative.
                continue
            try:
                self._validate_persisted_generation(generation["generation_id"])
                if status == "validated":
                    self._attach_intent(generation["generation_id"], intent)
                if intent.state == "reserved":
                    intent = self.activation.complete(intent)
                    counts["completed"] += 1
                if intent.state != "completed":
                    raise ProjectionReconciliationError("activation witness is terminal")
                if self._activate_completed_generation(
                    generation["generation_id"],
                    creator_account_id=intent.creator_account_id,
                    canonical_identity=identity,
                    require_current_owner=current_owner_live,
                ):
                    counts["activated"] += 1
            except (
                ProjectionActivationConflict,
                ProjectionReconciliationError,
                ProjectionValidationError,
                GraphReferentialIntegrityError,
                ValueError,
            ):
                if intent is not None and intent.state == "reserved":
                    try:
                        self.activation.cancel(intent.intent_id)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                elif intent is not None and intent.state == "completed":
                    try:
                        self.activation.reconcile_completed(intent)
                        counts["cancelled"] += 1
                    except ProjectionActivationConflict:
                        pass
                self._retire(generation["generation_id"])
                counts["retired"] += 1

        known = {row["generation_id"] for row in rows}
        for intent in self.activation.pending():
            if intent.generation_id not in known:
                self.activation.cancel(intent.intent_id)
                counts["cancelled"] += 1
        accounts = sorted({row["creator_account_id"] for row in rows})
        for account_id in accounts:
            self.collect_garbage(validated_account_ref(account_id))
        return counts

    def collect_garbage(self, partition_ref: AccountPartitionRef | str) -> int:
        """Delete one bounded retired-generation batch for a validated partition."""

        partition_ref = validated_account_ref(partition_ref)

        with self.database.transaction() as connection:
            from app.analytics.database import GENERATION_WRITE_CACHE_KIB
            connection.execute(f"PRAGMA cache_size=-{GENERATION_WRITE_CACHE_KIB}")
            rows = connection.execute(
                """
                SELECT generation_id FROM projection_generations
                WHERE creator_account_id=? AND status='retired'
                ORDER BY COALESCE(retired_at, started_at) DESC, generation_id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    partition_ref,
                    self.gc_batch_size,
                    self.rollback_retention,
                ),
            ).fetchall()
            from app.analytics.shared_graph import supported
            graph_tables = ("graph_owned_edges", "graph_owned_nodes") if supported(connection) else ("graph_edges", "graph_nodes")
            for row in rows:
                # Remove outgoing references before their endpoints in this transaction.
                for table in graph_tables:
                    connection.execute(
                        f"DELETE FROM {table} WHERE generation_id=? AND creator_account_id=?",
                        (row[0], partition_ref),
                    )
                connection.execute(
                    "DELETE FROM projection_generations WHERE generation_id=? AND status='retired'",
                    (row[0],),
                )
            return len(rows)

    def _validate_and_mark_generation(self, generation_id: str) -> None:
        self._validate_persisted_generation(generation_id, allow_building=True)
        with self.database.transaction() as connection:
            values = recompute_generation(connection, generation_id)
            updated = connection.execute(
                """
                UPDATE projection_generations
                SET status='validated', projection_digest=?, graph_digest=?,
                    node_count=?, edge_count=?, validated_at=?, lease_expires_at=?
                WHERE generation_id=? AND status='building' AND owner_id=?
                  AND owner_pid=? AND owner_process_started_at=?
                  AND owner_instance_nonce=? AND owner_capability_digest=?
                """,
                (
                    values["projection_digest"],
                    values["graph_digest"],
                    values["node_count"],
                    values["edge_count"],
                    _timestamp(_now()),
                    _timestamp(_now() + timedelta(seconds=self.lease_seconds)),
                    generation_id,
                    self.owner_id,
                    self.build_owner.pid,
                    self.build_owner.process_started_at,
                    self.build_owner.instance_nonce,
                    self.build_owner.capability_digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionValidationError("generation build ownership was lost")

    def _validate_persisted_generation(
        self,
        generation_id: str,
        *,
        allow_building: bool = False,
        materialize_graph: bool = False,
        deadline: float | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> dict[str, object]:
        def check() -> None:
            _check_operation_budget(deadline, cancellation_check)

        try:
            check()
            with self.database.read() as connection:
                connection.execute("BEGIN")
                connection.set_progress_handler(
                    lambda: int(
                        (deadline is not None and time.monotonic() > deadline)
                        or (
                            cancellation_check is not None
                            and cancellation_check()
                        )
                    ),
                    1_000,
                )
                generation = connection.execute(
                    "SELECT * FROM projection_generations WHERE generation_id=?",
                    (generation_id,),
                ).fetchone()
                if generation is None:
                    raise KeyError("projection_generation_missing")
                if generation["status"] == "building" and not allow_building:
                    raise ProjectionValidationError("projection_generation_unvalidated")
                values = recompute_generation(
                    connection,
                    generation_id,
                    check=check, materialize_graph=materialize_graph,
                )
                projection = values["projection"]
                if (
                    projection.account_ref != generation["creator_account_id"]
                    or projection.source_revision != int(generation["canonical_revision"])
                    or projection.pipeline_revision != generation["pipeline_revision"]
                    or projection.pipeline_config_digest
                    != generation["pipeline_config_digest"]
                    or projection.canonical_content_digest
                    != generation["canonical_content_digest"]
                    or projection.graph_digest != values["graph_digest"]
                    or pipeline_identity_digest(projection)
                    != generation["pipeline_identity_digest"]
                    or projection.pipeline_identity_digest
                    != generation["pipeline_identity_digest"]
                ):
                    raise ProjectionValidationError("projection_identity_invalid")
                if generation["status"] != "building" and (
                    values["projection_digest"] != generation["projection_digest"]
                    or values["graph_digest"] != generation["graph_digest"]
                    or values["node_count"] != int(generation["node_count"])
                    or values["edge_count"] != int(generation["edge_count"])
                ):
                    raise ProjectionValidationError("projection_digest_invalid")
                check()
                return values
        except GraphDeadlineExceeded:
            raise
        except (ProjectionValidationError, GraphReferentialIntegrityError):
            raise
        except sqlite3.OperationalError as error:
            if "interrupted" in str(error).lower():
                check()
                raise GraphDeadlineExceeded("graph_deadline_exceeded") from None
            raise ProjectionValidationError("projection_generation_invalid") from None
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError) as error:
            raise ProjectionValidationError("projection_generation_invalid") from None

    def _activate_completed_generation(
        self,
        generation_id: str,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        require_current_owner: bool = True,
    ) -> bool:
        generation = self._generation(generation_id)
        if generation is None:
            raise KeyError("projection_generation_missing")
        if require_current_owner:
            self._require_live_owner(generation)
        intent = self.activation.get(generation_id)
        if not self._intent_matches(generation, intent, require_completed=True):
            raise ProjectionReconciliationError("completed witness does not match")
        if (
            intent is None
            or intent.publication_capability_digest is None
            or not self.activation.publication_epoch_is_open(
                intent.publication_epoch,
                intent.publication_capability_digest,
            )
        ):
            raise ProjectionActivationConflict("publication epoch revoked")
        if self.canonical_identity_reader(creator_account_id) != (
            canonical_identity
        ):
            raise ProjectionActivationConflict("canonical identity changed")
        self._validate_persisted_generation(generation_id)
        now = _timestamp(_now())
        with self.database.transaction() as connection:
            candidate = connection.execute(
                """
                SELECT * FROM projection_generations
                WHERE generation_id=? AND creator_account_id=?
                  AND status='activation_pending'
                """,
                (generation_id, generation["creator_account_id"]),
            ).fetchone()
            if not self._intent_matches(candidate, intent, require_completed=True):
                raise ProjectionReconciliationError("activation witness CAS differs")
            current = connection.execute(
                """
                SELECT generation_id, canonical_revision
                FROM projection_generations
                WHERE creator_account_id=? AND status='active'
                """,
                (generation["creator_account_id"],),
            ).fetchone()
            if current is not None and current["generation_id"] == generation_id:
                return False
            current_id = None if current is None else current["generation_id"]
            current_revision = (
                None if current is None else int(current["canonical_revision"])
            )
            expected_id = candidate["expected_active_generation_id"]
            expected_revision = candidate["expected_active_revision"]
            expected_revision = (
                None if expected_revision is None else int(expected_revision)
            )
            # This in-transaction CAS prevents a delayed revision from rolling
            # back a newer generation activated after the build snapshot.
            if current_id != expected_id or current_revision != expected_revision:
                raise ProjectionActivationConflict("active generation changed")
            if current_revision is not None and current_revision > (
                int(generation["canonical_revision"])
            ):
                raise ProjectionActivationConflict("projection revision is stale")
            epoch = connection.execute(
                """
                SELECT scheduler_capability_digest FROM projection_publication_epochs
                WHERE publication_epoch=? AND state='open'
                """,
                (candidate["publication_epoch"],),
            ).fetchone()
            if epoch is None:
                raise ProjectionActivationConflict("publication epoch revoked")
            if (
                intent is None
                or intent.publication_capability_digest
                != epoch["scheduler_capability_digest"]
            ):
                raise ProjectionReconciliationError("activation capability differs")
            if candidate["publication_epoch"] in self._locally_fenced_epochs:
                raise ProjectionActivationConflict("publication epoch revoked")
            if (
                intent is None
                or intent.expected_previous_generation_id != expected_id
                or intent.expected_previous_revision != expected_revision
                or intent.publication_epoch != candidate["publication_epoch"]
                or intent.creator_account_id != creator_account_id
            ):
                raise ProjectionReconciliationError("activation witness CAS differs")
            if candidate["publication_epoch"] in self._locally_fenced_epochs:
                raise ProjectionActivationConflict("publication epoch revoked")
            connection.execute(
                """
                UPDATE projection_generations
                SET status='retired', retired_at=?
                WHERE creator_account_id=? AND status='active'
                """,
                (now, generation["creator_account_id"]),
            )
            owner_clause = """
                  AND owner_id=? AND owner_pid=?
                  AND owner_process_started_at=? AND owner_instance_nonce=?
                  AND owner_capability_digest=?
            """ if require_current_owner else ""
            owner_parameters: tuple[object, ...] = (
                (
                    self.build_owner.owner_id,
                    self.build_owner.pid,
                    self.build_owner.process_started_at,
                    self.build_owner.instance_nonce,
                    self.build_owner.capability_digest,
                )
                if require_current_owner
                else ()
            )
            updated = connection.execute(
                f"""
                UPDATE projection_generations
                SET status='active', activated_at=?
                WHERE generation_id=? AND status='activation_pending'
                  AND activation_intent_id=? AND witness_sequence=?
                  {owner_clause}
                """,
                (
                    now,
                    generation_id,
                    intent.intent_id if intent is not None else "",
                    intent.witness_sequence if intent is not None else -1,
                    *owner_parameters,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionReconciliationError("local activation CAS failed")
        return True

    def _attach_intent(
        self, generation_id: str, intent: ProjectionActivationIntent
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE projection_generations
                SET status='activation_pending', activation_intent_id=?,
                    witness_sequence=?
                WHERE generation_id=? AND status='validated'
                """,
                (intent.intent_id, intent.witness_sequence, generation_id),
            )

    def _matching_active_generation(
        self,
        creator_account_id: str,
        partition_ref: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
        deadline: float | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> str | None:
        _check_operation_budget(deadline, cancellation_check)
        expected = canonical_identity or self.canonical_identity_reader(
            creator_account_id
        )
        if expected is None:
            return None
        # Re-read authoritative identity even when the caller supplied its
        # snapshot; both must still agree at the visibility check.
        if self.canonical_identity_reader(creator_account_id) != expected:
            return None
        _check_operation_budget(deadline, cancellation_check)
        generation = self._active_generation_row(partition_ref)
        if generation is None:
            return None
        if (
            int(generation["canonical_revision"]) != expected.revision
            or generation["canonical_content_digest"] != expected.content_digest
        ):
            return None
        intent = self.activation.get(generation["generation_id"])
        _check_operation_budget(deadline, cancellation_check)
        if not self._intent_matches(generation, intent, require_completed=True):
            return None
        if intent is None or intent.creator_account_id != creator_account_id:
            return None
        return generation["generation_id"]

    def _active_generation_for_graph(
        self,
        account_partition_ref: str,
        *,
        deadline: float | None = None,
        cancellation_check: CancellationCheck | None = None,
        validate_rows: bool = False,
    ) -> str | None:
        generation = self._active_generation_row(account_partition_ref)
        if generation is None:
            return None
        intent = self.activation.get(generation["generation_id"])
        if (
            intent is None
            or intent.account_ref != account_partition_ref
            or account_ref(intent.creator_account_id) != account_partition_ref
        ):
            return None
        generation_id = self._matching_active_generation(
            intent.creator_account_id,
            account_partition_ref,
            deadline=deadline,
            cancellation_check=cancellation_check,
        )
        if generation_id is not None and validate_rows:
            self._validate_persisted_generation(
                generation_id,
                deadline=deadline,
                cancellation_check=cancellation_check,
            )
        return generation_id

    def _artifact_matches_active(
        self,
        creator_account_id: str,
        artifact: RebuildArtifact,
        identity: CanonicalIdentity,
    ) -> bool:
        current = self.get_artifact(
            creator_account_id,
            canonical_identity=identity,
        )
        if current is None:
            return False
        safe_nodes, safe_edges = safe_graph_records(artifact.nodes, artifact.edges)
        return (
            current.projection == artifact.projection
            and current.nodes == safe_nodes
            and current.edges == safe_edges
        )

    def _require_generation_identity(
        self, generation: sqlite3.Row, identity: CanonicalIdentity
    ) -> None:
        if (
            int(generation["canonical_revision"]) != identity.revision
            or generation["canonical_content_digest"] != identity.content_digest
        ):
            raise ProjectionActivationConflict("generation canonical identity differs")

    @staticmethod
    def _intent_matches(
        generation: sqlite3.Row | None,
        intent: ProjectionActivationIntent | None,
        *,
        require_completed: bool = False,
    ) -> bool:
        if generation is None or intent is None:
            return False
        if require_completed and intent.state != "completed":
            return False
        local_intent = generation["activation_intent_id"]
        local_sequence = generation["witness_sequence"]
        if require_completed and (
            local_intent != intent.intent_id
            or local_sequence is None
            or int(local_sequence) != intent.witness_sequence
        ):
            return False
        return bool(
            (local_intent is None or local_intent == intent.intent_id)
            and (local_sequence is None or int(local_sequence) == intent.witness_sequence)
            and intent.account_ref == generation["creator_account_id"]
            and intent.generation_id == generation["generation_id"]
            and intent.canonical_revision == int(generation["canonical_revision"])
            and intent.canonical_content_digest
            == generation["canonical_content_digest"]
            and intent.projection_digest == generation["projection_digest"]
            and intent.graph_digest == generation["graph_digest"]
            and intent.pipeline_revision == generation["pipeline_revision"]
            and intent.pipeline_config_digest
            == generation["pipeline_config_digest"]
            and intent.pipeline_identity_digest
            == generation["pipeline_identity_digest"]
            and intent.expected_previous_generation_id
            == generation["expected_active_generation_id"]
            and intent.expected_previous_revision
            == (
                None
                if generation["expected_active_revision"] is None
                else int(generation["expected_active_revision"])
            )
            and intent.publication_epoch == generation["publication_epoch"]
            and intent.writer_owner_id == generation["owner_id"]
            and intent.writer_owner_pid == int(generation["owner_pid"])
            and intent.writer_process_started_at
            == generation["owner_process_started_at"]
            and intent.writer_instance_nonce == generation["owner_instance_nonce"]
            and intent.writer_capability_digest
            == generation["owner_capability_digest"]
        )

    def _active_generation_row(self, creator_account_id: str) -> sqlite3.Row | None:
        with self.database.read() as connection:
            return connection.execute(
                """
                SELECT * FROM projection_generations
                WHERE creator_account_id=? AND status='active'
                """,
                (creator_account_id,),
            ).fetchone()

    def _generation(self, generation_id: str) -> sqlite3.Row | None:
        with self.database.read() as connection:
            return connection.execute(
                "SELECT * FROM projection_generations WHERE generation_id=?",
                (generation_id,),
            ).fetchone()

    def _owner_matches(self, generation: sqlite3.Row) -> bool:
        return (
            generation["owner_id"] == self.build_owner.owner_id
            and int(generation["owner_pid"]) == self.build_owner.pid
            and generation["owner_process_started_at"]
            == self.build_owner.process_started_at
            and generation["owner_instance_nonce"]
            == self.build_owner.instance_nonce
            and generation["owner_capability_digest"]
            == self.build_owner.capability_digest
        )

    def _require_live_owner(self, generation: sqlite3.Row) -> None:
        if not self._owner_matches(generation):
            raise ProjectionActivationConflict("generation ownership differs")

    def _retire_if_still_stale(
        self,
        generation: sqlite3.Row,
        *,
        owner_dead: bool,
    ) -> bool:
        with self.database.transaction() as connection:
            now = _now()
            clauses = [
                "generation_id=?",
                "status='building'",
                "owner_id=?",
                "owner_pid=?",
                "owner_process_started_at=?",
                "owner_instance_nonce=?",
                "owner_capability_digest=?",
            ]
            parameters: list[object] = [
                _timestamp(now),
                generation["generation_id"],
                generation["owner_id"],
                generation["owner_pid"],
                generation["owner_process_started_at"],
                generation["owner_instance_nonce"],
                generation["owner_capability_digest"],
            ]
            if owner_dead:
                clauses.append("lease_expires_at=?")
                parameters.append(generation["lease_expires_at"])
            else:
                clauses.append("lease_expires_at<=?")
                parameters.append(_timestamp(now))
            updated = connection.execute(
                f"""
                UPDATE projection_generations
                SET status='retired', retired_at=?
                WHERE {' AND '.join(clauses)}
                """,
                parameters,
            )
            return updated.rowcount == 1

    def _retire(self, generation_id: str, *, allow_active: bool = False) -> None:
        with self.database.transaction() as connection:
            statuses = "('building','validated','activation_pending','active')" if allow_active else "('building','validated','activation_pending')"
            connection.execute(
                f"""
                UPDATE projection_generations
                SET status='retired', retired_at=?
                WHERE generation_id=? AND status IN {statuses}
                """,
                (_timestamp(_now()), generation_id),
            )

    @staticmethod
    def _validate_artifact_shape(artifact: RebuildArtifact, *, owned_graph: bool = False) -> None:
        projection = artifact.projection
        if projection.graph.source_revision != projection.source_revision:
            raise ProjectionValidationError("graph and projection revisions differ")
        if projection.graph.node_count != len(artifact.nodes):
            raise ProjectionValidationError("graph node coverage differs")
        if projection.graph.edge_count != len(artifact.edges):
            raise ProjectionValidationError("graph edge coverage differs")
        node_counts = Counter(item.kind.value for item in artifact.nodes)
        edge_counts = Counter(item.relation.value for item in artifact.edges)
        if projection.graph.node_counts_by_kind != dict(sorted(node_counts.items())):
            raise ProjectionValidationError("graph node-kind coverage differs")
        if projection.graph.edge_counts_by_relation != dict(sorted(edge_counts.items())):
            raise ProjectionValidationError("graph relation coverage differs")
        node_ids = {item.node_id for item in artifact.nodes}
        if len(node_ids) != len(artifact.nodes):
            raise ProjectionValidationError("graph node identities are duplicated")
        if any(
            item.partition_key != projection.account_ref
            for item in artifact.nodes
        ) or any(
            item.partition_key != projection.account_ref
            for item in artifact.edges
        ):
            raise ProjectionValidationError("graph account scope differs")
        if any(
            item.source_id not in node_ids or item.target_id not in node_ids
            for item in artifact.edges
        ):
            raise GraphReferentialIntegrityError("graph endpoint is absent")
        if _projection_digest(projection) != projection.projection_digest:
            raise ProjectionValidationError("projection content digest differs")
        from app.analytics.graph_privacy import _validated_graph_digest

        digest_graph = _validated_graph_digest if owned_graph else _graph_digest
        if projection.graph_digest != digest_graph(artifact.nodes, artifact.edges):
            raise ProjectionValidationError("graph projection digest differs")

    def _checkpoint(self, stage: str, generation_id: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(stage, generation_id)


def _validate_generation_links(connection, generation_id, account_id, check):
    """Check the candidate's referential closure without scanning other accounts."""

    check()
    epoch = connection.execute(
        """SELECT 1 FROM projection_generations g
           JOIN projection_publication_epochs e ON e.publication_epoch=g.publication_epoch
           WHERE g.generation_id=? AND g.creator_account_id=?""",
        (generation_id, account_id),
    ).fetchone()
    if epoch is None:
        raise GraphReferentialIntegrityError("projection_epoch_absent")
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    optional_since = {"enrichment_reuse": 5, "conversation_fragments": 6, "projection_query_metadata": 7, "generation_graph_segments": 10, "conversation_page_sets": 11, "conversation_pages": 11}
    if schema_version >= 10:
        from app.analytics.shared_graph import verify_segment_links
        verify_segment_links(connection, generation_id, account_id)
    else:
        missing = connection.execute("""SELECT 1 FROM graph_edges AS e
            LEFT JOIN graph_nodes AS source ON source.generation_id=e.generation_id
                AND source.creator_account_id=e.creator_account_id AND source.node_id=e.source_id
            LEFT JOIN graph_nodes AS target ON target.generation_id=e.generation_id
                AND target.creator_account_id=e.creator_account_id AND target.node_id=e.target_id
            WHERE e.generation_id=? AND e.creator_account_id=?
              AND (source.node_id IS NULL OR target.node_id IS NULL) LIMIT 1""",
            (generation_id, account_id)).fetchone()
        check()
        if missing is not None:
            raise GraphReferentialIntegrityError("graph_endpoint_absent")
    for table in ("analytics_projections", "graph_nodes", "graph_edges",
                  "graph_partition_stats", "enrichment_reuse", "conversation_fragments", "conversation_page_sets", "conversation_pages",
                  "projection_query_metadata", "graph_algorithm_metrics", "generation_graph_segments"):
        check()
        if schema_version < optional_since.get(table, 0):
            continue
        for comparison in ("<", ">"):
            foreign = connection.execute(
                f"SELECT 1 FROM {table} WHERE generation_id=? AND creator_account_id{comparison}? LIMIT 1",
                (generation_id, account_id),
            ).fetchone()
            if foreign is not None:
                raise GraphReferentialIntegrityError("projection_account_mismatch")


def recompute_generation(
    connection: sqlite3.Connection,
    generation_id: str,
    *,
    check: Callable[[], None] | None = None,
    materialize_graph: bool = False,
) -> dict[str, object]:
    """Verify stored data with a connection-local page-cache target."""

    with generation_verification_cache(connection):
        return _recompute_generation(connection, generation_id, check=check,
                                     materialize_graph=materialize_graph)


def _recompute_generation(
    connection: sqlite3.Connection,
    generation_id: str,
    *,
    check: Callable[[], None] | None = None,
    materialize_graph: bool = False,
) -> dict[str, object]:
    """Recompute all row-derived validation values; stored digest fields are ignored."""

    run_check = check or (lambda: None)
    run_check()
    generation = connection.execute(
        "SELECT * FROM projection_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if generation is None:
        raise KeyError("projection_generation_missing")
    run_check()
    account_id = generation["creator_account_id"]
    projection_row = connection.execute(
        """
        SELECT * FROM analytics_projections
        WHERE generation_id=? AND creator_account_id=?
        """,
        (generation_id, account_id),
    ).fetchone()
    if projection_row is None:
        raise ProjectionValidationError("projection document is missing")
    projection = AnalyticsProjection.model_validate_json(
        projection_row["document_json"]
    )
    run_check()
    projection_digest = _projection_digest(projection)
    run_check()
    if (
        projection_digest != projection.projection_digest
        or projection_digest != projection_row["content_digest"]
        or projection.pipeline_revision != projection_row["pipeline_revision"]
        or projection.pipeline_config_digest
        != projection_row["pipeline_config_digest"]
        or projection.canonical_content_digest
        != generation["canonical_content_digest"]
        or projection.account_ref != generation["creator_account_id"]
        or projection.graph.account_ref != generation["creator_account_id"]
        or projection.pipeline_identity_digest
        != generation["pipeline_identity_digest"]
    ):
        raise ProjectionValidationError("projection row digest differs")
    _validate_generation_links(connection, generation_id, account_id, run_check)
    if materialize_graph:
        from app.analytics.graph_privacy import _validated_graph_digest

        nodes, edges = _generation_graph(connection, generation_id, account_id, check=run_check)
        graph_digest = _validated_graph_digest(nodes, edges, check=run_check)
        node_counts = Counter(item.kind.value for item in nodes)
        edge_counts = Counter(item.relation.value for item in edges)
    else:
        from app.analytics.graph_verification import verify_graph_rows

        verified = verify_graph_rows(connection, generation_id, account_id, check=run_check)
        nodes, edges = verified.nodes, verified.edges
        graph_digest = verified.digest
        node_counts, edge_counts = verified.node_counts, verified.edge_counts
    node_count, edge_count = sum(node_counts.values()), sum(edge_counts.values())
    run_check()
    if projection.graph_digest != graph_digest:
        raise ProjectionValidationError("graph document digest differs")
    stats = connection.execute(
        """
        SELECT * FROM graph_partition_stats
        WHERE generation_id=? AND creator_account_id=?
        """,
        (generation_id, account_id),
    ).fetchone()
    if (
        stats is None
        or int(stats["source_revision"]) != projection.source_revision
        or int(stats["node_count"]) != node_count
        or int(stats["edge_count"]) != edge_count
        or stats["graph_digest"] != graph_digest
        or projection.graph.node_count != node_count
        or projection.graph.edge_count != edge_count
    ):
        raise ProjectionValidationError("graph row coverage or digest differs")
    run_check()
    run_check()
    if projection.graph.node_counts_by_kind != dict(sorted(node_counts.items())):
        raise ProjectionValidationError("node-kind coverage differs")
    if projection.graph.edge_counts_by_relation != dict(sorted(edge_counts.items())):
        raise ProjectionValidationError("edge-kind coverage differs")
    return {
        "projection": projection,
        "nodes": nodes,
        "edges": edges,
        "projection_digest": projection_digest,
        "graph_digest": graph_digest,
        "node_count": node_count,
        "edge_count": edge_count,
    }


def _generation_graph(
    connection: sqlite3.Connection,
    generation_id: str,
    account_id: str,
    *,
    check: Callable[[], None] | None = None,
) -> tuple[list, list]:
    run_check = check or (lambda: None)
    nodes = []
    for row in connection.execute(
        """
        SELECT * FROM graph_nodes
        WHERE generation_id=? AND creator_account_id=? ORDER BY node_id
        """,
        (generation_id, account_id),
    ):
        run_check()
        nodes.append(_node(row))
    edges = []
    for row in connection.execute(
        """
        SELECT * FROM graph_edges
        WHERE generation_id=? AND creator_account_id=? ORDER BY edge_id
        """,
        (generation_id, account_id),
    ):
        run_check()
        edges.append(_edge(row))
    return nodes, edges


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection_time_timezone_required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_operation_budget(
    deadline: float | None,
    cancellation_check: CancellationCheck | None,
) -> None:
    check_cancelled(cancellation_check)
    if deadline is not None and time.monotonic() > deadline:
        raise GraphDeadlineExceeded("graph_deadline_exceeded")


def _projection_digest(projection: AnalyticsProjection) -> str:
    return projection_content_digest(projection)
