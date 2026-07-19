from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.opaque_refs import account_ref
from app.analytics.rebuild import rebuild_from_args
from app.models.analytics import GraphNodeKind, GraphRelation
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.protocol.payloads import IngestDeltaPayload, IngestSnapshotPayload
from app.services.data_ingest import CanonicalAnalyticsConsumer
from app.services.onlyfans_client import OnlyFansClient
from app.transport.ingestion import AccountReadModel, IngestionService, StreamKey


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"


def snapshot(name: str) -> IngestSnapshotPayload:
    return IngestSnapshotPayload.model_validate_json(
        (FIXTURES / f"{name}.snapshot.json").read_text(encoding="utf-8")
    )


def stream_key(payload: IngestSnapshotPayload) -> StreamKey:
    return StreamKey(
        payload.creator_account_id,
        payload.agent_installation_id,
        payload.agent_stream_id,
    )


async def seed(
    repositories: CanonicalRepositories, name: str
) -> tuple[IngestionService, IngestSnapshotPayload]:
    payload = snapshot(name)
    service = IngestionService(repositories.ingestion)
    outcome = await service.ingest_snapshot(stream_key(payload), payload)
    assert outcome.status == "accepted"
    return service, payload


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repositories(request, tmp_path: Path) -> CanonicalRepositories:
    if request.param == "memory":
        return create_canonical_repositories("memory")
    return create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )


@pytest.mark.asyncio
async def test_pipeline_consumes_both_canonical_backends_idempotently(
    repositories: CanonicalRepositories,
) -> None:
    _, payload = await seed(repositories, "creator-alpha")
    pipeline = AnalyticsPipeline(repositories.ingestion)

    first = pipeline.project_account(payload.creator_account_id)
    second = pipeline.project_account(payload.creator_account_id)
    projection = first.artifact.projection

    assert first.changed is True
    assert second.changed is False
    assert second.artifact == first.artifact
    assert projection.source_revision == 1
    assert projection.creator_metrics.conversation_count == 3
    assert projection.creator_metrics.participant_count == 2
    assert projection.creator_metrics.message_count == 7
    assert projection.creator_metrics.response_opportunity_count == 4
    assert projection.creator_metrics.responded_count == 3
    assert projection.creator_metrics.average_response_seconds == 220.0
    assert projection.graph.node_count == 34
    assert projection.graph.edge_count == 63
    assert projection.graph.node_counts_by_kind[GraphNodeKind.MESSAGE.value] == 7
    assert projection.graph.edge_counts_by_relation[GraphRelation.PRECEDES.value] == 5


@pytest.mark.asyncio
async def test_pipeline_is_revision_aware_and_forced_rebuild_is_equivalent(
    repositories: CanonicalRepositories,
) -> None:
    service, payload = await seed(repositories, "creator-alpha")
    pipeline = AnalyticsPipeline(repositories.ingestion)
    initial = pipeline.project_account(payload.creator_account_id)
    delta_document = {
        "connection_id": str(payload.connection_id),
        "fencing_token": payload.fencing_token,
        "creator_account_id": payload.creator_account_id,
        "agent_installation_id": str(payload.agent_installation_id),
        "event_id": "51000000-0000-4000-8000-000000000001",
        "agent_stream_id": str(payload.agent_stream_id),
        "source_seq": payload.through_seq + 1,
        "change": {
            "type": "message.upsert",
            "message": {
                "message_id": "alpha-message-8",
                "chat_id": "alpha-conversation-2",
                "sender_platform_user_id": "synthetic-participant-b",
                "text": "Thanks, the schedule works.",
                "sent_at": "2026-07-11T10:05:00Z",
                "direction": "inbound",
            },
        },
    }
    delta = IngestDeltaPayload.model_validate_json(json.dumps(delta_document))
    outcome = await service.ingest_delta(stream_key(payload), delta)

    refreshed = pipeline.project_account(payload.creator_account_id)
    rebuilt = pipeline.rebuild_account(payload.creator_account_id)

    assert outcome.status == "accepted"
    assert initial.artifact.projection.source_revision == 1
    assert refreshed.changed is True
    assert refreshed.artifact.projection.source_revision == 2
    assert refreshed.artifact.projection.creator_metrics.message_count == 8
    assert rebuilt.changed is True
    assert rebuilt.artifact == refreshed.artifact


