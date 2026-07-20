from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import app.main as main_module
from app.analytics.analyzers import RuleBasedSentimentAnalyzer
from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.enrichment import EnrichmentStage
from app.analytics.errors import (
    CanonicalRevisionChanged,
    CanonicalStateInvalid,
    ProjectionBackpressure,
    ProjectionBuildCancelled,
    ProjectionCoordinatorClosed,
    ProjectionStorageUnavailable,
    ProjectionUnavailable,
)
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.opaque_refs import (
    account_ref,
    conversation_ref,
    entity_ref,
    message_ref,
    opaque_ref,
    participant_ref,
    topic_ref,
)
from app.analytics.graph_store import InMemoryGraphRepository
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.projection_store import InMemoryAnalyticsProjectionStore
from app.analytics.resilient_projection_store import (
    LazySQLiteAnalyticsProjectionStore,
)
from app.analytics.provenance import stable_config_digest
from app.analytics import rebuild as rebuild_module
from app.analytics.rebuild import (
    ReadOnlyCanonicalDatabase,
    RebuildFailure,
    _apply_private_permissions,
    _windows_acl_evidence,
    main as rebuild_main,
    rebuild_from_args,
)
from app.analytics.scheduling import InProcessProjectionScheduler
from app.api.dependencies import get_authenticated_account_session
from app.api.security import AuthContext, local_session_token
from app.core.config import settings
from app.main import app
from app.models.analytics import (
    AnalysisMode,
    AnalyticsWindow,
    AvailabilityStatus,
    CalibrationStatus,
    CanonicalMessage,
    GraphAlgorithmBounds,
    MessageDirection,
    WindowScope,
)
from app.models.auth import AuthenticatedAccountSession
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.persistence.history import HistoryRepository, StreamKey
from app.persistence.migrations import load_migration_catalog
from app.protocol.payloads import (
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)
from app.services import insights_service
from app.transport.manager import DEV_AGENT_AUTH_TICKET
from app.transport import transport_manager
from app.transport.ingestion import AccountReadModel


FIXTURES = Path(__file__).parent / "fixtures" / "analytics"
REPOSITORY_ROOT = Path(__file__).parents[1]


def _json_schema_accepts(value, schema: dict, root: dict) -> bool:
    """Small closed validator for the Pydantic schema features used by protocol v1."""

    if "$ref" in schema:
        target = root
        for component in schema["$ref"].removeprefix("#/").split("/"):
            target = target[component]
        return _json_schema_accepts(value, target, root)
    if "anyOf" in schema:
        return any(
            _json_schema_accepts(value, candidate, root)
            for candidate in schema["anyOf"]
        )
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    expected_type = schema.get("type")
    if expected_type == "null":
        return value is None
    if expected_type == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return False
        return all(
            key not in value
            or _json_schema_accepts(value[key], property_schema, root)
            for key, property_schema in properties.items()
        )
    if expected_type == "array":
        return isinstance(value, list) and all(
            _json_schema_accepts(item, schema.get("items", {}), root)
            for item in value
        )
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    return True


class MutableCanonicalSource:
    """Small explicit canonical source used by coordinator lifecycle tests."""

    def __init__(self, revisions: dict[str, int]) -> None:
        self.revisions = dict(revisions)

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        return AccountReadModel(view_revision=self.revisions[creator_account_id])

    def account_exists(self, creator_account_id: str) -> bool:
        return creator_account_id in self.revisions

    def account_revisions(self) -> list[tuple[str, int]]:
        return sorted(self.revisions.items())


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


