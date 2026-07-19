from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_authenticated_account_session
from app.analytics.opaque_refs import account_ref
from app.main import app
from app.models.auth import AuthenticatedAccountSession
from app.protocol.payloads import IngestSnapshotPayload
from app.services import insights_service
from app.transport import transport_manager
from app.transport.ingestion import StreamKey


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"


def alpha_snapshot() -> IngestSnapshotPayload:
    return IngestSnapshotPayload.model_validate_json(
        (FIXTURES / "creator-alpha.snapshot.json").read_text(encoding="utf-8")
    )


@pytest.fixture(autouse=True)
def reset_runtime():
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()


async def seed_default_runtime() -> IngestSnapshotPayload:
    payload = alpha_snapshot()
    outcome = await transport_manager.ingestion.ingest_snapshot(
        StreamKey(
            payload.creator_account_id,
            payload.agent_installation_id,
            payload.agent_stream_id,
        ),
        payload,
    )
    assert outcome.status == "accepted"
    await insights_service.projection_scheduler().wait(payload.creator_account_id)
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