@pytest.mark.asyncio
async def test_synthetic_accounts_remain_isolated_in_metrics_and_graph() -> None:
    repositories = create_canonical_repositories("memory")
    _, alpha = await seed(repositories, "creator-alpha")
    _, beta = await seed(repositories, "creator-beta")
    pipeline = AnalyticsPipeline(repositories.ingestion)

    alpha_artifact = pipeline.project_account(alpha.creator_account_id).artifact
    beta_artifact = pipeline.project_account(beta.creator_account_id).artifact

    assert alpha_artifact.projection.creator_metrics.message_count == 7
    assert beta_artifact.projection.creator_metrics.message_count == 3
    assert {node.partition_key for node in alpha_artifact.nodes} == {
        account_ref(alpha.creator_account_id)
    }
    assert {node.partition_key for node in beta_artifact.nodes} == {
        account_ref(beta.creator_account_id)
    }
    assert {node.node_id for node in alpha_artifact.nodes}.isdisjoint(
        node.node_id for node in beta_artifact.nodes
    )


@pytest.mark.asyncio
async def test_legacy_service_names_are_read_only_canonical_adapters() -> None:
    repositories = create_canonical_repositories("memory")
    _, payload = await seed(repositories, "creator-alpha")
    consumer = CanonicalAnalyticsConsumer(repositories.ingestion)
    client = OnlyFansClient(repositories.ingestion)

    run = consumer.refresh(payload.creator_account_id)
    chats = await client.get_chats(payload.creator_account_id, limit=10)
    messages = await client.get_messages(
        payload.creator_account_id, "alpha-conversation-1", limit=10
    )

    assert run.artifact.projection.source_revision == 1
    assert len(chats) == 3
    assert [str(message.id) for message in messages] == [
        "alpha-message-1",
        "alpha-message-2",
        "alpha-message-3",
    ]
    assert not hasattr(consumer, "_delta_queues")
    assert not hasattr(client, "_cache")


@pytest.mark.asyncio
async def test_rebuild_entry_point_is_stable_across_fresh_sqlite_connections(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    _, payload = await seed(repositories, "creator-beta")
    arguments = argparse.Namespace(
        backend="sqlite",
        canonical_path=database_path,
        account_id=payload.creator_account_id,
        output=None,
    )

    first = rebuild_from_args(arguments)
    second = rebuild_from_args(arguments)

    assert first == second
    document = json.loads(first)
    assert document["projection"]["account_ref"] == account_ref(
        payload.creator_account_id
    )
    assert payload.creator_account_id not in first
    assert document["projection"]["source_revision"] == 1
    assert document["projection"]["creator_metrics"]["message_count"] == 3


def test_pipeline_retries_if_canonical_revision_changes_during_projection() -> None:
    class ChangingSource:
        def __init__(self) -> None:
            self.calls = 0

        def account_read_model(self, creator_account_id: str) -> AccountReadModel:
            assert creator_account_id == "synthetic-creator"
            self.calls += 1
            revision = 1 if self.calls == 1 else 2
            return AccountReadModel(view_revision=revision)

        def account_exists(self, creator_account_id: str) -> bool:
            return creator_account_id == "synthetic-creator"

        def account_revisions(self) -> list[tuple[str, int]]:
            return [("synthetic-creator", 2)]

    run = AnalyticsPipeline(ChangingSource()).project_account("synthetic-creator")

    assert run.attempts == 2
    assert run.artifact.projection.source_revision == 2
