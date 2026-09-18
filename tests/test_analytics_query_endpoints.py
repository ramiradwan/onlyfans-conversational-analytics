"""Exercise authenticated questions against real encrypted canonical storage."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.analytics.query_runtime import QuestionResources, PricingNotQualified
from app.analytics.runtime import AnalyticsRuntime
from app.analytics.scheduling import InProcessProjectionScheduler
from app.api.activation import require_activated_runtime
from app.api.dependencies import get_authenticated_account_session
from app.api.endpoints import insights
from app.api.security import csrf_token
from app.services import insights_service
from tests.test_analytics_evidence import stored, policy, ACCOUNT, OTHER, LOCATION, NOW as FIXTURE_NOW

SOURCE_NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOW = SOURCE_NOW + timedelta(days=1)


class KnownSyntheticSource(HistoryAnalyticsSource):
    """Supply independently known event kinds for synthetic integration data."""

    @contextmanager
    def open_question_scope(self, account, budget):
        with super().open_question_scope(account, budget) as scope:
            original = scope.conversations
            def known(question, work):
                for conversation in original(question, work):
                    yield replace(conversation, messages=tuple(
                        replace(message, kind="message", language="en") for message in conversation.messages))
            scope.conversations = known
            yield scope


@pytest.fixture
def ready(stored, tmp_path, monkeypatch):
    with stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET sent_at=?", ((SOURCE_NOW-timedelta(hours=1)).isoformat(),))
    source = KnownSyntheticSource(stored.history)
    stores = create_analytics_stores("sqlite", projections_path=tmp_path / "analytics.sqlite3",
        activation=stored.projection_activation,
        canonical_identity_reader=lambda account: canonical_identity(source.account_read_model(account)),
        retention_clock=lambda: NOW)
    pipeline = AnalyticsPipeline(source, projections=stores.projections, clock=lambda: NOW)
    pipeline.project_account(ACCOUNT)
    resources = QuestionResources(source, pipeline, clock=lambda: NOW)
    scheduler = InProcessProjectionScheduler(pipeline)
    scheduler.request_recovery = AsyncMock()
    runtime = AnalyticsRuntime(source, pipeline, scheduler, resources)
    monkeypatch.setattr(insights_service, "analytics_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[get_authenticated_account_session] = policy
    app.dependency_overrides[require_activated_runtime] = lambda: None
    client = TestClient(app)
    client.headers["x-csrf-token"] = csrf_token(policy())
    yield SimpleNamespace(source=source, stored=stored, pipeline=pipeline, resources=resources,
                          scheduler=scheduler, client=client, app=app, stores=stores)
    resources.close()
    scheduler.abort()
    stores.projections.close_retention_scheduler()
    client.close()


def plan(**updates):
    return {"question": "no_later_creator_reply.v1", "timezone": "UTC",
            "start": (SOURCE_NOW-timedelta(days=1)).isoformat(), "end": SOURCE_NOW.isoformat(),
            "cutoff": NOW.isoformat(), **updates}


def test_streamed_identity_equals_the_canonical_read_model(ready):
    expected = canonical_identity(ready.source.account_read_model(ACCOUNT))
    with ready.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits())) as scope:
        assert scope.identity == expected


def test_query_to_source_round_trip(ready):
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["page"]["rows"]) == 2
    assert response.headers["cache-control"] == "no-store"
    reference = body["page"]["rows"][0]["evidence"][0]
    source = ready.client.post("/api/v1/insights/questions/evidence", json=reference)
    assert source.status_code == 200, source.text
    assert source.json()["text"] == "A\U0001f44b price \u20ac20"
    assert source.headers["cache-control"] == "no-store"
    assert "text" not in body["page"]["rows"][0]
    assert ready.client.delete("/api/v1/insights/questions/evidence").status_code == 204
    assert ready.client.post("/api/v1/insights/questions/evidence", json=reference).status_code == 404


def test_production_source_preserves_missing_kind_as_undetermined(ready):
    ready.resources.source = HistoryAnalyticsSource(ready.stored.history)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    page = response.json()["page"]
    assert page["rows"] == [] and page["undetermined_conversation_count"] == 2
    assert page["coverage"]["history"] == "unknown"


def test_pricing_gate_is_not_a_query_option(ready):
    response = ready.client.post("/api/v1/insights/questions", json=plan(question="pricing_discussions.v1"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "analytics_pricing_not_qualified"
    definitions = ready.client.get("/api/v1/insights/questions").json()["questions"]
    assert definitions[1]["enabled"] is False
    ready.scheduler.request_recovery.assert_not_called()


@pytest.mark.parametrize("update", [
    {"creator_account_id": OTHER}, {"gremlin": "g.V()"}, {"page_size": 0},
    {"page_size": 201}, {"page_size": True}, {"timezone": "../UTC"},
    {"start": "2026-09-18"}, {"end": "2030-01-01T00:00:00Z"},
    {"filters": {"account": OTHER}}, {"question": "unsupported.v1"},
])
def test_invalid_request_does_not_echo_input(ready, update):
    response = ready.client.post("/api/v1/insights/questions", json=plan(**update))
    assert response.status_code == 422, response.text
    assert "g.V()" not in response.text and OTHER not in response.text
    assert response.headers["cache-control"] == "no-store"
    ready.scheduler.request_recovery.assert_not_called()


def test_csrf_and_activation_are_required(ready):
    assert ready.client.post("/api/v1/insights/questions", json=plan(),
        headers={"x-csrf-token": "invalid"}).status_code == 403
    from fastapi import HTTPException
    def inactive():
        raise HTTPException(403, "Activation is required")
    ready.app.dependency_overrides[require_activated_runtime] = inactive
    assert ready.client.post("/api/v1/insights/questions", json=plan()).status_code == 403


@pytest.mark.parametrize("body,status", [(b'{"question":"one","question":"two"}', 422),
    (b'{invalid', 422), (b'x'*16385, 413)])
def test_request_body_is_bounded_and_unambiguous(ready, body, status):
    assert ready.client.post("/api/v1/insights/questions", content=body,
        headers={"content-type":"application/json"}).status_code == status


def test_real_pagination_and_source_invalidation(ready):
    first = ready.client.post("/api/v1/insights/questions", json=plan(page_size=1)).json()
    cursor = first["next_cursor"]
    assert cursor is not None
    second = ready.client.post("/api/v1/insights/questions", json=plan(page_size=1, cursor=cursor))
    assert second.status_code == 200, second.text
    assert second.json()["next_cursor"] is None
    assert first["page"]["rows"][0]["conversation_ref"] != second.json()["page"]["rows"][0]["conversation_ref"]
    with ready.stored.database.transaction() as db:
        db.execute("UPDATE account_heads SET canonical_revision=8 WHERE creator_account_id=?", (ACCOUNT,))
    stale = ready.client.post("/api/v1/insights/questions", json=plan(page_size=1, cursor=cursor))
    assert stale.status_code == 503 and "rows" not in stale.json()
    ready.scheduler.request_recovery.assert_awaited_with(ACCOUNT, 8)


def test_changed_content_without_revision_is_not_a_current_projection(ready):
    with ready.stored.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='synthetic edited source' WHERE creator_account_id=?", (ACCOUNT,))
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 503
    assert "synthetic edited source" not in response.text


def test_query_does_not_materialize_an_account_or_run_analyzers(ready, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded or inference path")
    monkeypatch.setattr(ready.source, "account_read_model", forbidden)
    monkeypatch.setattr(ready.pipeline.enrichment, "enrich_conversation", forbidden)
    assert ready.client.post("/api/v1/insights/questions", json=plan()).status_code == 200


def test_missing_witness_never_becomes_an_empty_answer(ready, monkeypatch):
    monkeypatch.setattr(ready.stores.projections.activation, "get", lambda _: None)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 503 and "rows" not in response.json()


def test_cross_account_evidence_is_refused_before_source_lookup(ready, monkeypatch):
    row = ready.client.post("/api/v1/insights/questions", json=plan()).json()["page"]["rows"][0]
    ready.app.dependency_overrides[get_authenticated_account_session] = lambda: policy(OTHER)
    ready.client.headers["x-csrf-token"] = csrf_token(policy(OTHER))
    def forbidden(*args):
        raise AssertionError("foreign evidence read")
    monkeypatch.setattr(ready.source, "read_evidence_message", forbidden)
    assert ready.client.post("/api/v1/insights/questions/evidence", json=row["evidence"][0]).status_code == 404
    ready.scheduler.request_recovery.assert_not_called()


def test_streamed_verification_stops_at_the_record_budget(ready):
    with pytest.raises(QuestionLimitExceeded):
        with ready.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=1))):
            pytest.fail("oversized source was accepted")


def test_reader_capacity_is_bounded(ready):
    assert ready.resources._slots.acquire(False) and ready.resources._slots.acquire(False)
    try:
        assert ready.client.post("/api/v1/insights/questions", json=plan()).status_code == 503
    finally:
        ready.resources._slots.release()
        ready.resources._slots.release()


def test_canonical_change_during_a_query_suppresses_results(ready, monkeypatch):
    original = ready.source.read_evidence_message
    def changed(*args):
        record = original(*args)
        with ready.stored.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='synthetic concurrent edit' WHERE creator_account_id=?",
                       (ACCOUNT,))
        return record
    monkeypatch.setattr(ready.source, "read_evidence_message", changed)
    result = ready.client.post("/api/v1/insights/questions", json=plan())
    assert result.status_code in {404, 503}, result.text
    assert "rows" not in result.json() and "synthetic concurrent edit" not in result.text
    assert not ready.resources.evidence._entries


def test_source_expiry_cannot_be_revived_by_an_old_cutoff(ready):
    ready.resources.clock = lambda: NOW + timedelta(days=91)
    result = ready.client.post("/api/v1/insights/questions", json=plan())
    assert result.status_code == 503 and "rows" not in result.json()


def test_closing_runtime_invalidates_bound_source_references(ready):
    result = ready.client.post("/api/v1/insights/questions", json=plan()).json()
    reference = result["page"]["rows"][0]["evidence"][0]
    ready.resources.close()
    assert not ready.resources.evidence._entries
    assert ready.client.post("/api/v1/insights/questions/evidence", json=reference).status_code == 503


def test_origin_and_authenticated_account_are_required(ready, monkeypatch):
    import app.api.security as security
    from fastapi import HTTPException
    monkeypatch.setattr(security, "_development_context_allowed", lambda: False)
    result = ready.client.post("/api/v1/insights/questions", json=plan(),
                              headers={"origin": "https://unrelated.invalid"})
    assert result.status_code == 403
    def denied():
        raise HTTPException(401, "Authentication is required")
    ready.app.dependency_overrides[get_authenticated_account_session] = denied
    assert ready.client.get("/api/v1/insights/questions").status_code == 401


def test_source_change_hook_clears_navigation_without_reading(ready, monkeypatch):
    from app.analytics import runtime as registry
    ready.client.post("/api/v1/insights/questions", json=plan())
    assert ready.resources.evidence._entries
    monkeypatch.setattr(registry, "_RUNTIMES", {1: SimpleNamespace(questions=ready.resources)})
    registry.invalidate_question_sources(ACCOUNT)
    assert not ready.resources.evidence._entries
    ready.scheduler.request_recovery.assert_not_called()


def test_openapi_describes_closed_question_and_evidence_bodies(ready):
    schema = ready.app.openapi()
    operation = schema["paths"]["/api/v1/insights/questions"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["additionalProperties"] is False
    assert set(body["properties"]["question"]["enum"]) == {"no_later_creator_reply.v1", "pricing_discussions.v1"}


def test_retention_cleanup_notifies_only_accounts_with_expired_sources(tmp_path):
    from tests.test_retention_maintenance import database, seed_message, NOW as RETENTION_NOW, ACCOUNT as RETENTION_ACCOUNT
    from app.services.retention_maintenance import RetentionMaintenance
    db = database(tmp_path / "retained.sqlite3")
    seed_message(db, sent_at=RETENTION_NOW-timedelta(days=31))
    calls = []
    maintenance = RetentionMaintenance(db, clock=lambda: RETENTION_NOW, on_source_change=calls.append)
    assert maintenance.run_once().expired_message_count == 1
    assert calls == [RETENTION_ACCOUNT]
    assert maintenance.run_once().expired_message_count == 0
    assert calls == [RETENTION_ACCOUNT]


def test_refresh_failure_does_not_disclose_storage_errors(ready):
    with ready.stored.database.transaction() as db:
        db.execute("UPDATE account_heads SET canonical_revision=8 WHERE creator_account_id=?", (ACCOUNT,))
    ready.scheduler.request_recovery.side_effect = RuntimeError("synthetic private failure")
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 503
    assert "synthetic private failure" not in response.text


def test_scope_uses_indexed_account_and_conversation_access(ready):
    with ready.stored.database.read() as db:
        rows = db.execute("EXPLAIN QUERY PLAN SELECT message_id FROM account_messages WHERE creator_account_id=? AND chat_id=? AND is_deleted=0",
                          (ACCOUNT, LOCATION.conversation_id)).fetchall()
    details = " ".join(str(row[3]) for row in rows)
    assert "SEARCH" in details and "account_messages_page" in details


def test_pricing_handler_reads_published_topics_without_inference(ready, monkeypatch):
    from app.analytics.query_handlers import pricing_discussions
    from app.analytics.query_reader import PublishedQuestionReader
    from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion

    def forbidden(*args, **kwargs):
        raise AssertionError("inference or full-graph materialization during read")
    monkeypatch.setattr(ready.pipeline.enrichment, "enrich_conversation", forbidden)
    monkeypatch.setattr(ready.pipeline.graph, "nodes", forbidden)
    monkeypatch.setattr(ready.pipeline.graph, "edges", forbidden)
    reader = PublishedQuestionReader(ready.source, ready.stores.projections, ACCOUNT,
        policy(), ready.resources.evidence, ready.pipeline.pipeline_revision,
        ready.pipeline.pipeline_config_digest)
    service = AnalyticsQuestionService(reader,
        [RegisteredQuestion("pricing_discussions.v1", "stored-topics.v1", pricing_discussions)],
        clock=lambda: NOW)
    result = service.execute(policy(), plan(question="pricing_discussions.v1"))
    assert len(result.page.rows) == 2
    assert result.page.coverage.analyzed_classification_count == 2
    assert all(row.reason == "pricing_discussion" for row in result.page.rows)
    assert ready.client.post("/api/v1/insights/questions",
        json=plan(question="pricing_discussions.v1")).status_code == 503


def test_locator_cleanup_does_not_hold_the_resource_lock(monkeypatch):
    from threading import Thread
    resources = QuestionResources(None, None)
    resources._remember(policy())
    observed = []
    original = resources.evidence.clear_account
    def clear(value):
        def acquire():
            acquired = resources._lock.acquire(timeout=0.5)
            observed.append(acquired)
            if acquired:
                resources._lock.release()
        worker = Thread(target=acquire)
        worker.start()
        worker.join(timeout=1)
        original(value)
    monkeypatch.setattr(resources.evidence, "clear_account", clear)
    resources._remember(policy(OTHER))
    resources.close()
    assert observed == [True, True]


def test_cache_notification_failure_does_not_stop_retention(stored):
    from app.services.retention_maintenance import RetentionMaintenance
    def failed(account):
        raise RuntimeError("synthetic private notification failure")
    result = RetentionMaintenance(stored.database, clock=lambda: FIXTURE_NOW+timedelta(days=31),
                                  on_source_change=failed).run_once()
    assert result.expired_message_count == 4


@pytest.mark.parametrize("offset,expected_count", [
    (timedelta(hours=2), 1),
    (timedelta(days=1), 1),
    (timedelta(days=1, microseconds=1), 2),
])
def test_canonical_followup_honors_selection_and_cutoff(ready, offset, expected_count):
    followup = SOURCE_NOW + offset
    with ready.stored.database.transaction() as db:
        db.execute("""INSERT INTO account_messages(
            creator_account_id,message_id,chat_id,sender_platform_user_id,
            text,sent_at,direction,content_hash,winning_stream_epoch,
            winning_source_seq,is_deleted,updated_at)
            VALUES (?,'synthetic-reply',?,'synthetic-creator',
                '',?,'outbound','synthetic-reply-hash',1,2,0,?)""",
            (ACCOUNT, LOCATION.conversation_id, followup.isoformat(), SOURCE_NOW.isoformat()))
        db.execute("UPDATE account_heads SET canonical_revision=8 WHERE creator_account_id=?",
                   (ACCOUNT,))
    ready.pipeline.project_account(ACCOUNT)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    page = response.json()["page"]
    assert len(page["rows"]) == expected_count
    assert page["undetermined_conversation_count"] == 0


@pytest.mark.parametrize("route", ["/questions", "/questions/evidence"])
def test_query_parameters_cannot_override_an_authenticated_request(ready, route):
    response = ready.client.post("/api/v1/insights" + route,
        params={"creator_account_id": OTHER}, json=plan())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "analytics_question_invalid"
    assert OTHER not in response.text
    assert response.headers["cache-control"] == "no-store"
    ready.scheduler.request_recovery.assert_not_called()


@pytest.mark.parametrize("headers", [
    {"content-type": "text/plain"},
    {"content-type": "application/json", "content-encoding": "gzip"},
])
def test_unsupported_body_encoding_is_refused(ready, headers):
    response = ready.client.post("/api/v1/insights/questions",
        content=b"{}", headers=headers)
    assert response.status_code == 415
    assert response.headers["cache-control"] == "no-store"
    ready.scheduler.request_recovery.assert_not_called()