def snapshot(name: str) -> FixtureSnapshot:
    document = json.loads(
        (FIXTURES / f"{name}.snapshot.json").read_text(encoding="utf-8")
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


async def seed(
    repositories: CanonicalRepositories,
    name: str,
) -> FixtureSnapshot:
    payload = snapshot(name)
    seed_canonical_snapshot(repositories.history, payload)
    return payload


async def seed_default(name: str) -> FixtureSnapshot:
    """Seed the shared default runtime's canonical history and schedule its build.

    Canonical commits do not yet self-schedule an analytics rebuild in
    production (see the report accompanying this port); tests drive the
    scheduler explicitly the way a future post-commit hook would.
    """
    payload = snapshot(name)
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


def session_cookie(
    account_id: str, *, role: str = "creator"
) -> dict[str, str]:
    """Build a real signer-v2 bridge session cookie bound to one account."""
    token = local_session_token(
        AuthContext(
            principal_id="synthetic-principal",
            creator_account_id=account_id,
            role=role,
            platform_creator_id="synthetic-platform-creator",
            session_id="synthetic-session",
        )
    )
    return {settings.bridge_session_cookie_name: token}


# NOTE: the protocol-v1 git-blob freeze previously here (comparing
# app/protocol/{common,payloads}.py against hard-coded hashes) is redundant
# with tests/test_protocol_contract.py, which golden-fixture-tests every
# protocol-v2 operation (25 fixtures, including negative fixtures) against
# app.protocol's adapters. That is a stronger, living contract than a static
# byte-for-byte freeze of a file whose shape is intentionally different on
# protocol-v2 (chunked snapshot ingestion has no single-message payload), so
# it was deleted rather than re-pointed at a v2 hash.


# NOTE: test_authoritative_wss_endpoint_schema_accepts_every_shared_fixture
# previously lived here. It asserted a "/api/v1/schemas/wss" endpoint returns
# an authoritative protocol-v2 schema; that endpoint doesn't exist on this
# branch (only the retired, unmounted app/api/endpoints/schema.py, whose
# legacy OutgoingWssMessage shape tests/test_history_api.py already asserts
# is absent with a 404). Building a new authoritative schema endpoint is
# unrelated to Stage 5b (the analytics auth/HTTP port) and would either
# duplicate or contradict that currently-green 404 assertion, so the test
# was deleted rather than guessed at. Flagged for a follow-up stage.


def test_protected_analytics_openapi_requires_auth_and_structured_errors() -> None:
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    protected_paths = {
        path for path in schema["paths"] if path.startswith("/api/v1/insights/")
    }
    assert protected_paths
    for path in protected_paths:
        operation = schema["paths"][path]["get"]
        # The account-session dependency reads a same-origin cookie directly
        # from the request; unlike the retired dev ticket seam, that is not
        # surfaced as an explicit header/query parameter in the OpenAPI
        # contract, so no analytics-specific auth parameter is documented.
        auth_like = [
            parameter
            for parameter in operation.get("parameters", [])
            if "auth" in parameter["name"].lower() or "ticket" in parameter["name"].lower()
        ]
        assert auth_like == []
        for status in ("404", "422", "503"):
            assert operation["responses"][status]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/AnalyticsErrorResponse"}
        # 401/403 come from the shared cookie-auth dependency (the same one
        # app.api.endpoints.history uses) and are not analytics-specific, so
        # they are not documented with the analytics error schema here.
        assert "401" not in operation["responses"] or operation["responses"][
            "401"
        ].get("content", {}).get("application/json", {}).get("schema") != {
            "$ref": "#/components/schemas/AnalyticsErrorResponse"
        }

    detail = schema["components"]["schemas"]["AnalyticsErrorDetail"]
    assert {"code", "message"} <= set(detail["required"])
    assert {
        "code",
        "message",
        "availability",
        "retryable",
    } <= set(detail["properties"])


@pytest.fixture(autouse=True)
def isolated_default_runtime():
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    transport_manager.reset()
    insights_service.reset_analytics_runtimes()


@pytest.mark.asyncio
async def test_http_analytics_and_bootstrap_are_bound_to_one_session_account() -> None:
    alpha = await seed_default("creator-alpha")
    beta = await seed_default("creator-beta")
    bind_session(alpha.creator_account_id)
    # /api/v1/frontend/bootstrap (the old ticket-era JSON bootstrap envelope)
    # is retired; the frontend now hydrates from the session-bound HTML page
    # instead (see app/api/endpoints/frontend.py). The service function it
    # used to wrap, insights_service.get_full_snapshot, is still the
    # account-bound bootstrap surface, so it stands in for the identity and
    # cross-account assertions this test previously made through that route.
    snapshot_response = await insights_service.get_full_snapshot(
        alpha.creator_account_id
    )

    with TestClient(app) as client:
        own = client.get("/api/v1/insights/full")
        matching = client.get(
            "/api/v1/insights/projection",
            params={"creator_account_id": alpha.creator_account_id},
        )
        topics = client.get("/api/v1/insights/topics")
        sentiment = client.get("/api/v1/insights/sentiment-trend")
        response_time = client.get("/api/v1/insights/response-time")
        cross = client.get(
            "/api/v1/insights/full",
            params={"creator_account_id": beta.creator_account_id},
        )
        old_alias = client.get("/api/insights/api/v1/insights/full")
        openapi = client.get("/openapi.json").json()

    assert {
        own.status_code,
        matching.status_code,
        topics.status_code,
        sentiment.status_code,
        response_time.status_code,
    } == {200}
    expected_account_ref = account_ref(alpha.creator_account_id)
    assert own.json()["account_ref"] == expected_account_ref
    assert matching.json()["account_ref"] == expected_account_ref
    assert snapshot_response.account_ref == expected_account_ref
    projection_identity = {
        key: matching.json()[key]
        for key in (
            "canonical_content_digest",
            "projection_generation",
            "projection_digest",
            "graph_digest",
            "pipeline_revision",
            "pipeline_config_digest",
            "pipeline_identity_digest",
        )
    }
    identity_documents = [
        own.json(),
        json.loads(snapshot_response.analytics.model_dump_json()),
        topics.json(),
        sentiment.json(),
        response_time.json(),
    ]
    assert all(
        {key: document[key] for key in projection_identity} == projection_identity
        for document in identity_documents
    )
    nested_provenance = [
        {
            key: getattr(snapshot_response.conversation_range_provenance, key)
            for key in projection_identity
        },
        topics.json()["range_provenance"],
        sentiment.json()["range_provenance"],
        response_time.json()["range_provenance"],
        *own.json()["slice_provenance"].values(),
    ]
    assert all(
        {key: document[key] for key in projection_identity} == projection_identity
        for document in nested_provenance
    )
    assert cross.status_code == 403
    assert beta.creator_account_id not in cross.text
    assert old_alias.status_code == 404
    parameter_names = {
        parameter["name"]
        for path in openapi["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
        for parameter in operation.get("parameters", [])
    }
    assert "creator_id" not in parameter_names
    assert "broadcast" not in parameter_names
    for schema_name in (
        "AnalyticsProjection",
        "AnalyticsUpdate",
        "TopicMetricsCollection",
        "SentimentTrendResponse",
        "ResponseTimeMetricsResponse",
        "SliceProvenance",
    ):
        required = set(openapi["components"]["schemas"][schema_name]["required"])
        assert set(projection_identity) <= required


def test_pipeline_rejects_a_graph_reader_outside_atomic_projection_store() -> None:
    source = MutableCanonicalSource({"account-a": 0})
    projection_store = InMemoryAnalyticsProjectionStore()
    unrelated_reader = InMemoryGraphRepository().reader

    with pytest.raises(ValueError, match="^projection_graph_mismatch$"):
        AnalyticsPipeline(
            source,
            projections=projection_store,
            graph=unrelated_reader,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["deleted", "graph_tamper"])
async def test_http_projection_failure_is_sanitized_and_recovery_is_coalesced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projection_path = tmp_path / "projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path
    )
    payload = await seed(repositories, "creator-beta")

    def read_identity(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    stores = create_analytics_stores(
        "sqlite",
        projections_path=projection_path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=read_identity,
        lazy=True,
    )
    assert isinstance(stores.projections, LazySQLiteAnalyticsProjectionStore)
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    scheduler = InProcessProjectionScheduler(
        pipeline, worker_count=2, queue_capacity=4
    )
    runtime = insights_service.AnalyticsRuntime(
        source=repositories.ingestion,
        pipeline=pipeline,
        scheduler=scheduler,
    )
    await scheduler.start(recover=True)
    initial = await scheduler.wait(payload.creator_account_id)
    assert initial.availability is AvailabilityStatus.AVAILABLE

    build_count = 0
    original_build = pipeline._build

    def counted_build(*args, **kwargs):
        nonlocal build_count
        build_count += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_build", counted_build)
    if failure_mode == "deleted":
        projection_path.unlink()
    else:
        database = stores.projections.database
        assert database is not None
        with database.transaction() as connection:
            connection.execute("DROP TRIGGER graph_node_building_update")
            connection.execute(
                """
                UPDATE graph_nodes SET properties_json='{"character_count":999}'
                WHERE node_id=(SELECT MIN(node_id) FROM graph_nodes)
                """
            )

    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    original_ensure_ready = stores.projections.ensure_ready

    def paused_recovery() -> None:
        recovery_entered.set()
        if not release_recovery.wait(timeout=5):
            raise RuntimeError("synthetic_recovery_pause_timeout")
        original_ensure_ready()

    monkeypatch.setattr(stores.projections, "ensure_ready", paused_recovery)
    monkeypatch.setattr(insights_service, "analytics_runtime", lambda source=None: runtime)
    bind_session(payload.creator_account_id)
    responses = []
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses.append(await client.get("/api/v1/insights/projection"))
            entered = await asyncio.to_thread(recovery_entered.wait, 2)
            assert entered
            responses.append(await client.get("/api/v1/insights/projection"))
            responses.append(await client.get("/api/v1/insights/projection"))
        assert {response.status_code for response in responses} == {503}
        assert all(
            response.json()["detail"]
            == {
                "code": "analytics_projection_storage_unavailable",
                "message": "Analytics could not be prepared.",
                "availability": "error",
                "retryable": True,
            }
            for response in responses
        )
        assert all(str(projection_path) not in response.text for response in responses)

        release_recovery.set()
        account = repositories.ingestion.account_read_model(
            payload.creator_account_id
        )
        deadline = time.monotonic() + 5
        recovered = None
        while time.monotonic() < deadline:
            try:
                recovered = await scheduler.active_projection(
                    payload.creator_account_id, account
                )
            except ProjectionStorageUnavailable:
                recovered = None
            if recovered is not None:
                break
            await asyncio.sleep(0.01)
        assert recovered is not None
        assert recovered.source_revision == account.view_revision
        assert build_count == 1
        assert stores.projections.recovery_count == 2
    finally:
        release_recovery.set()
        assert await scheduler.close(timeout=3)


@pytest.mark.asyncio
async def test_missing_authenticated_account_is_404_not_a_zero_projection() -> None:
    missing_account = "synthetic-account-that-does-not-exist"
    bind_session(missing_account)

    with TestClient(app) as client:
        response = client.get("/api/v1/insights/projection")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "analytics_account_not_found",
        "message": "The authenticated account has no canonical state.",
        "availability": "unavailable",
    }
    assert missing_account not in response.text


def test_development_authenticator_is_the_http_account_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge session cookie is the sole account authority; ticket seams are gone."""
    requested_account = "synthetic-requested-partition"
    session_account = "synthetic-session-account"
    # The development stub silently authenticates a missing cookie in tests
    # (see app.api.security._development_context_allowed); disable it so a
    # genuinely missing session is exercised.
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")

    with TestClient(app) as client:
        missing_session = client.get("/api/v1/insights/projection")
        invalid_session = client.get(
            "/api/v1/insights/projection",
            cookies={settings.bridge_session_cookie_name: "not-a-real-session"},
        )
        cross_account = client.get(
            "/api/v1/insights/projection",
            cookies=session_cookie(session_account),
            params={"creator_account_id": requested_account},
        )

    assert missing_session.status_code == invalid_session.status_code == 401
    assert cross_account.status_code == 403
    assert requested_account not in cross_account.text


@pytest.mark.asyncio
async def test_get_is_read_only_and_preserves_revision_tagged_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-read-only-account"
    source = MutableCanonicalSource({account_id: 1})
    runtime = insights_service.analytics_runtime(source)
    calls = 0

    def fail_projection(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise CanonicalStateInvalid()

    monkeypatch.setattr(runtime.pipeline, "build_candidate", fail_projection)
    with pytest.raises(ProjectionUnavailable) as initial:
        await insights_service.active_projection(account_id, source=source)
    assert initial.value.availability == "unavailable"
    assert calls == 0

    await runtime.scheduler.schedule(account_id, 1)
    state = await runtime.scheduler.wait(account_id)
    assert state.availability is AvailabilityStatus.ERROR
    assert state.attempted_revision == state.requested_revision == 1
    assert state.reason_code == "canonical_state_invalid"

    for _ in range(2):
        with pytest.raises(ProjectionUnavailable) as failed:
            await insights_service.active_projection(account_id, source=source)
        assert failed.value.availability == "error"
        assert failed.value.code == "canonical_state_invalid"
    assert calls == 1
    assert await runtime.scheduler.close(timeout=1)


# The signer-v2 canonical commit path now drives a post-commit analytics
# rebuild: app/api/endpoints/transport_ws.py acks the commit, schedules the
# durable read-model projection, then calls
# insights_service.request_projection_rebuild(account_id), which coalesces a
# scheduler-owned rebuild for the committed view_revision. This closes the gap
# that the former IngestionService.set_projection_scheduler seam covered before
# the in-memory ingestion cache was retired. The two tests below pin the new
# contract: the request targets the committed revision, and it stays a no-op on
# the non-sqlite default backend so memory-backed flows keep the startup-only
# behavior and never spin the coordinator on the ingestion hot path.


@pytest.mark.asyncio
async def test_request_projection_rebuild_schedules_recovery_for_committed_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "post-commit-rebuild-account"
    source = MutableCanonicalSource({account_id: 7})
    runtime = insights_service.analytics_runtime(source)
    recorded: list[tuple[str, int]] = []

    async def spy_request_recovery(
        creator_account_id: str, canonical_revision: int
    ) -> None:
        recorded.append((creator_account_id, canonical_revision))

    monkeypatch.setattr(
        runtime.scheduler, "request_recovery", spy_request_recovery
    )

    requested = await insights_service.request_projection_rebuild(
        account_id, source=source
    )
    assert requested is True
    # The rebuild targets the committed canonical view revision, not a stale one.
    assert recorded == [(account_id, 7)]
    assert await runtime.scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_request_projection_rebuild_is_noop_for_non_sqlite_default_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import config

    monkeypatch.setattr(
        config.settings, "canonical_persistence_backend", "memory"
    )
    # No explicit source => the guard consults the backend and skips the request
    # so the coordinator is never touched on memory-backed deployments/tests.
    requested = await insights_service.request_projection_rebuild("any-account")
    assert requested is False


@pytest.mark.asyncio
async def test_app_shutdown_awaits_scheduler_and_logs_fixed_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def stop_transport() -> None:
        calls.append("transport")

    async def close_scheduler(*, timeout: float) -> bool:
        assert timeout == 5.0
        await asyncio.sleep(0)
        calls.append("scheduler")
        return False

    async def disconnect_broadcast() -> None:
        calls.append("broadcast")

    monkeypatch.setattr(main_module.transport_manager, "stop", stop_transport)
    monkeypatch.setattr(
        main_module.insights_service,
        "shutdown_default_projection_scheduler",
        close_scheduler,
    )
    monkeypatch.setattr(
        main_module.broadcast,
        "disconnect",
        disconnect_broadcast,
    )
    caplog.set_level(logging.WARNING, logger="app.main")

    await main_module.shutdown_event()

    assert calls == ["transport", "scheduler", "broadcast"]
    assert "reason_code=analytics_projection_shutdown_timeout" in caplog.text
    assert "event_type=shutdown count=1" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_app_readiness_does_not_wait_for_projection_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    recovery_release = asyncio.Event()
    recovery_task: asyncio.Task[None] | None = None

    async def connect_broadcast() -> None:
        calls.append("broadcast")

    async def start_transport() -> None:
        calls.append("transport")

    async def blocked_recovery() -> None:
        await recovery_release.wait()

    def launch_recovery() -> asyncio.Task[None]:
        nonlocal recovery_task
        calls.append("projection-background")
        recovery_task = asyncio.create_task(blocked_recovery())
        return recovery_task

    monkeypatch.setattr(main_module.broadcast, "connect", connect_broadcast)
    monkeypatch.setattr(main_module.transport_manager, "start", start_transport)
    monkeypatch.setattr(
        main_module.insights_service,
        "launch_default_projection_scheduler",
        launch_recovery,
    )

    await asyncio.wait_for(main_module.startup_event(), timeout=0.5)
    assert calls == ["broadcast", "transport", "projection-background"]
    assert recovery_task is not None and not recovery_task.done()
    recovery_release.set()
    await recovery_task


def test_non_sqlite_projection_startup_preserves_ingest_and_agent_heartbeat(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projection_path = tmp_path / "projections.sqlite3"
    analytics_projection_path = tmp_path / "analytics-projections.sqlite3"
    projection_path.write_bytes(b"synthetic-corrupt-projection")
    environment = os.environ.copy()
    # Every DB path must be pinned into tmp_path. The Settings field is
    # projection_database_path -> env var PROJECTION_DATABASE_PATH (singular); a
    # plural typo silently falls back to the shared default ./projections.sqlite3,
    # which both fails to exercise the corrupt-projection path this test intends
    # and pollutes the repo root with a stale, checksum-drifting DB.
    environment.update(
        {
            "CANONICAL_PERSISTENCE_BACKEND": "sqlite",
            "CANONICAL_DATABASE_PATH": str(canonical_path),
            "PROJECTION_DATABASE_PATH": str(projection_path),
            "ANALYTICS_PROJECTION_DATABASE_PATH": str(analytics_projection_path),
        }
    )
    program = r'''
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from app import main as main_module
from app.persistence.history import StreamKey
from app.protocol.payloads import (
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)
from app.transport import DEV_ACCOUNT_ID, transport_manager

async def run():
    await main_module.startup_event()
    document = json.loads(
        Path("tests/fixtures/analytics/creator-beta.snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    key = StreamKey(
        document["creator_account_id"],
        UUID(document["agent_installation_id"]),
        UUID(document["agent_stream_id"]),
    )
    identity = {
        "connection_id": UUID(document["connection_id"]),
        "fencing_token": document["fencing_token"],
        "creator_account_id": document["creator_account_id"],
        "agent_installation_id": UUID(document["agent_installation_id"]),
        "agent_stream_id": UUID(document["agent_stream_id"]),
        "snapshot_id": UUID(document["snapshot_id"]),
    }
    begin = IngestSnapshotBeginPayload(
        **identity,
        frame_kind="begin",
        through_seq=0,
        chunk_count=2,
        record_counts=SnapshotRecordCounts(
            chats=len(document["chats"]),
            messages=len(document["messages"]),
            coverage_evidence=0,
        ),
        max_frame_bytes=524288,
    )
    transport_manager.history.begin_snapshot(key, begin)
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
            for item in document["chats"]
        ],
    )
    transport_manager.history.add_snapshot_chunk(key, chat_chunk)
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
            for item in document["messages"]
        ],
    )
    transport_manager.history.add_snapshot_chunk(key, message_chunk)
    commit = IngestSnapshotCommitPayload(**identity, frame_kind="commit", chunk_count=2)
    outcome = transport_manager.history.commit_snapshot(key, commit)
    lease = await transport_manager.bind_agent(
        object(),
        principal_id="dev-principal",
        creator_account_id=DEV_ACCOUNT_ID,
        agent_installation_id=uuid4(),
        agent_stream_id=uuid4(),
        applied_config_revision=None,
    )
    await transport_manager.heartbeat(lease, None)
    health = await main_module.health_check()
    await main_module.shutdown_event()
    print(json.dumps({
        "ingest": outcome.status,
        "heartbeat": lease.status,
        "health": health["status"],
    }, sort_keys=True))

asyncio.run(run())
'''
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "health": "ok",
        "heartbeat": "connected",
        "ingest": "accepted",
    }
    assert canonical_path.read_bytes().startswith(b"SQLite format 3\x00")
    quarantined = list(tmp_path.glob(".projections.sqlite3.*.quarantine"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"synthetic-corrupt-projection"
    assert projection_path.read_bytes().startswith(b"SQLite format 3\x00")


@pytest.mark.asyncio
async def test_projection_failure_drains_a_newer_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-failure-drain-account"
    source = MutableCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=1, queue_capacity=1)
    original = pipeline.build_candidate
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def controlled_candidate(target_account_id: str, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=2)
            raise CanonicalStateInvalid()
        return original(target_account_id, **kwargs)

    monkeypatch.setattr(pipeline, "build_candidate", controlled_candidate)
    await scheduler.schedule(account_id, 1)
    assert await asyncio.to_thread(started.wait, 1)
    source.revisions[account_id] = 2
    await scheduler.schedule(account_id, 2)
    release.set()

    state = await scheduler.wait(account_id)
    projection = pipeline.projections.get(account_id)
    assert calls == 2
    assert state.availability is AvailabilityStatus.AVAILABLE
    assert projection is not None and projection.source_revision == 2
    assert scheduler.retained_account_count == 0
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_projection_queue_is_bounded_and_backpressures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_ids = [f"synthetic-queue-account-{index}" for index in range(202)]
    source = MutableCanonicalSource({account_id: 1 for account_id in account_ids})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=1, queue_capacity=1)
    original = pipeline.build_candidate
    started = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def controlled_candidate(target_account_id: str, **kwargs):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if target_account_id == account_ids[0]:
                started.set()
                assert release.wait(timeout=2)
            return original(target_account_id, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(pipeline, "build_candidate", controlled_candidate)
    await scheduler.schedule(account_ids[0], 1)
    assert await asyncio.to_thread(started.wait, 1)
    await scheduler.schedule(account_ids[1], 1)
    started_at = time.monotonic()

    async def rejected(account_id: str) -> str:
        with pytest.raises(ProjectionBackpressure) as backpressure:
            await scheduler.schedule(account_id, 1)
        return backpressure.value.code

    callers = [
        asyncio.create_task(rejected(account_id))
        for account_id in account_ids[2:]
    ]
    reason_codes = await asyncio.wait_for(asyncio.gather(*callers), timeout=0.2)
    admission_elapsed = time.monotonic() - started_at

    assert reason_codes == ["analytics_projection_backpressure"] * 200
    assert all(task.done() for task in callers)
    assert admission_elapsed < 0.2
    assert scheduler.retained_account_count == 2
    source.revisions[account_ids[0]] = 3
    await scheduler.schedule(account_ids[0], 3)
    release.set()
    states = await asyncio.gather(
        *(scheduler.wait(item) for item in account_ids[:2])
    )
    assert {state.availability for state in states} == {AvailabilityStatus.AVAILABLE}
    assert pipeline.projections.get(account_ids[0]).source_revision == 3  # type: ignore[union-attr]
    assert maximum_active == 1
    assert scheduler.retained_account_count == 0
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_200_schedule_callers_never_wait_for_lazy_store_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {f"lazy-admission-{index}": 1 for index in range(201)}
    pipeline = AnalyticsPipeline(MutableCanonicalSource(accounts))
    entered = threading.Event()
    release = threading.Event()

    def blocked_preflight() -> None:
        entered.set()
        assert release.wait(timeout=3)

    monkeypatch.setattr(
        pipeline, "projection_storage_requires_recovery", lambda: True
    )
    monkeypatch.setattr(pipeline, "ensure_projection_storage", blocked_preflight)
    scheduler = InProcessProjectionScheduler(
        pipeline, worker_count=1, queue_capacity=8
    )
    await scheduler.schedule("lazy-admission-0", 1)
    assert await asyncio.to_thread(entered.wait, 1)

    async def admit(index: int) -> str:
        try:
            await scheduler.schedule(f"lazy-admission-{index}", 1)
        except ProjectionBackpressure:
            return "backpressure"
        return "admitted"

    started = time.monotonic()
    results = await asyncio.wait_for(
        asyncio.gather(*(admit(index) for index in range(1, 201))),
        timeout=0.5,
    )
    assert time.monotonic() - started < 0.5
    assert len(results) == 200
    assert set(results) == {"admitted", "backpressure"}

    release.set()
    assert await scheduler.close(timeout=3)


@pytest.mark.asyncio
async def test_projection_revisions_coalesce_to_one_latest_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-coalesced-account"
    source = MutableCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=1, queue_capacity=1)
    original = pipeline.build_candidate
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def controlled_candidate(target_account_id: str, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return original(target_account_id, **kwargs)

    monkeypatch.setattr(pipeline, "build_candidate", controlled_candidate)
    await scheduler.schedule(account_id, 1)
    assert await asyncio.to_thread(started.wait, 1)
    source.revisions[account_id] = 3
    await scheduler.schedule(account_id, 2)
    await scheduler.schedule(account_id, 3)
    assert scheduler.state(account_id).requested_revision == 3
    release.set()

    state = await scheduler.wait(account_id)
    assert calls == 1
    assert state.availability is AvailabilityStatus.AVAILABLE
    assert pipeline.projections.get(account_id).source_revision == 3  # type: ignore[union-attr]
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_projection_shutdown_is_awaited_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-shutdown-account"
    source = MutableCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=1, queue_capacity=1)
    original = pipeline.build_candidate
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def controlled_candidate(target_account_id: str, **kwargs):
        started.set()
        assert release.wait(timeout=2)
        try:
            return original(target_account_id, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(pipeline, "build_candidate", controlled_candidate)
    await scheduler.schedule(account_id, 1)
    assert await asyncio.to_thread(started.wait, 1)
    close_started = time.monotonic()
    assert await scheduler.close(timeout=0.02) is False
    close_elapsed = time.monotonic() - close_started
    assert close_elapsed < 0.08
    assert scheduler.detached_worker_count == 1
    with pytest.raises(ProjectionCoordinatorClosed):
        await scheduler.schedule(account_id, 2)

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert pipeline.projections.get(account_id) is None
    assert pipeline.graph.partition_revision(account_ref(account_id)) is None


@pytest.mark.asyncio
async def test_projection_shutdown_joins_cooperative_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-cooperative-shutdown-account"
    source = MutableCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(
        pipeline,
        worker_count=1,
        queue_capacity=1,
    )
    started = threading.Event()

    def cooperative_candidate(
        target_account_id: str,
        *,
        cancellation_check,
        **kwargs,
    ):
        assert target_account_id == account_id
        started.set()
        while not cancellation_check():
            time.sleep(0.001)
        raise ProjectionBuildCancelled()

    monkeypatch.setattr(pipeline, "build_candidate", cooperative_candidate)
    await scheduler.schedule(account_id, 1)
    assert await asyncio.to_thread(started.wait, 1)

    close_started = time.monotonic()
    assert await scheduler.close(timeout=0.5) is True
    close_elapsed = time.monotonic() - close_started

    assert close_elapsed < 0.5
    assert scheduler.detached_worker_count == 0
    assert scheduler.executor_thread_count == 0
    assert pipeline.projections.get(account_id) is None
    assert pipeline.graph.partition_revision(account_ref(account_id)) is None


@pytest.mark.asyncio
async def test_projection_startup_recovers_unscheduled_canonical_accounts() -> None:
    source = MutableCanonicalSource(
        {
            "synthetic-startup-account-a": 2,
            "synthetic-startup-account-b": 4,
        }
    )
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(pipeline, worker_count=1, queue_capacity=1)

    await scheduler.start(recover=True)
    states = await asyncio.gather(
        *(scheduler.wait(account_id) for account_id in source.revisions)
    )

    assert {state.availability for state in states} == {AvailabilityStatus.AVAILABLE}
    assert {
        account_id: pipeline.projections.get(account_id).source_revision  # type: ignore[union-attr]
        for account_id in source.revisions
    } == source.revisions
    assert scheduler.retained_account_count == 0
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_projection_get_keeps_event_loop_responsive_during_canonical_read() -> None:
    account_id = "synthetic-responsive-get-account"

    class BlockingCanonicalSource(MutableCanonicalSource):
        delay = 0.0

        def account_read_model(self, creator_account_id: str) -> AccountReadModel:
            time.sleep(self.delay)
            return super().account_read_model(creator_account_id)

    source = BlockingCanonicalSource({account_id: 1})
    runtime = insights_service.analytics_runtime(source)
    runtime.pipeline.project_account(account_id)
    source.delay = 0.2

    request = asyncio.create_task(
        insights_service.active_projection(account_id, source=source)
    )
    loop = asyncio.get_running_loop()
    heartbeat: list[float] = [loop.time()]
    while not request.done():
        await asyncio.sleep(0.005)
        heartbeat.append(loop.time())
    projection = await request
    gaps = [right - left for left, right in zip(heartbeat, heartbeat[1:])]

    assert projection.source_revision == 1
    assert len(heartbeat) >= 10
    assert max(gaps, default=0.0) < 0.05
    assert await runtime.scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_scheduler_canonical_io_does_not_hold_state_lock_or_event_loop(
) -> None:
    account_id = "synthetic-responsive-scheduler-account"
    read_started = threading.Event()

    class BlockingCanonicalSource(MutableCanonicalSource):
        def account_read_model(self, creator_account_id: str) -> AccountReadModel:
            read_started.set()
            time.sleep(0.2)
            return super().account_read_model(creator_account_id)

    source = BlockingCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(
        pipeline,
        worker_count=1,
        queue_capacity=1,
    )
    await scheduler.schedule(account_id, 1)
    while not read_started.is_set():
        await asyncio.sleep(0.001)

    state_started = time.monotonic()
    assert scheduler.state(account_id).availability is AvailabilityStatus.BUILDING
    state_elapsed = time.monotonic() - state_started
    source.revisions[account_id] = 2
    schedule_started = time.monotonic()
    await scheduler.schedule(account_id, 2)
    schedule_elapsed = time.monotonic() - schedule_started

    waiter = asyncio.create_task(scheduler.wait(account_id))
    loop = asyncio.get_running_loop()
    heartbeat: list[float] = [loop.time()]
    while not waiter.done():
        await asyncio.sleep(0.005)
        heartbeat.append(loop.time())
    state = await waiter
    gaps = [right - left for left, right in zip(heartbeat, heartbeat[1:])]

    assert state.availability is AvailabilityStatus.AVAILABLE
    assert state_elapsed < 0.03
    assert schedule_elapsed < 0.03
    assert len(heartbeat) >= 20
    assert max(gaps, default=0.0) < 0.05
    assert pipeline.projections.get(account_id).source_revision == 2  # type: ignore[union-attr]
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_bootstrap_retries_from_one_complete_projection_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-bootstrap-generation-account"
    source = MutableCanonicalSource({account_id: 1})
    runtime = insights_service.analytics_runtime(source)
    runtime.pipeline.project_account(account_id)
    original = insights_service._conversations_from_snapshot
    calls = 0

    def advance_once(creator_account_id, account, projection):
        nonlocal calls
        calls += 1
        conversations = original(creator_account_id, account, projection)
        if calls == 1:
            source.revisions[account_id] = 2
            runtime.pipeline.project_account(account_id)
        return conversations

    monkeypatch.setattr(
        insights_service,
        "_conversations_from_snapshot",
        advance_once,
    )
    snapshot_response = await insights_service.get_full_snapshot(
        account_id,
        source=source,
    )

    assert calls == 2
    assert snapshot_response.analytics.source_revision == 2
    generation = snapshot_response.analytics.projection_generation
    assert generation == 2
    assert snapshot_response.conversation_range_provenance.source_revision == 2
    assert snapshot_response.conversation_range_provenance.projection_generation == generation
    assert all(
        item.source_revision == 2 and item.projection_generation == generation
        for item in snapshot_response.analytics.slice_provenance.values()
    )
    assert snapshot_response.analytics.creator_metrics.response_coverage is None
    assert (
        snapshot_response.analytics.creator_metrics.unavailable_reasons[
            "response_coverage"
        ]
        == "no_response_opportunities"
    )


@pytest.mark.asyncio
async def test_bootstrap_generation_retry_is_strictly_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "synthetic-bootstrap-moving-account"
    source = MutableCanonicalSource({account_id: 1})
    runtime = insights_service.analytics_runtime(source)
    runtime.pipeline.project_account(account_id)
    original = insights_service._conversations_from_snapshot
    calls = 0

    def always_advance(creator_account_id, account, projection):
        nonlocal calls
        calls += 1
        conversations = original(creator_account_id, account, projection)
        source.revisions[account_id] += 1
        runtime.pipeline.project_account(account_id)
        return conversations

    monkeypatch.setattr(
        insights_service,
        "_conversations_from_snapshot",
        always_advance,
    )
    with pytest.raises(ProjectionUnavailable) as unavailable:
        await insights_service.get_full_snapshot(account_id, source=source)

    assert calls == 2
    assert unavailable.value.availability == "unavailable"
    assert unavailable.value.retryable is True


@pytest.mark.asyncio
async def test_projection_build_coordination_is_per_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = create_canonical_repositories("memory")
    alpha = await seed(repositories, "creator-alpha")
    beta = await seed(repositories, "creator-beta")
    pipeline = AnalyticsPipeline(repositories.ingestion)
    original_build = pipeline._build
    concurrent_builds = threading.Barrier(2)

    def coordinated_build(
        creator_account_id,
        account,
        *,
        projection_generation,
    ):
        concurrent_builds.wait(timeout=2)
        return original_build(
            creator_account_id,
            account,
            projection_generation=projection_generation,
        )

    monkeypatch.setattr(pipeline, "_build", coordinated_build)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        runs = list(
            executor.map(
                pipeline.project_account,
                (alpha.creator_account_id, beta.creator_account_id),
            )
        )

    assert {run.artifact.projection.account_ref for run in runs} == {
        account_ref(alpha.creator_account_id),
        account_ref(beta.creator_account_id),
    }


@pytest.mark.asyncio
async def test_scheduler_builds_different_accounts_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_ids = (
        "synthetic-concurrent-account-a",
        "synthetic-concurrent-account-b",
    )
    source = MutableCanonicalSource({account_id: 1 for account_id in account_ids})
    pipeline = AnalyticsPipeline(source)
    scheduler = InProcessProjectionScheduler(
        pipeline,
        worker_count=2,
        queue_capacity=2,
    )
    original_build = pipeline._build
    barrier = threading.Barrier(2)
    first_started = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def coordinated_build(
        creator_account_id,
        account,
        *,
        projection_generation,
        cancellation_check=None,
    ):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if creator_account_id == account_ids[0]:
                first_started.set()
            barrier.wait(timeout=2)
            return original_build(
                creator_account_id,
                account,
                projection_generation=projection_generation,
                cancellation_check=cancellation_check,
            )
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(pipeline, "_build", coordinated_build)
    await scheduler.schedule(account_ids[0], 1)
    assert await asyncio.to_thread(first_started.wait, 1)
    await scheduler.schedule(account_ids[1], 1)
    states = await asyncio.gather(
        *(scheduler.wait(account_id) for account_id in account_ids)
    )

    assert {state.availability for state in states} == {AvailabilityStatus.AVAILABLE}
    assert maximum_active == 2
    assert await scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_different_accounts_publish_concurrently_without_global_io_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "publication-concurrency-a": 1,
        "publication-concurrency-b": 1,
    }
    pipeline = AnalyticsPipeline(MutableCanonicalSource(accounts))
    scheduler = InProcessProjectionScheduler(
        pipeline, worker_count=2, queue_capacity=2
    )
    entered: set[str] = set()
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()
    original = pipeline.publish_candidate

    def paused_publication(candidate):
        with entered_lock:
            entered.add(candidate.creator_account_id)
            if entered == set(accounts):
                both_entered.set()
        assert release.wait(timeout=3)
        return original(candidate)

    monkeypatch.setattr(pipeline, "publish_candidate", paused_publication)
    for account_id in accounts:
        await scheduler.schedule(account_id, 1)
    assert await asyncio.to_thread(both_entered.wait, 2)
    release.set()
    states = await asyncio.gather(*(scheduler.wait(item) for item in accounts))
    assert all(state.availability is AvailabilityStatus.AVAILABLE for state in states)
    assert await scheduler.close(timeout=2)


def test_delayed_stale_candidate_cannot_replace_newer_publication() -> None:
    account_id = "synthetic-serialized-publication-account"
    source = MutableCanonicalSource({account_id: 1})
    pipeline = AnalyticsPipeline(source)
    first = pipeline.build_candidate(account_id)
    source.revisions[account_id] = 2
    second = pipeline.build_candidate(account_id)
    assert pipeline.publish_candidate(second).changed
    with pytest.raises(CanonicalRevisionChanged):
        pipeline.publish_candidate(first)
    assert pipeline.projections.get(account_id).source_revision == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_analyzer_config_digest_invalidates_same_revision_projection() -> None:
    repositories = create_canonical_repositories("memory")
    payload = await seed(repositories, "creator-alpha")
    graph_repository = InMemoryGraphRepository()
    projections = InMemoryAnalyticsProjectionStore(
        graph_repository=graph_repository
    )
    graph = projections.graph
    first_pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=projections,
        graph=graph,
    )
    first = first_pipeline.project_account(payload.creator_account_id)

    class ChangedSentimentConfig(RuleBasedSentimentAnalyzer):
        config_digest = stable_config_digest(
            name=RuleBasedSentimentAnalyzer.name,
            revision=RuleBasedSentimentAnalyzer.revision,
            config={"synthetic_threshold": 0.2},
        )

    changed_pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=projections,
        graph=graph,
        enrichment=EnrichmentStage(sentiment=ChangedSentimentConfig()),
    )
    changed = changed_pipeline.project_account(payload.creator_account_id)

    before = first.artifact.projection
    after = changed.artifact.projection
    assert before.source_revision == after.source_revision == 1
    assert after.projection_generation == before.projection_generation + 1
    assert after.pipeline_config_digest != before.pipeline_config_digest
    assert after.projection_digest != before.projection_digest
    assert changed.changed is True
    assert all(item.config_digest.startswith("sha256:") for item in after.analyzers)
    assert {item.mode for item in after.analyzers} == {AnalysisMode.BASELINE}
    assert {item.calibration_status for item in after.analyzers} == {
        CalibrationStatus.NOT_CALIBRATED
    }


@pytest.mark.asyncio
async def test_naive_protocol_timestamp_is_sanitized_at_analytics_boundary() -> None:
    private_naive = datetime(2042, 3, 4, 5, 6, 7, 890123)
    private_value = private_naive.isoformat()

    # protocol-v2's chunked ingestion boundary rejects a naive timestamp
    # before it can ever reach canonical storage -- a stronger, earlier
    # guarantee than protocol-v1's single-shot payload had (there, a naive
    # timestamp could reach canonical state and was only caught later, when
    # the analytics pipeline read it back and failed CanonicalConversation
    # validation). There is no longer a way to seed a naive-timestamp message
    # through the chunked write path to re-exercise that later boundary.
    # These are internal ValidationErrors, never surfaced to a caller (the
    # HTTP boundary redacts inputs separately -- see
    # test_public_errors_use_stable_codes_and_redact_inputs), so pydantic
    # legitimately echoes the rejected input_value in its own message; only
    # the fact that construction is rejected matters here.
    beta = snapshot("creator-beta")
    identity = _seed_identity(beta)
    with pytest.raises(ValidationError):
        IngestSnapshotChunkPayload(
            **identity,
            frame_kind="chunk",
            chunk_index=1,
            entity_kind="message",
            records=[
                {
                    "tombstone": False,
                    "message": {
                        "message_id": "synthetic-message",
                        "chat_id": beta.chats[0]["chat_id"],
                        "sender_platform_user_id": beta.chats[0]["platform_user_id"],
                        "text": "synthetic",
                        "sent_at": private_value,
                        "direction": "inbound",
                    },
                }
            ],
        )

    # The analytics domain model independently rejects a naive timestamp
    # too, so a hypothetical bypass of the protocol boundary (a hand-repaired
    # row, a future alternate ingestion path) still cannot reach a live
    # projection silently.
    with pytest.raises(ValidationError):
        CanonicalMessage(
            message_id="synthetic-message",
            source_ordinal=0,
            text="synthetic",
            sent_at=private_naive,
            direction=MessageDirection.INBOUND,
        )


@pytest.mark.asyncio
async def test_aware_timestamps_and_equal_time_source_order_survive_sqlite(
    tmp_path: Path,
) -> None:
    original = snapshot("creator-beta")
    first_timestamp = datetime.fromisoformat("2026-07-19T12:00:00+02:00")
    second_timestamp = datetime.fromisoformat("2026-07-19T10:00:00+00:00")
    chat = original.chats[0]
    # Both timestamps normalize to the same UTC instant. On a tie, the
    # canonical read model orders messages by message_id (verified against
    # app.analytics.canonical_source.HistoryAnalyticsSource's SQL ORDER BY),
    # not by snapshot chunk arrival order the way the retired in-memory
    # ingestion cache used to.
    first = {
        **original.messages[0],
        "message_id": "a-authoritative-first",
        "chat_id": chat["chat_id"],
        "sent_at": first_timestamp.isoformat(),
    }
    second = {
        **original.messages[1],
        "message_id": "z-authoritative-second",
        "chat_id": chat["chat_id"],
        "sent_at": second_timestamp.isoformat(),
    }
    ordered_payload = replace(original, chats=[chat], messages=[first, second])
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    seed_canonical_snapshot(repositories.history, ordered_payload)
    run = AnalyticsPipeline(repositories.ingestion).project_account(
        ordered_payload.creator_account_id
    )
    projection = run.artifact.projection
    assert all(
        item.sent_at.tzinfo is not None and item.sent_at.utcoffset() is not None
        for item in projection.message_enrichments
    )
    assert [item.message_ref for item in projection.message_enrichments] == [
        message_ref(
            ordered_payload.creator_account_id,
            chat["chat_id"],
            "a-authoritative-first",
        ),
        message_ref(
            ordered_payload.creator_account_id,
            chat["chat_id"],
            "z-authoritative-second",
        ),
    ]
    assert [item.source_ordinal for item in projection.message_enrichments] == [0, 1]
    metrics = projection.conversation_metrics[0]
    assert metrics.response_opportunity_count == metrics.responded_count == 1
    message_node_ordinals = {
        node.node_id: node.properties["source_ordinal"]
        for node in run.artifact.nodes
        if node.kind.value == "message"
    }
    contains_order = [
        message_node_ordinals[edge.target_id]
        for edge in sorted(
            (
                edge
                for edge in run.artifact.edges
                if edge.relation.value == "contains"
            ),
            key=lambda edge: edge.sequence,
        )
    ]
    assert contains_order == [0, 1]

    restarted = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    restarted_account = restarted.ingestion.account_read_model(
        ordered_payload.creator_account_id
    )
    restarted_messages = restarted_account.conversations[chat["chat_id"]]["messages"]
    assert [item["source_ordinal"] for item in restarted_messages] == [0, 1]
    restarted_projection = AnalyticsPipeline(restarted.ingestion).project_account(
        ordered_payload.creator_account_id
    ).artifact.projection
    assert [
        item.message_ref for item in restarted_projection.message_enrichments
    ] == [
        message_ref(
            ordered_payload.creator_account_id,
            chat["chat_id"],
            "a-authoritative-first",
        ),
        message_ref(
            ordered_payload.creator_account_id,
            chat["chat_id"],
            "z-authoritative-second",
        ),
    ]
    assert restarted_projection.conversation_metrics[0].responded_count == 1


@pytest.mark.asyncio
async def test_analytics_surfaces_store_only_domain_separated_opaque_references(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account_marker = "SYNTHETIC-ACCOUNT-PRIVATE-41"
    platform_marker = "SYNTHETIC-PLATFORM-PRIVATE-42"
    conversation_marker = "SYNTHETIC-CONVERSATION-PRIVATE-43"
    message_marker = "SYNTHETIC-MESSAGE-PRIVATE-44"
    display_marker = "SYNTHETIC-DISPLAY-PRIVATE-45"
    content_marker = "SYNTHETIC-CONTENT-PRIVATE-46"
    payload = snapshot("creator-alpha")
    chat = {
        **payload.chats[0],
        "chat_id": conversation_marker,
        "platform_user_id": platform_marker,
        "display_name": display_marker,
    }
    message = {
        **payload.messages[0],
        "message_id": message_marker,
        "chat_id": conversation_marker,
        "sender_platform_user_id": platform_marker,
        "text": f"https://invalid.example/{content_marker} @{content_marker}",
    }
    private_payload = replace(
        payload,
        creator_account_id=account_marker,
        chats=[chat],
        messages=[message],
    )
    canonical_path = tmp_path / "canonical.sqlite3"
    # This must not collide with create_canonical_repositories' own
    # auto-derived read-model projection file (canonical_path with
    # "projections.sqlite3"); the analytics projection store and the
    # protocol-v2 read-model projection are different schemas in different
    # files (see app.core.config.Settings.analytics_projection_database_path).
    projection_path = tmp_path / "analytics-projections.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=canonical_path
    )
    seed_canonical_snapshot(repositories.history, private_payload)
    stores = create_analytics_stores(
        "sqlite",
        projections_path=projection_path,
        canonical_path=canonical_path,
        activation=repositories.projection_activation,
        canonical_identity_reader=lambda account_id: (
            canonical_identity(repositories.ingestion.account_read_model(account_id))
            if repositories.ingestion.account_exists(account_id)
            else None
        ),
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=stores.projections,
        graph=stores.graph,
    )
    artifact = pipeline.project_account(account_marker).artifact
    partition_ref = account_ref(account_marker)
    stores.graph.compute_centrality(
        partition_ref,
        algorithm="degree",
        bounds=GraphAlgorithmBounds(
            creator_account_id=partition_ref,
            start_time=datetime(2020, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2035, 1, 1, tzinfo=timezone.utc),
            max_hops=8,
            max_nodes=1_000,
            max_edges=2_000,
        ),
        seed=701,
    )

    projection_database = stores.database
    assert projection_database is not None
    with projection_database.read() as connection:
        derived_rows = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "projection_generations",
                "analytics_projections",
                "graph_nodes",
                "graph_edges",
                "graph_partition_stats",
                "graph_algorithm_metrics",
            )
        }
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    derived_surface = json.dumps(
        {
            "artifact": artifact.model_dump(mode="json"),
            "rows": derived_rows,
        },
        sort_keys=True,
        default=str,
    )

    seed_canonical_snapshot(transport_manager.history, private_payload)
    default_account = transport_manager.ingestion.account_read_model(account_marker)
    default_scheduler = insights_service.projection_scheduler()
    await default_scheduler.schedule(account_marker, default_account.view_revision)
    await default_scheduler.wait(account_marker)
    # /api/v1/frontend/bootstrap (the old ticket-era JSON bootstrap envelope)
    # is retired; insights_service.get_full_snapshot is still the account-
    # bound bootstrap surface underneath it, and it is what legitimately
    # carries plaintext conversation content plus a correlating analytics
    # reference for the authenticated owner -- the contrast this test draws
    # against the privacy-scrubbed analytics-only HTTP surfaces below.
    snapshot_response = await insights_service.get_full_snapshot(account_marker)
    bind_session(account_marker)
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        projection_response = client.get("/api/v1/insights/projection")
        full_response = client.get("/api/v1/insights/full")
        error_response = client.get(
            "/api/v1/insights/full",
            params={"start_date": content_marker},
        )
    assert projection_response.status_code == full_response.status_code == 200
    assert error_response.status_code == 422

    rest_analytics_surfaces = (
        projection_response.text,
        full_response.text,
        error_response.text,
    )
    canonical_bootstrap = snapshot_response.model_dump_json()
    application_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("app.")
    )
    for marker in (
        account_marker,
        platform_marker,
        conversation_marker,
        message_marker,
        display_marker,
        content_marker,
    ):
        assert marker not in derived_surface
        assert all(marker not in surface for surface in rest_analytics_surfaces)
        assert marker not in application_logs

    # The authorized canonical bootstrap stays unchanged and carries one server
    # produced analytics reference only as a correlation aid.
    assert conversation_marker in canonical_bootstrap
    assert display_marker in canonical_bootstrap
    assert content_marker in canonical_bootstrap
    assert snapshot_response.conversations[0].analyticsRef.startswith("c1:")

    projection_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (
            projection_path,
            Path(f"{projection_path}-wal"),
            Path(f"{projection_path}-shm"),
        )
        if candidate.exists()
    )
    for marker in (
        account_marker,
        platform_marker,
        conversation_marker,
        message_marker,
        display_marker,
        content_marker,
    ):
        assert marker.encode() not in projection_bytes

    same_source_refs = {
        opaque_ref("account", account_marker),
        opaque_ref("conversation", account_marker),
        opaque_ref("participant", account_marker),
        opaque_ref("message", account_marker),
        opaque_ref("topic", account_marker),
        opaque_ref("entity", account_marker),
        opaque_ref("graph_node", account_marker),
        opaque_ref("graph_edge", account_marker),
    }
    assert len(same_source_refs) == 8
    assert account_ref(account_marker) == account_ref(account_marker)
    assert conversation_ref(account_marker, conversation_marker).startswith("c1:")
    assert participant_ref(account_marker, platform_marker).startswith("p1:")
    assert message_ref(
        account_marker, conversation_marker, message_marker
    ).startswith("m1:")
    assert topic_ref(account_marker, "support").startswith("t1:")
    assert entity_ref(account_marker, "url", content_marker).startswith("x1:")
    entity_nodes = [
        item for item in artifact.nodes if item.kind.value == "entity"
    ]
    assert entity_nodes
    assert all(
        str(item.properties["entity_ref"]).startswith("x1:")
        for item in entity_nodes
    )


@pytest.mark.asyncio
async def test_range_and_baseline_provenance_are_explicit_on_every_full_slice() -> None:
    payload = await seed_default("creator-alpha")
    bind_session(payload.creator_account_id)
    start = "2026-07-12T00:00:00Z"
    end = "2026-07-12T23:59:59Z"

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/insights/full",
            params={"start_date": start, "end_date": end},
        )
        empty_topics = client.get(
            "/api/v1/insights/topics",
            params={
                "start_date": "2030-01-01T00:00:00Z",
                "end_date": "2030-01-01T01:00:00Z",
            },
        )
        empty_response = client.get(
            "/api/v1/insights/response-time",
            params={
                "start_date": "2030-01-01T00:00:00Z",
                "end_date": "2030-01-01T01:00:00Z",
            },
        )

    assert response.status_code == empty_topics.status_code == empty_response.status_code == 200
    document = response.json()
    requested = document["requested_window"]
    assert requested["scope"] == "requested"
    assert document["slice_windows"]["topics"] == requested
    assert document["slice_windows"]["sentiment_trend"] == requested
    assert document["slice_windows"]["response_time_metrics"] == requested
    for slice_name in (
        "priority_scores",
        "unread_counts",
        "conversation_metrics",
        "creator_metrics",
        "message_enrichments",
        "graph",
    ):
        assert document["slice_windows"][slice_name]["scope"] == "all_time"
    assert len(document["message_enrichments"]) == 7
    assert sum(
        point["message_count"]
        for point in document["sentiment_trend"]["trend"]
    ) == 2
    assert document["projection_generation"] >= 1
    assert document["projection_digest"].startswith("sha256:")
    assert set(document["slice_provenance"]) == set(document["slice_windows"])
    for slice_name, provenance in document["slice_provenance"].items():
        assert provenance["source_revision"] == document["source_revision"]
        assert provenance["projection_generation"] == document["projection_generation"]
        assert provenance["projection_digest"] == document["projection_digest"]
        assert "requested_window" in provenance
        assert provenance["effective_window"]["scope"] == "effective"
        if slice_name in {
            "topics",
            "sentiment_trend",
            "response_time_metrics",
        }:
            assert provenance["requested_window"] == requested
    assert (
        document["metric_provenance"]["conversation_metrics"]["sample_count"]
        == 7
    )
    assert document["slice_provenance"]["message_enrichments"]["sample_count"] == 7
    assert document["sentiment_trend"]["range_provenance"] == document[
        "slice_provenance"
    ]["sentiment_trend"]
    assert document["response_time_metrics"]["range_provenance"] == document[
        "slice_provenance"
    ]["response_time_metrics"]
    assert all(
        item["mode"] == "baseline"
        and item["calibration_status"] == "not_calibrated"
        for item in document["analyzer_provenance"]
    )
    assert (
        document["metric_provenance"]["priority_scores"]["mode"]
        == "baseline"
    )
    assert (
        document["response_time_metrics"]["provenance"]["calibration_status"]
        == "not_calibrated"
    )
    assert empty_topics.json()["topics"] == []
    assert empty_topics.json()["window"]["scope"] == "requested"
    assert empty_topics.json()["range_provenance"]["sample_coverage"] is None
    assert (
        empty_topics.json()["range_provenance"]["unavailable_reason"]
        == "no_eligible_samples"
    )
    empty_response_document = empty_response.json()
    assert empty_response_document["average_handling_time_minutes"] is None
    assert empty_response_document["silence_percentage"] is None
    assert empty_response_document["turns"] is None
    assert empty_response_document["response_coverage"] is None
    assert empty_response_document["unavailable_reasons"] == {
        "average_handling_time_minutes": "no_responses",
        "response_coverage": "no_response_opportunities",
        "silence_percentage": "no_response_opportunities",
        "turns": "no_messages",
    }
    for topic in document["topics"]:
        if topic["trend"] is None:
            assert topic["trend_unavailable_reason"] in {
                "insufficient_time_span",
                "zero_baseline_samples",
            }


@pytest.mark.asyncio
async def test_zero_denominators_are_unavailable_instead_of_fabricated() -> None:
    repositories = create_canonical_repositories("memory")
    payload = await seed(repositories, "creator-alpha")
    projection = AnalyticsPipeline(repositories.ingestion).project_account(
        payload.creator_account_id
    ).artifact.projection
    outbound = next(
        item
        for item in projection.message_enrichments
        if item.direction is MessageDirection.OUTBOUND
    )
    response_window = AnalyticsWindow(
        scope=WindowScope.REQUESTED,
        start=outbound.sent_at,
        end=outbound.sent_at,
    )
    response = insights_service._response_metrics(
        projection,
        [outbound],
        response_window,
    )
    assert response.response_coverage is None
    assert response.silence_percentage is None
    assert response.average_handling_time_minutes is None
    assert response.turns == 1.0
    assert response.provenance.sample_coverage is None
    assert response.provenance.unavailable_reason == "no_response_opportunities"

    topic_message = next(
        item
        for item in projection.message_enrichments
        if item.topic_entities.topics
    )
    topic_window = AnalyticsWindow(
        scope=WindowScope.REQUESTED,
        start=topic_message.sent_at - timedelta(hours=2),
        end=topic_message.sent_at,
    )
    topic_metrics = insights_service._topic_metrics([topic_message], topic_window)
    assert topic_metrics
    assert all(item.trend is None for item in topic_metrics)
    assert {item.trend_unavailable_reason for item in topic_metrics} == {
        "zero_baseline_samples"
    }


@pytest.mark.asyncio
async def test_public_errors_use_stable_codes_and_redact_inputs() -> None:
    payload = await seed_default("creator-beta")
    bind_session(payload.creator_account_id)
    private_account = "private-cross-account-7f3e"
    private_timestamp = "private-canonical-value-not-a-timestamp"

    with TestClient(app) as client:
        cross = client.get(
            "/api/v1/insights/full",
            params={"creator_account_id": private_account},
        )
        invalid_time = client.get(
            "/api/v1/insights/full",
            params={"start_date": private_timestamp},
        )
        naive_time = client.get(
            "/api/v1/insights/full",
            params={"start_date": "2026-07-19T12:00:00"},
        )
        bad_config = client.get(
            "/api/v1/agent/config",
            params={
                "auth_ticket": DEV_AGENT_AUTH_TICKET,
                # agent_installation_id is a UUID-typed query parameter;
                # FastAPI validates and rejects a malformed one (422, echoing
                # the invalid input) before the endpoint body's own
                # "auth_ticket must not appear in the URL" check ever runs.
                # Keep it well-formed so this exercises that 400 rejection
                # specifically, without a private value leaking through an
                # unrelated FastAPI parameter-validation error instead.
                "agent_installation_id": "00000000-0000-4000-8000-000000000000",
                "creator_account_id": private_account,
                "supported_config_schema_versions": "1",
            },
        )

    assert cross.status_code == 403
    assert cross.json()["detail"]["code"] == "account_binding_mismatch"
    assert invalid_time.status_code == naive_time.status_code == 422
    assert invalid_time.json()["detail"]["code"] == "analytics_timestamp_invalid"
    assert (
        naive_time.json()["detail"]["code"]
        == "analytics_timestamp_timezone_required"
    )
    assert bad_config.status_code == 400
    for response in (cross, invalid_time, naive_time, bad_config):
        assert private_account not in response.text
        assert private_timestamp not in response.text


@pytest.mark.asyncio
async def test_rebuild_source_is_existing_read_only_schema_and_output_is_atomic(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    payload = await seed(repositories, "creator-beta")
    before = database_path.read_bytes()
    before_mtime = database_path.stat().st_mtime_ns
    arguments = argparse.Namespace(
        backend="sqlite",
        canonical_path=database_path,
        account_id=payload.creator_account_id,
        output=None,
    )

    first = rebuild_from_args(arguments)
    second = rebuild_from_args(arguments)
    output = tmp_path / "projection.json"
    assert rebuild_main(
        [
            "--canonical-path",
            str(database_path),
            "--account-id",
            payload.creator_account_id,
            "--output",
            str(output),
        ]
    ) == 0

    assert first == second == output.read_text(encoding="utf-8")
    assert database_path.read_bytes() == before
    assert database_path.stat().st_mtime_ns == before_mtime
    assert json.loads(first)["projection"]["source_revision"] == 1
    assert not list(tmp_path.glob(".projection.json.*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) & stat.S_IWOTH == 0

    missing_path = tmp_path / "mistyped" / "canonical.sqlite3"
    with pytest.raises(RebuildFailure) as missing:
        rebuild_from_args(
            argparse.Namespace(
                backend="sqlite",
                canonical_path=missing_path,
                account_id=payload.creator_account_id,
                output=None,
            )
        )
    assert missing.value.code == "canonical_database_missing"
    assert not missing_path.exists()
    assert not missing_path.parent.exists()

    invalid_database = tmp_path / "invalid.sqlite3"
    invalid_database.write_bytes(b"synthetic-not-a-sqlite-database")
    invalid_before = invalid_database.read_bytes()
    with pytest.raises(RebuildFailure) as invalid:
        rebuild_from_args(
            argparse.Namespace(
                backend="sqlite",
                canonical_path=invalid_database,
                account_id=payload.creator_account_id,
                output=None,
            )
        )
    assert invalid.value.code in {
        "canonical_database_invalid",
        "canonical_schema_incompatible",
    }
    assert invalid_database.read_bytes() == invalid_before

    private_missing_account = "private-missing-account-1"
    with pytest.raises(RebuildFailure) as unknown:
        rebuild_from_args(
            argparse.Namespace(
                backend="sqlite",
                canonical_path=database_path,
                account_id=private_missing_account,
                output=None,
            )
        )
    assert unknown.value.code == "canonical_account_not_found"
    assert private_missing_account not in str(unknown.value)

    with pytest.raises(SystemExit) as conflict:
        rebuild_main(
            [
                "--canonical-path",
                str(database_path),
                "--account-id",
                payload.creator_account_id,
                "--output",
                str(database_path),
            ]
        )
    assert "rebuild_output_conflicts_with_source" in str(conflict.value)
    assert database_path.read_bytes() == before


@pytest.mark.asyncio
async def test_rebuild_cli_subprocess_uses_sanitized_atomic_boundary(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    payload = await seed(repositories, "creator-beta")
    output = tmp_path / "projection.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.analytics.rebuild",
            "--canonical-path",
            str(database_path),
            "--account-id",
            payload.creator_account_id,
            "--output",
            str(output),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert json.loads(output.read_text(encoding="utf-8"))["projection"][
        "source_revision"
    ] == 1
    if os.name == "nt":
        evidence = _windows_acl_evidence(output)
        assert evidence.protected and evidence.only_owner_has_access
        assert evidence.ace_count == 1

    private_account = "private-subprocess-account-4c9a"
    rejected = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.analytics.rebuild",
            "--canonical-path",
            str(database_path),
            "--account-id",
            private_account,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "canonical_account_not_found" in rejected.stderr
    assert private_account not in rejected.stdout + rejected.stderr
    assert str(database_path) not in rejected.stdout + rejected.stderr


@pytest.mark.asyncio
async def test_rebuild_pins_one_source_transaction_across_path_swap(
    tmp_path: Path,
) -> None:
    alpha_path = tmp_path / "canonical-alpha.sqlite3"
    beta_path = tmp_path / "canonical-beta.sqlite3"
    alpha_repositories = create_canonical_repositories(
        "sqlite", canonical_path=alpha_path
    )
    beta_repositories = create_canonical_repositories("sqlite", canonical_path=beta_path)
    alpha = await seed(alpha_repositories, "creator-alpha")
    beta = await seed(beta_repositories, "creator-beta")

    database = ReadOnlyCanonicalDatabase(alpha_path)
    with database:
        database.validate_schema()
        # Mirrors app.analytics.rebuild._open_source's own construction: the
        # pinned read-only connection is bound directly and HistoryRepository
        # itself is never touched (see HistoryAnalyticsSource._read), so an
        # uninitialized instance is safe here. rebuild.py no longer defines a
        # standalone SQLiteIngestionRepository class.
        source = HistoryAnalyticsSource(
            HistoryRepository.__new__(HistoryRepository),
            connection=database.connection,
        )
        with database.read() as first, database.read() as second:
            assert first is second
        assert source.account_exists(alpha.creator_account_id)
        assert not source.account_exists(beta.creator_account_id)

        swapped = False
        try:
            os.replace(beta_path, alpha_path)
            swapped = True
        except PermissionError:
            # Windows may deny rename/delete sharing while SQLite owns the handle.
            pass

        assert source.account_exists(alpha.creator_account_id)
        assert not source.account_exists(beta.creator_account_id)
        if swapped:
            with pytest.raises(RebuildFailure) as changed:
                database.verify_identity()
            assert changed.value.code == "canonical_source_changed"
        else:
            database.verify_identity()


@pytest.mark.asyncio
async def test_rebuild_rejects_link_and_parent_alias_sources(tmp_path: Path) -> None:
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    payload = await seed(repositories, "creator-beta")
    arguments = lambda path: argparse.Namespace(
        backend="sqlite",
        canonical_path=path,
        account_id=payload.creator_account_id,
        output=None,
    )

    alias_path = tmp_path / "canonical-alias.sqlite3"
    try:
        alias_path.symlink_to(database_path)
    except OSError:
        alias_path = None
    if alias_path is not None:
        with pytest.raises(RebuildFailure) as linked:
            rebuild_from_args(arguments(alias_path))
        assert linked.value.code == "canonical_source_unsafe"

    parent_alias = tmp_path / "synthetic-child" / ".." / database_path.name
    with pytest.raises(RebuildFailure) as aliased:
        rebuild_from_args(arguments(parent_alias))
    assert aliased.value.code == "canonical_source_unsafe"


@pytest.mark.asyncio
async def test_rebuild_trusts_only_the_repository_migration_prefix_and_schema(
    tmp_path: Path,
) -> None:
    trusted_path = tmp_path / "trusted.sqlite3"
    repositories = create_canonical_repositories("sqlite", canonical_path=trusted_path)
    payload = await seed(repositories, "creator-beta")
    cases: list[Path] = []

    checksum_path = tmp_path / "tampered-checksum.sqlite3"
    shutil.copy2(trusted_path, checksum_path)
    with sqlite3.connect(checksum_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 2",
            ("0" * 64,),
        )
    cases.append(checksum_path)

    skipped_path = tmp_path / "skipped-migration.sqlite3"
    shutil.copy2(trusted_path, skipped_path)
    with sqlite3.connect(skipped_path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 1")
    cases.append(skipped_path)

    missing_index_path = tmp_path / "missing-index.sqlite3"
    shutil.copy2(trusted_path, missing_index_path)
    with sqlite3.connect(missing_index_path) as connection:
        connection.execute("DROP INDEX account_messages_page")
    cases.append(missing_index_path)

    forged_path = tmp_path / "forged-partial.sqlite3"
    catalog = load_migration_catalog()
    with sqlite3.connect(forged_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE account_read_models (
                creator_account_id TEXT PRIMARY KEY,
                view_revision INTEGER NOT NULL
            );
            CREATE TABLE read_model_chats (
                creator_account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                document_json TEXT NOT NULL
            );
            CREATE TABLE read_model_messages (
                creator_account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                document_json TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (?, ?, ?, 'synthetic')
            """,
            [(item.version, item.name, item.checksum) for item in catalog],
        )
        connection.execute(f"PRAGMA user_version = {len(catalog)}")
    cases.append(forged_path)

    private_value = "private-canonical-value-91fd"
    for path in cases:
        with pytest.raises(RebuildFailure) as rejected:
            rebuild_from_args(
                argparse.Namespace(
                    backend="sqlite",
                    canonical_path=path,
                    account_id=payload.creator_account_id,
                    output=None,
                )
            )
        assert rejected.value.code == "canonical_schema_incompatible"
        assert private_value not in str(rejected.value)
        assert str(path) not in str(rejected.value)


