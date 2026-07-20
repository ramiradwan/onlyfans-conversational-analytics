from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_authenticated_account_session
from app.analytics.opaque_refs import account_ref
from app.main import app
from app.models.auth import AuthenticatedAccountSession
from app.persistence.history import HistoryRepository, StreamKey
from app.protocol.payloads import (
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)
from app.services import insights_service
from app.transport import transport_manager


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"


@dataclass(frozen=True, slots=True)
class FixtureSnapshot:
    """A fixture's flat chat/message document, ready for chunked canonical seeding."""

    connection_id: UUID
    fencing_token: str
    creator_account_id: str
    agent_installation_id: UUID
    agent_stream_id: UUID
    snapshot_id: UUID
    chats: list[dict]
    messages: list[dict]


def alpha_snapshot() -> FixtureSnapshot:
    document = json.loads(
        (FIXTURES / "creator-alpha.snapshot.json").read_text(encoding="utf-8")
    )
    return FixtureSnapshot(
        connection_id=UUID(document["connection_id"]),
        fencing_token=document["fencing_token"],
        creator_account_id=document["creator_account_id"],
        agent_installation_id=UUID(document["agent_installation_id"]),
        agent_stream_id=UUID(document["agent_stream_id"]),
        snapshot_id=UUID(document["snapshot_id"]),
        chats=document["chats"],
        messages=document["messages"],
    )


def stream_key(payload: FixtureSnapshot) -> StreamKey:
    return StreamKey(
        payload.creator_account_id,
        payload.agent_installation_id,
        payload.agent_stream_id,
    )


def _seed_identity(payload: FixtureSnapshot) -> dict:
    return {
        "connection_id": payload.connection_id,
        "fencing_token": payload.fencing_token,
        "creator_account_id": payload.creator_account_id,
        "agent_installation_id": payload.agent_installation_id,
        "agent_stream_id": payload.agent_stream_id,
        "snapshot_id": payload.snapshot_id,
    }


def seed_canonical_snapshot(history: HistoryRepository, payload: FixtureSnapshot) -> None:
    """Write one fixture snapshot through the signer-v2 chunked canonical path."""
    key = stream_key(payload)
    identity = _seed_identity(payload)
    begin = IngestSnapshotBeginPayload(
        **identity,
        frame_kind="begin",
        through_seq=0,
        chunk_count=2,
        record_counts=SnapshotRecordCounts(
            chats=len(payload.chats),
            messages=len(payload.messages),
            coverage_evidence=0,
        ),
        max_frame_bytes=524288,
    )
    assert history.begin_snapshot(key, begin).status == "accepted"
    chat_chunk = IngestSnapshotChunkPayload(
        **identity,
        frame_kind="chunk",
        chunk_index=0,
        entity_kind="chat",
        records=[
            {
                "tombstone": False,
                "chat": {
                    "record_kind": "full",
                    "chat_id": item["chat_id"],
                    "platform_user_id": item["platform_user_id"],
                    "display_name": item.get("display_name"),
                    "updated_at": item["updated_at"],
                },
            }
            for item in payload.chats
        ],
    )
    assert history.add_snapshot_chunk(key, chat_chunk).status == "accepted"
    message_chunk = IngestSnapshotChunkPayload(
        **identity,
        frame_kind="chunk",
        chunk_index=1,
        entity_kind="message",
        records=[
            {
                "tombstone": False,
                "message": {
                    "message_id": item["message_id"],
                    "chat_id": item["chat_id"],
                    "sender_platform_user_id": item["sender_platform_user_id"],
                    "text": item["text"],
                    "sent_at": item["sent_at"],
                    "direction": item["direction"],
                },
            }
            for item in payload.messages
        ],
    )
    assert history.add_snapshot_chunk(key, message_chunk).status == "accepted"
    commit = IngestSnapshotCommitPayload(**identity, frame_kind="commit", chunk_count=2)
    assert history.commit_snapshot(key, commit).status == "accepted"


@pytest.fixture(autouse=True)
def reset_runtime():
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()


async def seed_default_runtime() -> FixtureSnapshot:
    payload = alpha_snapshot()
    seed_canonical_snapshot(transport_manager.history, payload)
    account = transport_manager.ingestion.account_read_model(
        payload.creator_account_id
    )
    scheduler = insights_service.projection_scheduler()
    await scheduler.schedule(payload.creator_account_id, account.view_revision)
    await scheduler.wait(payload.creator_account_id)
    return payload


def bind_session(account_id: str) -> None:
    app.dependency_overrides[get_authenticated_account_session] = lambda: (
        AuthenticatedAccountSession(
            principal_id="synthetic-principal",
            creator_account_id=account_id,
        )
    )


@pytest.mark.asyncio
async def test_insights_service_derives_real_metrics_from_canonical_state() -> None:
    payload = await seed_default_runtime()

    topics = await insights_service.fetch_topic_metrics(
        None, None, payload.creator_account_id
    )
    sentiment = await insights_service.fetch_sentiment_trend(
        None, None, payload.creator_account_id
    )
    response = await insights_service.fetch_response_time_metrics(
        None, None, payload.creator_account_id
    )
    full = await insights_service.build_analytics_update(
        payload.creator_account_id, None, None
    )

    assert {item.topic for item in topics} >= {"Media", "Pricing", "Support"}
    assert sum(item.volume for item in topics) == 16
    assert len(sentiment.trend) == 3
    assert response.average_handling_time_minutes == pytest.approx(220 / 60)
    assert response.response_coverage == 0.75
    assert full.source_revision == 1
    assert full.creator_metrics is not None
    assert full.creator_metrics.message_count == 7
    assert full.projection_digest.startswith("sha256:")
    assert full.graph is not None and full.graph.node_count == 34


@pytest.mark.asyncio
async def test_canonical_route_is_session_bound_and_legacy_alias_is_removed() -> None:
    payload = await seed_default_runtime()
    bind_session(payload.creator_account_id)
    query = f"creator_account_id={payload.creator_account_id}"

    with TestClient(app) as client:
        canonical = client.get(f"/api/v1/insights/full?{query}")
        compatibility = client.get(
            f"/api/insights/api/v1/insights/full?{query}"
        )
        projection = client.get(f"/api/v1/insights/projection?{query}")

    assert canonical.status_code == 200
    assert compatibility.status_code == 404
    assert canonical.json()["account_ref"] == account_ref(payload.creator_account_id)
    assert canonical.json()["creator_metrics"]["conversation_count"] == 3
    assert projection.status_code == 200
    assert projection.json()["source_revision"] == 1
    assert projection.json()["creator_metrics"]["message_count"] == 7


@pytest.mark.asyncio
async def test_explicit_date_filter_is_applied_to_summary_series() -> None:
    payload = await seed_default_runtime()
    bind_session(payload.creator_account_id)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/insights/full",
            params={
                "creator_account_id": payload.creator_account_id,
                "start_date": "2026-07-12T00:00:00Z",
                "end_date": "2026-07-12T23:59:59Z",
            },
        )
        invalid = client.get(
            "/api/v1/insights/full",
            params={
                "creator_account_id": payload.creator_account_id,
                "start_date": "2026-07-13T00:00:00Z",
                "end_date": "2026-07-12T00:00:00Z",
            },
        )

    assert response.status_code == 200
    document = response.json()
    assert sum(point["message_count"] for point in document["sentiment_trend"]["trend"]) == 2
    assert document["response_time_metrics"]["responded_count"] == 1
    assert invalid.status_code == 422
