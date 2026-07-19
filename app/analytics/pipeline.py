"""Canonical-store consumer that builds deterministic analytics projections."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, ContextManager, Iterator, Protocol

from pydantic import ValidationError

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.enrichment import EnrichmentStage
from app.analytics.errors import (
    CanonicalAccountNotFound,
    CanonicalRevisionChanged,
    CanonicalStateInvalid,
)
from app.analytics.graph_projection import RelationshipGraphProjector
from app.analytics.graph_privacy import graph_content_digest
from app.analytics.graph_store import (
    GraphReader,
    InMemoryGraphRepository,
)
from app.analytics.identity import (
    CanonicalIdentity,
    canonical_identity,
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
from app.models.analytics import (
    AnalyticsProjection,
    AnalyticsWindow,
    CanonicalConversation,
    RebuildArtifact,
    WindowScope,
)
from app.transport.ingestion import AccountReadModel


class CanonicalReadModelSource(Protocol):
    """Read-only portion of the canonical repository required by analytics."""

    def account_read_model(self, creator_account_id: str) -> AccountReadModel: ...

    def account_exists(self, creator_account_id: str) -> bool: ...

    def account_revisions(self) -> list[tuple[str, int]]: ...


@dataclass(frozen=True, slots=True)
class PipelineRun:
    artifact: RebuildArtifact
    changed: bool
    attempts: int


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

    def artifact(self) -> RebuildArtifact:
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
    ) -> None:
        if max_revision_retries <= 0:
            raise ValueError("max_revision_retries must be positive")
        self.source = source
        self._memory_graph_repository = None
        self.projections: AtomicAnalyticsProjectionStore
        identity_reader = lambda account_id: (
            canonical_identity(source.account_read_model(account_id))
            if source.account_exists(account_id)
            else None
        )
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
        self.pipeline_revision = (
            f"analytics.pipeline.v2+{self.enrichment.revision}+graph.relationship.v1"
        )
        self.pipeline_config_digest = stable_config_digest(
            name="analytics_pipeline",
            revision=self.pipeline_revision,
            config={
                "enrichment_config_digest": self.enrichment.config_digest,
                "graph_projector": "relationship_graph.v1",
                "timestamp_policy": "aware_utc_stable_source_order",
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

    def account_exists(self, creator_account_id: str) -> bool:
        return self.source.account_exists(creator_account_id)

    def canonical_account(self, creator_account_id: str) -> AccountReadModel:
        """Read one existing canonical account on a caller-selected thread."""

        if not creator_account_id.strip() or not self.source.account_exists(
            creator_account_id
        ):
            raise CanonicalAccountNotFound()
        return self.source.account_read_model(creator_account_id)

    @staticmethod
    def _candidate(
        artifact: RebuildArtifact,
        *,
        creator_account_id: str,
        canonical_snapshot: CanonicalIdentity,
        publication_epoch: str | None,
        staged_generation_id: str | None,
        reset_derived: bool,
        requires_publication: bool,
        attempts: int,
    ) -> ProjectionCandidate:
        projection = artifact.projection
        return ProjectionCandidate(
            creator_account_id=creator_account_id,
            source_revision=projection.source_revision,
            projection_generation=projection.projection_generation,
            pipeline_revision=projection.pipeline_revision,
            pipeline_config_digest=projection.pipeline_config_digest,
            canonical_content_digest=canonical_snapshot.content_digest,
            publication_epoch=publication_epoch,
            staged_generation_id=staged_generation_id,
            artifact_json=artifact.model_dump_json().encode("utf-8"),
            reset_derived=reset_derived,
            requires_publication=requires_publication,
            attempts=attempts,
        )

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
                account = self.source.account_read_model(creator_account_id)
                account_identity = canonical_identity(account)
                check_cancelled(cancellation_check)
                current = self.projections.get(
                    creator_account_id,
                    canonical_identity=account_identity,
                )
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
                check_cancelled(cancellation_check)
                observed = self.source.account_read_model(creator_account_id)
                check_cancelled(cancellation_check)
                if canonical_identity(observed) != account_identity:
                    continue
                if publication_epoch is None:
                    publication_epoch = self.open_publication_epoch(
                        f"direct-pipeline-{id(self):x}"
                    )
                staged_generation_id = self.projections.stage_artifact(
                    artifact,
                    creator_account_id=creator_account_id,
                    canonical_identity=account_identity,
                    publication_epoch=publication_epoch,
                    cancellation_check=cancellation_check,
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
                    publication_epoch=publication_epoch,
                    staged_generation_id=staged_generation_id,
                    reset_derived=reset_derived,
                    requires_publication=True,
                    attempts=attempt,
                )
        raise CanonicalRevisionChanged()

    def publish_candidate(self, candidate: ProjectionCandidate) -> PipelineRun:
        """Perform the scheduler-approved canonical witness/CAS publication."""

        artifact = candidate.artifact()
        projection = artifact.projection
        if (
            projection.account_ref != account_ref(candidate.creator_account_id)
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
        if canonical_identity(
            self.source.account_read_model(candidate.creator_account_id)
        ) != expected_identity:
            raise CanonicalRevisionChanged()

        with self._account_lock(candidate.creator_account_id):
            if candidate.staged_generation_id is not None:
                changed = self.projections.publish_generation(
                    candidate.staged_generation_id,
                    creator_account_id=candidate.creator_account_id,
                    canonical_identity=expected_identity,
                )
                return PipelineRun(
                    artifact=artifact,
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
                        artifact=artifact,
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
        account = self.source.account_read_model(creator_account_id)
        if account.view_revision < requested_revision:
            return False
        projection = self.active_projection(creator_account_id, account)
        return bool(
            projection is not None
            and projection.source_revision >= requested_revision
            and projection.pipeline_revision == self.pipeline_revision
            and projection.pipeline_config_digest == self.pipeline_config_digest
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
        with publication_lock or nullcontext():
            if publication_allowed is not None and not publication_allowed():
                raise CanonicalRevisionChanged()
            return self.publish_candidate(candidate)

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
        check_cancelled(cancellation_check)
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
        nodes, edges, graph_summary = self.graph_projector.project(
            creator_account_id,
            account.view_revision,
            conversations,
            enrichments,
            conversation_metrics,
            cancellation_check=cancellation_check,
        )
        check_cancelled(cancellation_check)
        projection = AnalyticsProjection(
            pipeline_revision=self.pipeline_revision,
            pipeline_config_digest=self.pipeline_config_digest,
            pipeline_identity_digest="sha256:" + "0" * 64,
            account_ref=account_ref(creator_account_id),
            source_revision=account.view_revision,
            projection_generation=projection_generation,
            canonical_content_digest=canonical_identity(account).content_digest,
            graph_digest=graph_content_digest(nodes, edges),
            analyzers=self.enrichment.provenance(enrichments),
            window=AnalyticsWindow(
                scope=WindowScope.ALL_TIME,
                start=creator_metrics.active_from,
                end=creator_metrics.active_until,
            ),
            message_enrichments=enrichments,
            conversation_metrics=conversation_metrics,
            creator_metrics=creator_metrics,
            graph=graph_summary,
            projection_digest="sha256:" + "0" * 64,
        )
        projection = projection.model_copy(
            update={"pipeline_identity_digest": pipeline_identity_digest(projection)}
        )
        payload = projection.model_dump(mode="json", exclude={"projection_digest"})
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        projection = projection.model_copy(
            update={
                "projection_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            }
        )
        check_cancelled(cancellation_check)
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