@pytest.mark.asyncio
async def test_rebuild_rejects_quick_check_ok_missing_index_entry(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "index-corruption.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite",
        canonical_path=database_path,
    )
    payload = await seed(repositories, "creator-beta")
    private_value = "private-corrupt-index-account-8d21"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO commands (
                command_id, creator_account_id, action_json, deadline,
                idempotency_policy, issued_at, state, connection_id,
                fencing_token, delivery_attempts, failure_reason,
                result_apply_count
            ) VALUES (?, ?, '{}', ?, 'at_most_once', ?, 'issued', NULL,
                      NULL, 0, NULL, 0)
            """,
            (
                "synthetic-corrupt-command",
                payload.creator_account_id,
                "2026-01-02T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        index_row = connection.execute(
            """
            SELECT type, name, tbl_name, rootpage, sql
            FROM sqlite_schema
            WHERE name = 'commands_by_account'
            """
        ).fetchone()
        assert index_row is not None
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "DELETE FROM sqlite_schema WHERE name = 'commands_by_account'"
        )
        connection.execute("PRAGMA writable_schema = OFF")
        schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")

    # Reopen with the index absent from the schema cache so this update cannot
    # update its b-tree, then restore the exact catalog row and root page.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE commands
            SET creator_account_id = ?, issued_at = ?
            WHERE command_id = 'synthetic-corrupt-command'
            """,
            (private_value, "2026-01-03T00:00:00+00:00"),
        )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            """
            INSERT INTO sqlite_schema(type, name, tbl_name, rootpage, sql)
            VALUES (?, ?, ?, ?, ?)
            """,
            index_row,
        )
        connection.execute("PRAGMA writable_schema = OFF")
        schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")

    with sqlite3.connect(database_path) as connection:
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    assert quick_rows == [("ok",)]
    assert any("missing from index commands_by_account" in row[0] for row in integrity_rows)

    with pytest.raises(RebuildFailure) as rejected:
        rebuild_from_args(
            argparse.Namespace(
                backend="sqlite",
                canonical_path=database_path,
                account_id=payload.creator_account_id,
                output=None,
            )
        )
    public = str(rejected.value)
    assert rejected.value.code == "canonical_database_invalid"
    assert public == "The canonical database failed integrity validation."
    assert private_value not in public
    assert "commands_by_account" not in public
    assert "missing from index" not in public
    assert str(database_path) not in public


