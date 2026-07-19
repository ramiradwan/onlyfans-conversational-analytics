"""Revision-aware storage ports for rebuildable analytics projections."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Literal, Protocol, runtime_checkable
from uuid import uuid4

from app.analytics.cancellation import CancellationCheck
from app.analytics.graph_privacy import graph_content_digest, safe_graph_records
from app.analytics.graph_store import GraphReader, InMemoryGraphRepository
from app.analytics.identity import CanonicalIdentity, pipeline_identity_digest
from app.analytics.opaque_refs import account_ref
from app.analytics.ownership import BuildOwner, capability_digest, current_build_owner
from app.models.analytics import (
    AnalyticsProjection,
    AnalyticsWindow,
    RebuildArtifact,
    WindowScope,
)
from app.persistence.projection_activation import (
    InMemoryProjectionActivationRepository,
    ProjectionActivationConflict,
    ProjectionActivationIntent,
    ProjectionActivationRepository,
)


CanonicalIdentityReader = Callable[[str], CanonicalIdentity | None]
CLEAR_PIPELINE_REVISION = "analytics.clear.v1"
CLEAR_PIPELINE_CONFIG_DIGEST = "sha256:" + hashlib.sha256(
    b"ofca:analytics-clear:v1"
).hexdigest()


class ProjectionRevisionConflict(RuntimeError):
    """Raised when projection revision monotonicity or determinism is violated."""


@runtime_checkable
class AnalyticsProjectionStore(Protocol):
    def get(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> AnalyticsProjection | None: ...

    def replace(
        self,
        projection: AnalyticsProjection,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> bool: ...

    def clear(self, creator_account_id: str) -> None: ...


@runtime_checkable
class AtomicAnalyticsProjectionStore(AnalyticsProjectionStore, Protocol):
    """Projection store that activates analytics and graph as one generation."""

    @property
    def graph(self) -> GraphReader: ...

    def get_artifact(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> RebuildArtifact | None: ...

    def replace_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
        force: bool = False,
        cancellation_check: CancellationCheck | None = None,
    ) -> bool: ...

    def stage_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        publication_epoch: str | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> str: ...

    def publish_generation(
        self,
        generation_id: str,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        cancellation_check: CancellationCheck | None = None,
    ) -> bool: ...

    def discard_generation(self, generation_id: str) -> None: ...

    def next_projection_generation(self, creator_account_id: str) -> int: ...

    def open_publication_epoch(
        self,
        scheduler_owner_id: str,
        capability_secret: str,
        *,
        retain_fence_connection: bool = False,
    ) -> str: ...

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_secret: str,
    ) -> None: ...

    def fence_publication_epoch(self, publication_epoch: str) -> None: ...


MemoryProjectionStatus = Literal["validated", "active", "retired"]


@dataclass(slots=True)
class _MemoryProjectionGeneration:
    generation_id: str
    artifact: RebuildArtifact
    canonical_identity: CanonicalIdentity
    publication_epoch: str
    expected_active_generation_id: str | None
    expected_active_revision: int | None
    owner_token: str
    creator_account_id: str
    writer_owner: BuildOwner
    publication_capability_digest: str
    ordinal: int
    status: MemoryProjectionStatus = "validated"
    intent: ProjectionActivationIntent | None = None

    @property
    def account_ref(self) -> str:
        return self.artifact.projection.account_ref


class InMemoryAnalyticsProjectionStore:
    """Atomic witnessed generations sharing one lock with the memory graph."""

    def __init__(
        self,
        *,
        graph_repository: InMemoryGraphRepository | None = None,
        activation: ProjectionActivationRepository | None = None,
        canonical_identity_reader: CanonicalIdentityReader | None = None,
        rollback_retention: int = 1,
        gc_batch_size: int = 8,
        graph_lease_seconds: float = 120.0,
    ) -> None:
        if rollback_retention < 0 or gc_batch_size <= 0 or graph_lease_seconds <= 0:
            raise ValueError("projection_retention_invalid")
        self._repository = graph_repository or InMemoryGraphRepository(
            rollback_retention=rollback_retention,
            gc_batch_size=gc_batch_size,
        )
        self._lock: RLock = self._repository._lock
        self.graph = self._repository.reader
        self._canonical_identity_reader = canonical_identity_reader
        self._direct_authority: dict[str, CanonicalIdentity] = {}
        self._activation = activation or InMemoryProjectionActivationRepository(
            self._observed_identity
        )
        self._generations: dict[
            tuple[str, str], _MemoryProjectionGeneration
        ] = {}
        self._active: dict[str, str] = {}
        self._epochs: dict[
            str, tuple[str, str, Literal["open", "revoked"]]
        ] = {}
        self._locally_fenced_epochs: set[str] = set()
        self._build_owner = current_build_owner()
        self._direct_publication_secret = secrets.token_hex(32)
        self._rollback_retention = rollback_retention
        self._gc_batch_size = gc_batch_size
        self._graph_lease_seconds = graph_lease_seconds
        self._ordinal = 0
        self._repository.set_active_visibility(self._graph_generation_is_visible)

    def get(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> AnalyticsProjection | None:
        with self._lock:
            generation = self._visible_generation_locked(
                creator_account_id, canonical_identity
            )
            return (
                None
                if generation is None
                else deepcopy(generation.artifact.projection)
            )

    def get_artifact(
        self,
        creator_account_id: str,
        *,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> RebuildArtifact | None:
        with self._lock:
            generation = self._visible_generation_locked(
                creator_account_id, canonical_identity
            )
            return None if generation is None else deepcopy(generation.artifact)

    def replace(
        self,
        projection: AnalyticsProjection,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
    ) -> bool:
        if projection.graph.node_count or projection.graph.edge_count:
            raise ValueError("projection_graph_requires_artifact")
        if projection.account_ref != account_ref(creator_account_id):
            raise ProjectionActivationConflict("projection_account_ref_invalid")
        identity = canonical_identity or self._observed_identity(creator_account_id)
        if identity is None:
            raise ProjectionActivationConflict("canonical_account_unavailable")
        return self.replace_artifact(
            RebuildArtifact(projection=projection, nodes=[], edges=[]),
            creator_account_id=creator_account_id,
            canonical_identity=identity,
        )

    def replace_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity | None = None,
        force: bool = False,
        cancellation_check=None,
    ) -> bool:
        del cancellation_check
        if artifact.projection.account_ref != account_ref(creator_account_id):
            raise ProjectionActivationConflict("projection_account_ref_invalid")
        identity = canonical_identity or self._observed_identity(creator_account_id)
        if identity is None:
            raise ProjectionActivationConflict("canonical_account_unavailable")
        if not force and self.get_artifact(
            creator_account_id,
            canonical_identity=identity,
        ) == artifact:
            return False
        generation_id = self.stage_artifact(
            artifact,
            creator_account_id=creator_account_id,
            canonical_identity=identity,
        )
        return self.publish_generation(
            generation_id,
            creator_account_id=creator_account_id,
            canonical_identity=identity,
        )

    def stage_artifact(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        publication_epoch: str | None = None,
        cancellation_check=None,
    ) -> str:
        del cancellation_check
        safe_artifact = self._validated_artifact(artifact, canonical_identity)
        account_id = creator_account_id
        partition_ref = safe_artifact.projection.account_ref
        if partition_ref != account_ref(account_id):
            raise ProjectionActivationConflict("projection_account_ref_invalid")
        with self._lock:
            self._require_authority_locked(account_id, canonical_identity)
            current = self._active_generation_locked(account_id)
            self._validate_replacement_locked(safe_artifact.projection, current)
            if publication_epoch is None:
                publication_epoch = self.open_publication_epoch(
                    "direct-memory", self._direct_publication_secret
                )
            epoch = self._epochs.get(publication_epoch)
            if epoch is None or epoch[2] != "open":
                raise ProjectionActivationConflict("publication_epoch_unavailable")
            expected_active = self._repository._active.get(partition_ref)
            expected_revision = (
                None
                if expected_active is None
                else self._repository._required(expected_active).source_revision
            )
            owner_token = str(uuid4())
            writer = self._repository.begin_generation(
                partition_ref,
                source_revision=canonical_identity.revision,
                owner_token=owner_token,
                lease_seconds=self._graph_lease_seconds,
            )
            try:
                writer.replace(nodes=safe_artifact.nodes, edges=safe_artifact.edges)
                observed_graph_digest = writer.validate()
                if observed_graph_digest != safe_artifact.projection.graph_digest:
                    raise ProjectionRevisionConflict("projection_graph_digest_invalid")
            except BaseException:
                try:
                    self._repository.discard_generation(
                        writer.generation_id, owner_token=owner_token
                    )
                except Exception:
                    pass
                raise
            self._ordinal += 1
            generation = _MemoryProjectionGeneration(
                generation_id=writer.generation_id,
                artifact=deepcopy(safe_artifact),
                canonical_identity=canonical_identity,
                publication_epoch=publication_epoch,
                expected_active_generation_id=expected_active,
                expected_active_revision=expected_revision,
                owner_token=owner_token,
                creator_account_id=account_id,
                writer_owner=self._build_owner,
                publication_capability_digest=epoch[1],
                ordinal=self._ordinal,
            )
            self._generations[(account_id, writer.generation_id)] = generation
            return writer.generation_id

    def publish_generation(
        self,
        generation_id: str,
        *,
        creator_account_id: str,
        canonical_identity: CanonicalIdentity,
        cancellation_check=None,
    ) -> bool:
        del cancellation_check
        with self._lock:
            generation = self._generation_by_id_locked(generation_id)
            if generation.creator_account_id != creator_account_id:
                raise ProjectionActivationConflict("projection_account_mismatch")
            if generation.status == "active":
                return False
            if generation.status != "validated":
                raise ProjectionActivationConflict("projection_generation_unavailable")
            self._repository.validate_generation_owner(
                generation.generation_id,
                owner_token=generation.owner_token,
                expected_status="validated",
            )
            if generation.canonical_identity != canonical_identity:
                raise ProjectionActivationConflict("canonical_identity_changed")
            self._require_authority_locked(
                generation.creator_account_id, canonical_identity
            )
            projection = generation.artifact.projection
            intent = self._activation.reserve(
                creator_account_id=generation.creator_account_id,
                account_ref=generation.account_ref,
                generation_id=generation.generation_id,
                canonical_identity=canonical_identity,
                projection_digest=projection.projection_digest,
                graph_digest=projection.graph_digest,
                pipeline_revision=projection.pipeline_revision,
                pipeline_config_digest=projection.pipeline_config_digest,
                pipeline_identity_digest=pipeline_identity_digest(projection),
                expected_previous_generation_id=(
                    generation.expected_active_generation_id
                ),
                expected_previous_revision=generation.expected_active_revision,
                publication_epoch=generation.publication_epoch,
                writer_owner=generation.writer_owner,
                publication_capability_digest=(
                    generation.publication_capability_digest
                ),
            )
            generation.intent = intent
            completed = self._activation.complete(intent)
            generation.intent = completed
            try:
                epoch = self._epochs.get(generation.publication_epoch)
                if (
                    epoch is None
                    or epoch[2] != "open"
                    or generation.publication_epoch in self._locally_fenced_epochs
                    or not self._activation.publication_epoch_is_open(
                        generation.publication_epoch,
                        generation.publication_capability_digest,
                    )
                ):
                    raise ProjectionActivationConflict("publication_epoch_revoked")
                self._require_authority_locked(
                    generation.creator_account_id, canonical_identity
                )
                self._repository.validate_generation_owner(
                    generation.generation_id,
                    owner_token=generation.owner_token,
                    expected_status="validated",
                )
                if (
                    self._repository._active.get(generation.account_ref)
                    != generation.expected_active_generation_id
                ):
                    raise ProjectionActivationConflict(
                        "projection_active_generation_changed"
                    )
                current = self._active_generation_locked(
                    generation.creator_account_id
                )
                self._validate_replacement_locked(projection, current)
                self._require_authority_locked(
                    generation.creator_account_id, canonical_identity
                )
                self._repository.activate(
                    generation.generation_id,
                    expected_active=generation.expected_active_generation_id,
                    owner_token=generation.owner_token,
                )
                if current is not None:
                    current.status = "retired"
                generation.status = "active"
                self._active[generation.creator_account_id] = generation.generation_id
                self._collect_locked(generation.creator_account_id)
                return True
            except BaseException:
                try:
                    self._activation.reconcile_completed(completed)
                finally:
                    try:
                        self._repository.discard_generation(
                            generation.generation_id,
                            owner_token=generation.owner_token,
                        )
                    finally:
                        generation.status = "retired"
                raise

    def discard_generation(self, generation_id: str) -> None:
        with self._lock:
            generation = self._generation_by_id_locked(generation_id)
            if generation.status == "retired":
                return
            if generation.status == "active":
                raise ProjectionActivationConflict("projection_generation_active")
            if generation.intent is not None:
                if generation.intent.state == "reserved":
                    self._activation.cancel(generation.intent.intent_id)
                elif generation.intent.state == "completed":
                    self._activation.reconcile_completed(generation.intent)
            self._repository.discard_generation(
                generation.generation_id,
                owner_token=generation.owner_token,
            )
            generation.status = "retired"
            self._collect_locked(generation.creator_account_id)

    def next_projection_generation(self, creator_account_id: str) -> int:
        with self._lock:
            values = [
                generation.artifact.projection.projection_generation
                for (account_id, _), generation in self._generations.items()
                if account_id == creator_account_id
            ]
            return max(values, default=0) + 1

    def open_publication_epoch(
        self,
        scheduler_owner_id: str,
        capability_secret: str,
        *,
        retain_fence_connection: bool = False,
    ) -> str:
        del retain_fence_connection
        with self._lock:
            epoch = str(uuid4())
            digest = capability_digest(capability_secret)
            self._activation.register_publication_epoch(
                epoch, scheduler_owner_id, digest
            )
            self._epochs[epoch] = (
                scheduler_owner_id,
                digest,
                "open",
            )
            return epoch

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_secret: str,
    ) -> None:
        with self._lock:
            observed = self._epochs.get(publication_epoch)
            if (
                observed is None
                or observed[0] != scheduler_owner_id
                or observed[1] != capability_digest(capability_secret)
            ):
                raise ProjectionActivationConflict("publication_epoch_unavailable")
            self._activation.revoke_publication_epoch(
                publication_epoch, scheduler_owner_id, observed[1]
            )
            self._locally_fenced_epochs.add(publication_epoch)
            self._epochs[publication_epoch] = (
                scheduler_owner_id,
                observed[1],
                "revoked",
            )
            self._activation.release_publication_epoch_fence(publication_epoch)

    def fence_publication_epoch(self, publication_epoch: str) -> None:
        with self._lock:
            self._locally_fenced_epochs.add(publication_epoch)

    def clear(self, creator_account_id: str) -> None:
        with self._lock:
            current = self._active_generation_locked(creator_account_id)
            if current is None:
                return
            if current.artifact.projection.pipeline_revision == CLEAR_PIPELINE_REVISION:
                return
            identity = current.canonical_identity
            cleared = empty_projection(current.artifact.projection)
        self.replace_artifact(
            RebuildArtifact(projection=cleared, nodes=[], edges=[]),
            creator_account_id=creator_account_id,
            canonical_identity=identity,
            force=True,
        )

    def _observed_identity(self, creator_account_id: str) -> CanonicalIdentity | None:
        if self._canonical_identity_reader is not None:
            return self._canonical_identity_reader(creator_account_id)
        return self._direct_authority.get(creator_account_id)

    def _require_authority_locked(
        self, creator_account_id: str, expected: CanonicalIdentity
    ) -> None:
        if self._canonical_identity_reader is None:
            current = self._direct_authority.get(creator_account_id)
            if current is not None and expected.revision < current.revision:
                raise ProjectionActivationConflict("canonical_identity_changed")
            self._direct_authority[creator_account_id] = expected
            return
        if self._canonical_identity_reader(creator_account_id) != expected:
            raise ProjectionActivationConflict("canonical_identity_changed")

    def _visible_generation_locked(
        self,
        creator_account_id: str,
        expected: CanonicalIdentity | None,
    ) -> _MemoryProjectionGeneration | None:
        generation = self._active_generation_locked(creator_account_id)
        if generation is None:
            return None
        observed = self._observed_identity(creator_account_id)
        required = expected or observed
        if (
            required is None
            or observed != required
            or generation.canonical_identity != required
            or generation.intent is None
        ):
            return None
        intent = self._activation.get(generation.generation_id)
        if (
            intent is None
            or intent.state != "completed"
            or intent != generation.intent
            or intent.projection_digest
            != generation.artifact.projection.projection_digest
            or intent.graph_digest != generation.artifact.projection.graph_digest
        ):
            return None
        return generation

    def _graph_generation_is_visible(
        self, account_partition_ref: str, generation_id: str
    ) -> bool:
        try:
            candidate = self._generation_by_id_locked(generation_id)
        except KeyError:
            return False
        if candidate.account_ref != account_partition_ref:
            return False
        generation = self._visible_generation_locked(
            candidate.creator_account_id, None
        )
        return generation is not None and generation.generation_id == generation_id

    def _active_generation_locked(
        self, creator_account_id: str
    ) -> _MemoryProjectionGeneration | None:
        generation_id = self._active.get(creator_account_id)
        return (
            None
            if generation_id is None
            else self._generations.get((creator_account_id, generation_id))
        )

    def _generation_by_id_locked(
        self, generation_id: str
    ) -> _MemoryProjectionGeneration:
        matches = [
            generation
            for (_, candidate_id), generation in self._generations.items()
            if candidate_id == generation_id
        ]
        if len(matches) != 1:
            raise KeyError("projection_generation_missing")
        return matches[0]

    @staticmethod
    def _validate_replacement_locked(
        projection: AnalyticsProjection,
        current: _MemoryProjectionGeneration | None,
    ) -> None:
        if current is None:
            return
        existing = current.artifact.projection
        if projection.source_revision < existing.source_revision:
            raise ProjectionRevisionConflict("projection_revision_moved_backwards")
        if projection.source_revision == existing.source_revision:
            if projection == existing:
                return
            identity_changed = (
                projection.pipeline_revision != existing.pipeline_revision
                or projection.pipeline_config_digest
                != existing.pipeline_config_digest
            )
            if not identity_changed:
                raise ProjectionRevisionConflict("projection_identity_unchanged")
        if projection.projection_generation <= existing.projection_generation:
            raise ProjectionRevisionConflict("projection_generation_not_advanced")

    @staticmethod
    def _validated_artifact(
        artifact: RebuildArtifact, canonical_identity: CanonicalIdentity
    ) -> RebuildArtifact:
        projection = artifact.projection
        if (
            projection.source_revision != canonical_identity.revision
            or projection.canonical_content_digest != canonical_identity.content_digest
        ):
            raise ProjectionRevisionConflict("projection_canonical_identity_invalid")
        nodes, edges = safe_graph_records(artifact.nodes, artifact.edges)
        node_counts = Counter(item.kind.value for item in nodes)
        edge_counts = Counter(item.relation.value for item in edges)
        node_ids = {item.node_id for item in nodes}
        if (
            projection.graph.source_revision != projection.source_revision
            or projection.graph.node_count != len(nodes)
            or projection.graph.edge_count != len(edges)
            or projection.graph.node_counts_by_kind != dict(sorted(node_counts.items()))
            or projection.graph.edge_counts_by_relation
            != dict(sorted(edge_counts.items()))
            or projection.graph_digest != graph_content_digest(nodes, edges)
            or projection.projection_digest != projection_content_digest(projection)
            or any(item.partition_key != projection.account_ref for item in nodes)
            or any(item.partition_key != projection.account_ref for item in edges)
            or any(
                edge.source_id not in node_ids or edge.target_id not in node_ids
                for edge in edges
            )
        ):
            raise ProjectionRevisionConflict("projection_artifact_invalid")
        return RebuildArtifact(
            projection=deepcopy(projection),
            nodes=nodes,
            edges=edges,
        )

    def _collect_locked(self, creator_account_id: str) -> None:
        retired = sorted(
            (
                generation
                for (account_id, _), generation in self._generations.items()
                if account_id == creator_account_id and generation.status == "retired"
            ),
            key=lambda generation: generation.ordinal,
            reverse=True,
        )
        for generation in retired[
            self._rollback_retention : self._rollback_retention + self._gc_batch_size
        ]:
            self._generations.pop(
                (creator_account_id, generation.generation_id), None
            )


def projection_content_digest(projection: AnalyticsProjection) -> str:
    payload = projection.model_dump(mode="json", exclude={"projection_digest"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def empty_projection(projection: AnalyticsProjection) -> AnalyticsProjection:
    """Create a truthful empty administrative replacement generation."""

    empty_window = AnalyticsWindow(scope=WindowScope.ALL_TIME)
    creator = projection.creator_metrics.model_copy(
        update={
            "conversation_count": 0,
            "participant_count": 0,
            "message_count": 0,
            "inbound_message_count": 0,
            "outbound_message_count": 0,
            "active_from": None,
            "active_until": None,
            "average_messages_per_conversation": None,
            "response_opportunity_count": 0,
            "responded_count": 0,
            "response_coverage": None,
            "average_response_seconds": None,
            "average_sentiment_score": None,
            "sentiment_counts": {},
            "topic_counts": {},
            "entity_counts": {},
            "engagement_counts": {},
            "provenance": projection.creator_metrics.provenance.model_copy(
                update={
                    "sample_count": 0,
                    "sample_coverage": None,
                    "unavailable_reason": "analytics_cleared",
                }
            ),
            "window": empty_window,
            "unavailable_reasons": {"analytics": "cleared"},
        }
    )
    candidate = projection.model_copy(
        update={
            "pipeline_revision": CLEAR_PIPELINE_REVISION,
            "pipeline_config_digest": CLEAR_PIPELINE_CONFIG_DIGEST,
            "projection_generation": projection.projection_generation + 1,
            "graph_digest": graph_content_digest([], []),
            "analyzers": [
                analyzer.model_copy(
                    update={
                        "analyzed_sample_count": 0,
                        "eligible_sample_count": 0,
                        "sample_coverage": None,
                        "mean_confidence": None,
                        "unavailable_reason": "analytics_cleared",
                    }
                )
                for analyzer in projection.analyzers
            ],
            "window": empty_window,
            "message_enrichments": [],
            "conversation_metrics": [],
            "creator_metrics": creator,
            "graph": projection.graph.model_copy(
                update={
                    "node_count": 0,
                    "edge_count": 0,
                    "node_counts_by_kind": {},
                    "edge_counts_by_relation": {},
                }
            ),
            "pipeline_identity_digest": "sha256:" + "0" * 64,
            "projection_digest": "sha256:" + "0" * 64,
        }
    )
    candidate = candidate.model_copy(
        update={"pipeline_identity_digest": pipeline_identity_digest(candidate)}
    )
    return candidate.model_copy(
        update={"projection_digest": projection_content_digest(candidate)}
    )
