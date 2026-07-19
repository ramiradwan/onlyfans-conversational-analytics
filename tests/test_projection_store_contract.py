from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analytics.identity import CanonicalIdentity, pipeline_identity_digest
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.graph_store import GraphStoreError
from app.analytics.projection_store import (
    AnalyticsProjectionStore,
    InMemoryAnalyticsProjectionStore,
)
from app.analytics.sqlite_projection_store import (
    SQLiteAnalyticsProjectionStore,
    _graph_digest,
    _projection_digest,
)
from app.models.analytics import AnalyticsProjection, GraphProjectionSummary, RebuildArtifact
from app.persistence.projection_activation import (
    InMemoryProjectionActivationRepository,
)
from app.transport.ingestion import AccountReadModel


class EmptySource:
    def __init__(self, revision: int) -> None:
        self.revision = revision

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        assert creator_account_id == "account-a"
        return AccountReadModel(view_revision=self.revision)

    def account_exists(self, creator_account_id: str) -> bool:
        return creator_account_id == "account-a"


@dataclass
class ProjectionCase:
    store: AnalyticsProjectionStore
    state: dict[str, CanonicalIdentity]


@pytest.fixture(params=["memory", "sqlite"])
def projection_case(request, tmp_path: Path) -> ProjectionCase:
    state = {"identity": CanonicalIdentity(1, "sha256:" + "1" * 64)}
    if request.param == "memory":
        return ProjectionCase(InMemoryAnalyticsProjectionStore(), state)
    activation = InMemoryProjectionActivationRepository(
        lambda account_id: state["identity"] if account_id == "account-a" else None
    )
    return ProjectionCase(
        SQLiteAnalyticsProjectionStore(
            tmp_path / "projections.sqlite3",
            activation=activation,
            canonical_identity_reader=lambda account_id: (
                state["identity"] if account_id == "account-a" else None
            ),
        ),
        state,
    )


def projection(
    source_revision: int,
    *,
    generation: int,
    pipeline_revision: str = "contract.v1",
) -> AnalyticsProjection:
    base = AnalyticsPipeline(EmptySource(source_revision)).project_account(
        "account-a"
    ).artifact.projection
    config_character = "a" if pipeline_revision == "contract.v1" else "b"
    candidate = base.model_copy(
        update={
            "pipeline_revision": pipeline_revision,
            "pipeline_config_digest": "sha256:" + config_character * 64,
            "source_revision": source_revision,
            "projection_generation": generation,
            "canonical_content_digest": "sha256:" + str(source_revision) * 64,
            "graph_digest": _graph_digest([], []),
            "graph": GraphProjectionSummary(
                account_ref=base.account_ref,
                source_revision=source_revision,
                node_count=0,
                edge_count=0,
            ),
            "projection_digest": "sha256:" + "0" * 64,
        }
    )
    candidate = candidate.model_copy(
        update={"pipeline_identity_digest": pipeline_identity_digest(candidate)}
    )
    return candidate.model_copy(
        update={"projection_digest": _projection_digest(candidate)}
    )


def test_memory_projection_publish_rejects_expired_validated_graph_generation() -> None:
    store = InMemoryAnalyticsProjectionStore(graph_lease_seconds=0.01)
    identity = CanonicalIdentity(1, "sha256:" + "1" * 64)
    candidate = projection(1, generation=1)
    generation_id = store.stage_artifact(
        RebuildArtifact(projection=candidate, nodes=[], edges=[]),
        creator_account_id="account-a",
        canonical_identity=identity,
    )
    generation = store._repository._required(generation_id)
    generation.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(GraphStoreError, match="graph_generation_ownership_lost"):
        store.publish_generation(
            generation_id,
            creator_account_id="account-a",
            canonical_identity=identity,
        )
    assert store.get("account-a", canonical_identity=identity) is None


def test_projection_replace_is_witnessed_revisioned_and_clearable(
    projection_case: ProjectionCase,
) -> None:
    store = projection_case.store
    first_identity = projection_case.state["identity"]
    first = projection(1, generation=1)
    assert store.get("account-a", canonical_identity=first_identity) is None
    assert store.replace(
        first, creator_account_id="account-a", canonical_identity=first_identity
    )
    assert not store.replace(
        first, creator_account_id="account-a", canonical_identity=first_identity
    )
    assert store.get("account-a", canonical_identity=first_identity) == first

    with pytest.raises(RuntimeError):
        store.replace(
            projection(1, generation=2),
            creator_account_id="account-a",
            canonical_identity=first_identity,
        )
    with pytest.raises(RuntimeError):
        store.replace(
            projection(0, generation=2),
            creator_account_id="account-a",
            canonical_identity=first_identity,
        )

    identity_changed = projection(
        1, generation=2, pipeline_revision="contract.v2"
    )
    assert store.replace(
        identity_changed,
        creator_account_id="account-a",
        canonical_identity=first_identity,
    )

    second_identity = CanonicalIdentity(2, "sha256:" + "2" * 64)
    projection_case.state["identity"] = second_identity
    second = projection(2, generation=3, pipeline_revision="contract.v2")
    assert store.replace(
        second, creator_account_id="account-a", canonical_identity=second_identity
    )
    assert store.get("account-a", canonical_identity=first_identity) is None
    assert store.get("account-a", canonical_identity=second_identity) == second
    store.clear("account-a")
    cleared = store.get("account-a", canonical_identity=second_identity)
    assert cleared is not None
    assert cleared.projection_generation == second.projection_generation + 1
    assert cleared.message_enrichments == []
    assert cleared.conversation_metrics == []
    assert cleared.graph.node_count == 0
    assert cleared.graph.edge_count == 0
    assert store.graph.nodes(account_ref("account-a")) == []
    assert store.graph.edges(account_ref("account-a")) == []
