"""Canonical-store consumer that builds deterministic analytics projections."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, ContextManager, Iterator, Protocol

from contextvars import ContextVar
from app.analytics.historical_derivation import (
    HISTORICAL_DERIVATION_SCHEMA,
    historical_retention_cutoff,
    source_time_is_authorized,
    PARTICIPANT_ANALYTICS_MAX_DAYS,
)

_RETENTION_CUTOFF: ContextVar[datetime | None] = ContextVar(
    "analytics_retention_cutoff", default=None
)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

from pydantic import ValidationError

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.enrichment import EnrichmentStage
from app.analytics.generation_reference import GenerationReference
from app.analytics.enrichment_cache import reuse_build
from app.analytics.compact_graph import CompactArtifact, CompactGraph
from app.analytics.conversation_reuse import assemble, conversation_build
from app.analytics.source_snapshot import SourceCatalog
from app.analytics.errors import (
    CanonicalAccountNotFound,
    CanonicalRevisionChanged,
    CanonicalStateInvalid,
)
from app.analytics.graph_projection import RelationshipGraphProjector
from app.analytics.shared_graph import (
    GRAPH_SEGMENT_ROOT_PIPELINE_REVISION,
    projection_graph_digest,
)
from app.analytics.graph_store import (
    GraphReader,
    InMemoryGraphRepository,
)
from app.analytics.identity import (
    CanonicalIdentity,
    canonical_identity, source_identity, snapshot_identity,
    pipeline_identity_digest,
)
from app.analytics.metrics import build_conversation_metrics, build_creator_metrics
from app.analytics.provenance import stable_config_digest
from app.analytics.opaque_refs import account_ref
from app.analytics.projection_store import (
    AnalyticsProjectionStore,
    AtomicAnalyticsProjectionStore,
    InMemoryAnalyticsProjectionStore,
)
from app.canonical.read_models import AccountReadModel
from app.models.analytics import (
    AnalyticsProjection,
    AnalyticsWindow,
    CanonicalConversation,
    RebuildArtifact,
    WindowScope,
)


class CanonicalReadModelSource(Protocol):
    """Read-only portion of the canonical repository required by analytics."""

    def account_read_model(self, creator_account_id: str) -> AccountReadModel: ...

    def account_exists(self, creator_account_id: str) -> bool: ...

    def account_revisions(self) -> list[tuple[str, int]]: ...


@dataclass(frozen=True, slots=True)
class PipelineRun:
    _artifact: RebuildArtifact | None
    changed: bool
    attempts: int
    reference: GenerationReference | None = field(default=None, repr=False)
    _artifact_reader: Callable[[], RebuildArtifact] | None = field(default=None, repr=False, compare=False)

    @property
    def artifact(self) -> RebuildArtifact:
        if self._artifact is not None:
            return self._artifact
        if self._artifact_reader is None:
            raise CanonicalRevisionChanged()
        return self._artifact_reader()


@dataclass(frozen=True, slots=True)
class ProjectionCandidate:
    """Immutable handoff from background computation to active publication."""

    creator_account_id: str
    source_revision: int
    projection_generation: int
    pipeline_revision: str
    pipeline_config_digest: str
    canonical_content_digest: str
    publication_epoch: str | None
    staged_generation_id: str | None
    artifact_json: bytes
    reset_derived: bool
    requires_publication: bool
    attempts: int

    reference: GenerationReference | None = None
    source_identity_proof: object | None = field(default=None, repr=False, compare=False)
    _artifact_reader: Callable[[], RebuildArtifact] | None = field(default=None, repr=False, compare=False)

    def artifact(self) -> RebuildArtifact:
        if self.reference is not None:
            if self._artifact_reader is None:
                raise CanonicalRevisionChanged()
            return self._artifact_reader()
        return RebuildArtifact.model_validate_json(self.artifact_json)


class AnalyticsPipeline:
    """Replay canonical account state into metrics, enrichments, and graph state."""

    def __init__(
        self,
        source: CanonicalReadModelSource,
        *,
        projections: AnalyticsProjectionStore | None = None,
        graph: GraphReader | None = None,
        enrichment: EnrichmentStage | None = None,
        graph_projector: RelationshipGraphProjector | None = None,
        max_revision_retries: int = 3,
        clock: Callable[[], datetime] = utc_now,
        reuse_enrichment: bool = True,
        reuse_conversations: bool = True,
        compact_graph: bool = True,
    ) -> None:
        if max_revision_retries <= 0:
            raise ValueError("max_revision_retries must be positive")
        self.source = source
        self._memory_graph_repository = None
        self.projections: AtomicAnalyticsProjectionStore
        identity_reader = lambda account_id: source_identity(source, account_id)
        if projections is None and graph is None:
            self._memory_graph_repository = InMemoryGraphRepository()
            self.projections = InMemoryAnalyticsProjectionStore(
                graph_repository=self._memory_graph_repository,
                canonical_identity_reader=identity_reader,
            )
            self.graph = self.projections.graph
        elif projections is None:
            repository = getattr(graph, "_repository", None)
            if not isinstance(repository, InMemoryGraphRepository):
                repository = InMemoryGraphRepository()
            self._memory_graph_repository = repository
            self.projections = InMemoryAnalyticsProjectionStore(
                graph_repository=repository,
                canonical_identity_reader=identity_reader,
            )
            self.graph = self.projections.graph
        else:
            if not isinstance(projections, AtomicAnalyticsProjectionStore):
                raise ValueError("projection_store_not_atomic")
            self.projections = projections
            projection_graph = projections.graph
            if (
                graph is not None
                and graph is not projection_graph
            ):
                raise ValueError("projection_graph_mismatch")
            self.graph = projection_graph
        self.enrichment = enrichment or EnrichmentStage()
        self.graph_projector = graph_projector or RelationshipGraphProjector()
        self.max_revision_retries = max_revision_retries
        self._retention_clock = clock
        self.reuse_enrichment = reuse_enrichment
        self.reuse_conversations = reuse_conversations
        self.compact_graph = compact_graph
        self.pipeline_revision = (
            f"analytics.pipeline.v3+{self.enrichment.revision}"
            "+graph.relationship.v1+enrichment.units.v1"
            f"+{GRAPH_SEGMENT_ROOT_PIPELINE_REVISION}"
        )
        self.pipeline_config_digest = stable_config_digest(
            name="analytics_pipeline",
            revision=self.pipeline_revision,
            config={
                "enrichment_config_digest": self.enrichment.config_digest,
                "graph_projector": "relationship_graph.v1",
                "graph_digest_policy": GRAPH_SEGMENT_ROOT_PIPELINE_REVISION,
                "timestamp_policy": "aware_utc_stable_source_order",
                "participant_retention_days": PARTICIPANT_ANALYTICS_MAX_DAYS,
                "retention_clock": "canonical_message_sent_at",
                "historical_derivation_provenance": HISTORICAL_DERIVATION_SCHEMA,
            },
        )
        self._account_locks: dict[str, tuple[RLock, int]] = {}
        self._account_locks_guard = RLock()
        self._direct_publication_capability = secrets.token_hex(32)

    @contextmanager
    def _account_lock(self, creator_account_id: str) -> Iterator[None]:
        """Serialize one account while releasing its lock record when idle."""

        with self._account_locks_guard:
            existing = self._account_locks.get(creator_account_id)
            lock, users = existing if existing is not None else (RLock(), 0)
            self._account_locks[creator_account_id] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._account_locks_guard:
                current = self._account_locks.get(creator_account_id)
                if current is not None and current[0] is lock:
                    remaining = current[1] - 1
                    if remaining:
                        self._account_locks[creator_account_id] = (lock, remaining)
                    else:
                        self._account_locks.pop(creator_account_id, None)

    def _expired(self, projection) -> bool:
        first = getattr(projection.message_enrichments, "first_source_at", None)
        if first is not None:
            return first + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) <= self._retention_clock()
        return any(message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                   <= self._retention_clock() for message in projection.message_enrichments)

    def _capture_source(self, account_id, cancellation_check):
        capture = getattr(self.source, "analytics_snapshot", None)
        if (callable(capture) and callable(getattr(self.projections, "load_conversation_fragment", None))
                and type(self.graph_projector) is RelationshipGraphProjector
                and type(self.enrichment) is EnrichmentStage
                and all(self.enrichment._input_policies)):
            self.enrichment.validate_configuration()
            return capture(account_id, cancellation_check=cancellation_check)
        return self.source.account_read_model(account_id)

    def account_exists(self, creator_account_id: str) -> bool:
        return self.source.account_exists(creator_account_id)

    def canonical_account(self, creator_account_id: str) -> AccountReadModel:
        """Read one existing canonical account on a caller-selected thread."""

        if not creator_account_id.strip() or not self.source.account_exists(
            creator_account_id
        ):
            raise CanonicalAccountNotFound()
        return self.source.account_read_model(creator_account_id)

    def _candidate(
        self,
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_snapshot: CanonicalIdentity,
        source_identity_proof: object | None,
        publication_epoch: str | None,
        staged_generation_id: str | None,
        reset_derived: bool,
        requires_publication: bool,
        attempts: int,
    ) -> ProjectionCandidate:
        projection = artifact.projection
        reference = None
        reader = None
        if staged_generation_id is not None and getattr(self.projections, "generation_references_supported", lambda: False)():
            reference = GenerationReference.from_projection(projection, staged_generation_id, publication_epoch)
            self.projections.check_generation_reference(creator_account_id, reference)
            reader = lambda: self._read_reference(creator_account_id, reference)
        return ProjectionCandidate(
            creator_account_id=creator_account_id,
            source_revision=projection.source_revision,
            projection_generation=projection.projection_generation,
            pipeline_revision=projection.pipeline_revision,
            pipeline_config_digest=projection.pipeline_config_digest,
            canonical_content_digest=canonical_snapshot.content_digest,
            publication_epoch=publication_epoch,
            staged_generation_id=staged_generation_id,
            artifact_json=b"" if reference is not None else artifact.model_dump_json().encode("utf-8"),
            reference=reference, source_identity_proof=source_identity_proof,
            _artifact_reader=reader,
            reset_derived=reset_derived,
            requires_publication=requires_publication,
            attempts=attempts,
        )

    def _source_identity_matches(
        self, account_id: str, identity: CanonicalIdentity, proof: object | None
    ) -> bool:
        verify = getattr(self.source, "verify_identity_proof", None)
        if proof is not None and callable(verify):
            matched = verify(account_id, identity, proof)
            if matched is not None:
                return bool(matched)
        return source_identity(self.source, account_id) == identity

    def build_candidate(
        self,
        creator_account_id: str,
        *,
        force: bool = False,
        publication_epoch: str | None = None,
        cancellation_check: CancellationCheck | None = None,
    ) -> ProjectionCandidate:
        """Compute an immutable candidate without mutating active stores."""

        check_cancelled(cancellation_check)
        if not creator_account_id.strip():
            raise CanonicalAccountNotFound()
        if not self.account_exists(creator_account_id):
            raise CanonicalAccountNotFound()
        with self._account_lock(creator_account_id):
            for attempt in range(1, self.max_revision_retries + 1):
                check_cancelled(cancellation_check)
                account = self._capture_source(creator_account_id, cancellation_check)
                account_identity = snapshot_identity(account)
                source_proof = getattr(account, "identity_proof", None)
                check_cancelled(cancellation_check)
                current = self.projections.get(
                    creator_account_id,
                    canonical_identity=account_identity,
                )
                if current is not None and self._expired(current):
                    self.projections.clear(creator_account_id)
                    current = None
                existing = self.projections.get(creator_account_id)
                graph_revision = self.graph.partition_revision(
                    account_ref(creator_account_id)
                )
                if (
                    not force
                    and current is not None
                    and current.source_revision == account.view_revision
                    and current.pipeline_revision == self.pipeline_revision
                    and current.pipeline_config_digest
                    == self.pipeline_config_digest
                    and graph_revision == account.view_revision
                ):
                    return self._candidate(
                        self._artifact(current, creator_account_id),
                        creator_account_id=creator_account_id,
                        canonical_snapshot=account_identity,
                        source_identity_proof=source_proof,
                        publication_epoch=publication_epoch,
                        staged_generation_id=None,
                        reset_derived=False,
                        requires_publication=False,
                        attempts=attempt,
                    )

                reset_derived = force or (
                    existing is not None
                    and (
                        existing.source_revision > account.view_revision
                        or existing.pipeline_revision != self.pipeline_revision
                        or existing.pipeline_config_digest
                        != self.pipeline_config_digest
                    )
                )
                generation = self._next_generation(existing, account.view_revision)
                next_generation = getattr(
                    self.projections, "next_projection_generation", None
                )
                if existing is None and callable(next_generation):
                    generation = next_generation(creator_account_id)
                try:
                    with reuse_build(self.projections, creator_account_id,
                            self._retention_clock, cancellation_check,
                            enabled=self.reuse_enrichment) as reuse, conversation_build(
                                self.projections, creator_account_id,
                                compact=self.compact_graph,
                            ) as conversation_state:
                        if cancellation_check is None:
                            artifact = self._build(
                                creator_account_id,
                                account,
                                projection_generation=generation,
                            )
                        else:
                            artifact = self._build(
                                creator_account_id,
                                account,
                                projection_generation=generation,
                                cancellation_check=cancellation_check,
                            )
                except CanonicalRevisionChanged:
                    continue
                check_cancelled(cancellation_check)
                if not self._source_identity_matches(
                    creator_account_id, account_identity, source_proof
                ):
                    continue
                check_cancelled(cancellation_check)
                if publication_epoch is None:
                    publication_epoch = self.open_publication_epoch(
                        f"direct-pipeline-{id(self):x}"
                    )
                stage = self.projections.stage_artifact
                if type(self.graph_projector) is RelationshipGraphProjector and type(self.enrichment) is EnrichmentStage:
                    stage = getattr(self.projections, "stage_built_artifact", stage)
                enrichment_units = tuple(conversation_state.enrichment_units)
                enrichment_units_complete = (
                    bool(enrichment_units)
                    and len(enrichment_units)
                        == len(artifact.projection.conversation_metrics)
                    and sum(unit.header.message_count for unit in enrichment_units)
                        == artifact.projection.creator_metrics.message_count
                )
                staged_generation_id = stage(
                    artifact,
                    creator_account_id=creator_account_id,
                    canonical_identity=account_identity,
                    publication_epoch=publication_epoch,
                    cancellation_check=cancellation_check,
                    **({"enrichment_entries": tuple(reuse.entries.values())}
                       if reuse and not enrichment_units_complete else {}),
                    **({"conversation_pages": tuple(conversation_state.page_sets)}
                       if conversation_state.page_sets else {}),
                    **({"conversation_graph_units": tuple(conversation_state.graph_units)}
                       if conversation_state.graph_units else {}),
                    **({"conversation_enrichment_units": enrichment_units}
                       if enrichment_units else {}),
                    **({"conversation_fragments": tuple(conversation_state.entries)}
                       if conversation_state.entries else {}),
                )
                try:
                    check_cancelled(cancellation_check)
                except BaseException:
                    self.projections.discard_generation(staged_generation_id)
                    raise
                return self._candidate(
                    artifact,
                    creator_account_id=creator_account_id,
                    canonical_snapshot=account_identity,
                    source_identity_proof=source_proof,
                    publication_epoch=publication_epoch,
                    staged_generation_id=staged_generation_id,
                    reset_derived=reset_derived,
                    requires_publication=True,
                    attempts=attempt,
                )
        raise CanonicalRevisionChanged()

    def _read_reference(self, account_id, reference):
        if reference.retention_due_at is not None and reference.retention_due_at <= self._retention_clock():
            raise CanonicalRevisionChanged()
        artifact = self.projections.read_generation_artifact(account_id, reference)
        if self._expired(artifact.projection):
            raise CanonicalRevisionChanged()
        return artifact

    def publish_candidate(self, candidate: ProjectionCandidate) -> PipelineRun:
        """Perform the scheduler-approved canonical witness/CAS publication."""

        reference = candidate.reference
        artifact = None if reference is not None else candidate.artifact()
        projection = reference if reference is not None else artifact.projection
        if reference is not None:
            if candidate.staged_generation_id != reference.generation_id or candidate.publication_epoch != reference.publication_epoch:
                raise CanonicalRevisionChanged()
            self.projections.check_generation_reference(candidate.creator_account_id, reference)
        expired = (reference.retention_due_at is not None and reference.retention_due_at <= self._retention_clock()) if reference else self._expired(projection)
        if expired:
            raise CanonicalRevisionChanged()
        if (
            projection.account_ref != account_ref(candidate.creator_account_id)
            or projection.canonical_content_digest != candidate.canonical_content_digest
            or projection.source_revision != candidate.source_revision
            or projection.projection_generation != candidate.projection_generation
            or projection.pipeline_revision != candidate.pipeline_revision
            or projection.pipeline_config_digest != candidate.pipeline_config_digest
            or projection.pipeline_revision != self.pipeline_revision
            or projection.pipeline_config_digest != self.pipeline_config_digest
        ):
            raise CanonicalRevisionChanged()
        expected_identity = CanonicalIdentity(
            revision=candidate.source_revision,
            content_digest=candidate.canonical_content_digest,
        )
        if not self._source_identity_matches(
            candidate.creator_account_id,
            expected_identity,
            candidate.source_identity_proof,
        ):
            raise CanonicalRevisionChanged()

        with self._account_lock(candidate.creator_account_id):
            if candidate.staged_generation_id is not None:
                changed = self.projections.publish_generation(
                    candidate.staged_generation_id,
                    creator_account_id=candidate.creator_account_id,
                    canonical_identity=expected_identity,
                    source_identity_proof=candidate.source_identity_proof,
                )
                refresh = getattr(self.source, "refresh_identity_cache", None)
                if callable(refresh):
                    try:
                        refresh(candidate.creator_account_id)
                    except Exception:
                        # Optional cache warming cannot undo a completed publication.
                        # Reads still verify current tokens and fail closed on a miss.
                        pass
                return PipelineRun(
                    _artifact=artifact,
                    _artifact_reader=candidate.artifact if artifact is None else None,
                    reference=reference,
                    changed=changed,
                    attempts=candidate.attempts,
                )
            existing = self.projections.get(
                candidate.creator_account_id,
                canonical_identity=expected_identity,
            )
            graph_revision = self.graph.partition_revision(
                account_ref(candidate.creator_account_id)
            )
            if existing is not None and existing.source_revision > candidate.source_revision:
                raise CanonicalRevisionChanged()
            if not candidate.requires_publication:
                if (
                    existing == projection
                    and graph_revision == candidate.source_revision
                ):
                    return PipelineRun(
                        _artifact=artifact,
                        _artifact_reader=candidate.artifact if artifact is None else None,
                        reference=reference,
                        changed=False,
                        attempts=candidate.attempts,
                    )
                raise CanonicalRevisionChanged()
            raise CanonicalRevisionChanged()

    def open_publication_epoch(
        self,
        scheduler_owner_id: str,
        capability_secret: str | None = None,
        *,
        retain_fence_connection: bool = False,
    ) -> str:
        return self.projections.open_publication_epoch(
            scheduler_owner_id,
            capability_secret or self._direct_publication_capability,
            retain_fence_connection=retain_fence_connection,
        )

    def ensure_projection_storage(self) -> None:
        ensure = getattr(self.projections, "ensure_ready", None)
        if callable(ensure):
            ensure()

    def projection_storage_requires_recovery(self) -> bool:
        return callable(getattr(self.projections, "ensure_ready", None))

    def set_projection_failure_callback(
        self, callback: Callable[[str | None], None] | None
    ) -> None:
        setter = getattr(self.projections, "set_failure_callback", None)
        if callable(setter):
            setter(callback)

    def close_projection_storage(self) -> None:
        closer = getattr(self.projections, "close", None)
        if callable(closer):
            closer()

    def revoke_publication_epoch(
        self,
        publication_epoch: str,
        scheduler_owner_id: str,
        capability_secret: str | None = None,
    ) -> None:
        self.projections.revoke_publication_epoch(
            publication_epoch,
            scheduler_owner_id,
            capability_secret or self._direct_publication_capability,
        )

    def fence_publication_epoch(self, publication_epoch: str) -> None:
        self.projections.fence_publication_epoch(publication_epoch)

    def discard_candidate(self, candidate: ProjectionCandidate) -> None:
        """Retire an unpublished inactive generation after coalescing/cancellation."""

        if candidate.staged_generation_id is None:
            return
        self.projections.discard_generation(candidate.staged_generation_id)

    def active_projection(
        self,
        creator_account_id: str,
        account: AccountReadModel,
    ) -> AnalyticsProjection | None:
        """Read only a projection bound to the caller's canonical snapshot."""

        return self.projections.get(
            creator_account_id,
            canonical_identity=canonical_identity(account),
        )

    def projection_is_current(
        self, creator_account_id: str, requested_revision: int
    ) -> bool:
        """Worker-thread currentness check used by scheduler admission."""

        if not self.source.account_exists(creator_account_id):
            return False
        identity = source_identity(self.source, creator_account_id)
        if identity is None or identity.revision < requested_revision:
            return False
        currentness = getattr(self.projections, "projection_currentness", None)
        if callable(currentness):
            current = currentness(creator_account_id, identity, self.pipeline_revision,
                                  self.pipeline_config_digest, self._retention_clock)
        else:
            projection = self.projections.get(creator_account_id, canonical_identity=identity)
            current = bool(
                projection is not None
                and not self._expired(projection)
                and projection.source_revision >= requested_revision
                and projection.pipeline_revision == self.pipeline_revision
                and projection.pipeline_config_digest == self.pipeline_config_digest
            )
        # Stored-state verification can outlive the source identity cache.
        return bool(current) and self._source_identity_matches(
            creator_account_id, identity, None
        )

    def project_account(
        self,
        creator_account_id: str,
        *,
        force: bool = False,
        publication_lock: ContextManager[object] | None = None,
        publication_allowed: Callable[[], bool] | None = None,
    ) -> PipelineRun:
        """Refresh one account iff its canonical revision is not already projected."""

        candidate = self.build_candidate(creator_account_id, force=force)
        snapshot = candidate.artifact()
        with publication_lock or nullcontext():
            if publication_allowed is not None and not publication_allowed():
                raise CanonicalRevisionChanged()
            run = self.publish_candidate(candidate)
            return PipelineRun(_artifact=snapshot, changed=run.changed, attempts=run.attempts)

    def _next_generation(
        self,
        existing: AnalyticsProjection | None,
        source_revision: int,
    ) -> int:
        if existing is None:
            return 1
        identity_unchanged = (
            existing.source_revision == source_revision
            and existing.pipeline_revision == self.pipeline_revision
            and existing.pipeline_config_digest == self.pipeline_config_digest
        )
        return (
            existing.projection_generation
            if identity_unchanged
            else existing.projection_generation + 1
        )

    def rebuild_account(self, creator_account_id: str) -> PipelineRun:
        """Force an exact graph/projection replacement from canonical state."""

        return self.project_account(creator_account_id, force=True)

    def _build(
        self,
        creator_account_id: str,
        account: AccountReadModel,
        *,
        projection_generation: int,
        cancellation_check: CancellationCheck | None = None,
    ) -> RebuildArtifact:
        token = _RETENTION_CUTOFF.set(
            historical_retention_cutoff(self._retention_clock())
        )
        try:
            return self._build_inner(
                creator_account_id,
                account,
                projection_generation=projection_generation,
                cancellation_check=cancellation_check,
            )
        finally:
            _RETENTION_CUTOFF.reset(token)

    def _build_inner(
        self,
        creator_account_id: str,
        account: AccountReadModel,
        *,
        projection_generation: int,
        cancellation_check: CancellationCheck | None = None,
    ) -> RebuildArtifact:
        check_cancelled(cancellation_check)
        if isinstance(account, SourceCatalog):
            enrichments, conversation_metrics, nodes, edges, graph_summary = assemble(
                self, creator_account_id, account, _RETENTION_CUTOFF.get(), cancellation_check)
        else:
            conversations = self._canonical_conversations(
                account,
                cancellation_check=cancellation_check,
            )
            enrichments = []
            conversation_metrics = []
            for conversation in conversations:
                check_cancelled(cancellation_check)
                conversation_enrichments = self.enrichment.enrich_conversation(
                    creator_account_id,
                    conversation,
                    cancellation_check=cancellation_check,
                )
                enrichments.extend(conversation_enrichments)
                check_cancelled(cancellation_check)
                conversation_metrics.append(
                    build_conversation_metrics(
                        creator_account_id,
                        conversation,
                        conversation_enrichments,
                    )
                )
        check_cancelled(cancellation_check)
        creator_metrics = build_creator_metrics(
            creator_account_id, conversation_metrics
        )
        check_cancelled(cancellation_check)
        if not isinstance(account, SourceCatalog):
            nodes, edges, graph_summary = self.graph_projector.project(
                creator_account_id,
                account.view_revision,
                conversations,
                enrichments,
                conversation_metrics,
                cancellation_check=cancellation_check,
            )
        check_cancelled(cancellation_check)
        streamed_enrichments = callable(getattr(enrichments, "iter_canonical_records", None))
        projection = AnalyticsProjection(
            pipeline_revision=self.pipeline_revision,
            pipeline_config_digest=self.pipeline_config_digest,
            pipeline_identity_digest="sha256:" + "0" * 64,
            account_ref=account_ref(creator_account_id),
            source_revision=account.view_revision,
            projection_generation=projection_generation,
            canonical_content_digest=snapshot_identity(account).content_digest,
            graph_digest=projection_graph_digest(
                self.pipeline_revision, nodes, edges,
                check=lambda: check_cancelled(cancellation_check),
            ),
            analyzers=self.enrichment.provenance(enrichments),
            window=AnalyticsWindow(
                scope=WindowScope.ALL_TIME,
                start=creator_metrics.active_from,
                end=creator_metrics.active_until,
            ),
            message_enrichments=[] if streamed_enrichments else enrichments,
            conversation_metrics=conversation_metrics,
            creator_metrics=creator_metrics,
            graph=graph_summary,
            projection_digest="sha256:" + "0" * 64,
        )
        if streamed_enrichments:
            projection = projection.model_copy(update={"message_enrichments": enrichments})
        projection = projection.model_copy(
            update={"pipeline_identity_digest": pipeline_identity_digest(projection)}
        )
        from app.analytics.projection_encoding import projection_digest

        projection = projection.model_copy(update={
            "projection_digest": projection_digest(projection,
                check=lambda: check_cancelled(cancellation_check))
        })
        check_cancelled(cancellation_check)
        if isinstance(nodes, CompactGraph) or getattr(nodes, "compact_graph", False):
            return CompactArtifact(projection, nodes)
        return RebuildArtifact(projection=projection, nodes=nodes, edges=edges)

    def _artifact(
        self, projection: AnalyticsProjection, creator_account_id: str
    ) -> RebuildArtifact:
        return RebuildArtifact(
            projection=projection,
            nodes=self.graph.nodes(account_ref(creator_account_id)),
            edges=self.graph.edges(account_ref(creator_account_id)),
        )

    @classmethod
    def _canonical_conversations(
        cls,
        account: AccountReadModel,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> list[CanonicalConversation]:
        conversations = cls._canonical_conversations_inner(
            account,
            cancellation_check=cancellation_check,
        )
        cutoff = _RETENTION_CUTOFF.get()
        if cutoff is None:
            return conversations
        bounded: list[CanonicalConversation] = []
        for conversation in conversations:
            messages = [
                message
                for message in conversation.messages
                if source_time_is_authorized(message.sent_at, cutoff=cutoff)
            ]
            if not messages:
                continue
            bounded.append(
                conversation.model_copy(
                    update={
                        "last_message_at": messages[-1].sent_at,
                        "messages": messages,
                    }
                )
            )
        return bounded

    @classmethod
    def _canonical_conversations_inner(
        cls,
        account: AccountReadModel,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> list[CanonicalConversation]:
        conversations: list[CanonicalConversation] = []
        for key in sorted(account.conversations):
            check_cancelled(cancellation_check)
            try:
                conversation = CanonicalConversation.model_validate(
                    account.conversations[key]
                )
            except ValidationError as error:
                raise CanonicalStateInvalid() from error
            if conversation.conversation_id != key:
                raise CanonicalStateInvalid()
            normalized_messages = [
                message.model_copy(update={"sent_at": cls._utc(message.sent_at)})
                for message in conversation.messages
            ]
            source_ordinals = [
                message.source_ordinal for message in normalized_messages
            ]
            if sorted(source_ordinals) != list(range(len(normalized_messages))):
                raise CanonicalStateInvalid()
            normalized_messages.sort(
                key=lambda message: (message.sent_at, message.source_ordinal)
            )
            conversations.append(
                conversation.model_copy(
                    update={
                        "last_message_at": (
                            cls._utc(conversation.last_message_at)
                            if conversation.last_message_at is not None
                            else None
                        ),
                        "messages": normalized_messages,
                    }
                )
            )
        check_cancelled(cancellation_check)
        return conversations

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalStateInvalid()
        return value.astimezone(timezone.utc)


def rebuild_projection(
    source: CanonicalReadModelSource,
    creator_account_id: str,
    *,
    projections: AnalyticsProjectionStore | None = None,
    graph: GraphReader | None = None,
) -> RebuildArtifact:
    """Functional rebuild entry point for jobs and tests."""

    pipeline = AnalyticsPipeline(
        source,
        projections=projections,
        graph=graph,
    )
    return pipeline.rebuild_account(creator_account_id).artifact