@pytest.mark.asyncio
async def test_rebuild_sanitizes_repository_and_validation_failures(
    tmp_path: Path,
) -> None:
    private_value = "private-direction-value-d148"
    private_account = "private-account-value-28a7"
    database_path = tmp_path / "private-source-name.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    payload = await seed(repositories, "creator-beta")
    with sqlite3.connect(database_path) as connection:
        # account_messages.direction has a CHECK constraint restricting it to
        # inbound/outbound; disable enforcement on this connection to
        # reproduce a row that was corrupted by some other means (a manual
        # repair, a future writer bug) and would otherwise fail
        # CanonicalMessage's MessageDirection enum validation.
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE account_messages
            SET direction = ?
            WHERE creator_account_id = ?
            """,
            (private_value, payload.creator_account_id),
        )

    with pytest.raises(RebuildFailure) as invalid:
        rebuild_from_args(
            argparse.Namespace(
                backend="sqlite",
                canonical_path=database_path,
                account_id=payload.creator_account_id,
                output=None,
            )
        )
    assert invalid.value.code == "analytics_rebuild_unavailable"
    public = str(invalid.value)
    assert private_value not in public
    assert private_account not in public
    assert str(database_path) not in public
    assert "validation" not in public.lower()


def test_private_output_platform_ports_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "synthetic-output.json"
    output.write_text("{}", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(
        rebuild_module,
        "_set_windows_owner_only_acl",
        lambda path: calls.append("set"),
    )
    monkeypatch.setattr(
        rebuild_module,
        "_verify_private_permissions",
        lambda path, *, platform_name=None: calls.append("verify"),
    )
    _apply_private_permissions(output, platform_name="nt")
    assert calls == ["set", "verify"]

    def refuse(path: Path) -> None:
        raise OSError("synthetic security refusal")

    monkeypatch.setattr(rebuild_module, "_set_windows_owner_only_acl", refuse)
    with pytest.raises(RebuildFailure) as refused:
        _apply_private_permissions(output, platform_name="nt")
    assert refused.value.code == "rebuild_output_security_failed"
    assert "synthetic security refusal" not in str(refused.value)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL APIs")
@pytest.mark.asyncio
async def test_windows_rebuild_output_has_one_protected_owner_ace(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=database_path
    )
    payload = await seed(repositories, "creator-beta")
    output = tmp_path / "projection.json"

    assert rebuild_main(
        [
            "--canonical-path",
            str(database_path),
            "--account-id",
            payload.creator_account_id,
            "--output",
            str(output),
        ]
    ) == 0
    evidence = _windows_acl_evidence(output)
    assert evidence.protected is True
    assert evidence.ace_count == 1
    assert evidence.only_owner_has_access is True
