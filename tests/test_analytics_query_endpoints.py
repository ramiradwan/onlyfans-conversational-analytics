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

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]

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


def test_question_reuses_pinned_canonical_connection_for_evidence(ready, monkeypatch):
    original = ready.source.read_evidence_message
    scoped = []

    def observed(*args, **kwargs):
        scoped.append(
            getattr(ready.source._question_scope_local, "connection", None) is not None
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(ready.source, "read_evidence_message", observed)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    assert len(response.json()["page"]["rows"]) == 2
    assert scoped and all(scoped)


def test_production_source_preserves_missing_kind_as_undetermined(ready):
    ready.resources.source = HistoryAnalyticsSource(ready.stored.history)
    ready.resources.source.prepare_question_identity(ACCOUNT)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    page = response.json()["page"]
    assert page["rows"] == [] and page["undetermined_conversation_count"] == 2
    assert page["coverage"]["history"] == "unknown"


def test_production_coverage_survives_publication_without_known_kinds(ready):
    from tests.analytics_coverage_fixture import seed_coverage

    with ready.stored.database.transaction() as db:
        seed_coverage(db, ACCOUNT, ("synthetic-chat-a", "synthetic-chat-b"), NOW)
    ready.pipeline.project_account(ACCOUNT)
    ready.resources.source = HistoryAnalyticsSource(ready.stored.history)
    ready.resources.source.prepare_question_identity(ACCOUNT)
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    page = response.json()["page"]
    assert page["rows"] == [] and page["undetermined_conversation_count"] == 2
    assert page["coverage"]["history"] == "complete"


def test_coverage_change_refuses_old_cursor_and_clears_source_links(ready):
    from tests.analytics_coverage_fixture import seed_coverage

    with ready.stored.database.transaction() as db:
        seed_coverage(db, ACCOUNT, ("synthetic-chat-a", "synthetic-chat-b"), NOW)
    ready.pipeline.project_account(ACCOUNT)
    first = ready.client.post("/api/v1/insights/questions", json=plan(page_size=1)).json()
    assert first["page"]["coverage"]["history"] == "complete" and first["next_cursor"]
    reference = first["page"]["rows"][0]["evidence"][0]
    with ready.stored.database.transaction() as db:
        db.execute("UPDATE coverage_members SET history_started_at=NULL WHERE creator_account_id=?", (ACCOUNT,))
    assert ready.client.post("/api/v1/insights/questions/evidence", json=reference).status_code == 404
    assert not ready.resources.evidence._entries
    stale = ready.client.post("/api/v1/insights/questions", json=plan(page_size=1, cursor=first["next_cursor"]))
    assert stale.status_code == 503 and "rows" not in stale.json()
    assert not ready.resources.evidence._entries
    assert ready.client.post("/api/v1/insights/questions/evidence", json=reference).status_code == 404


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


def test_cold_question_requests_owned_preparation_before_returning_answers(ready):
    ready.source._identity_cache.clear()
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 503
    ready.scheduler.request_recovery.assert_awaited_once()
    assert ready.pipeline.prepare_questions(ACCOUNT, ready.source.account_revision(ACCOUNT))
    response = ready.client.post("/api/v1/insights/questions", json=plan())
    assert response.status_code == 200, response.text
    assert len(response.json()["page"]["rows"]) == 2


@pytest.fixture
def lazy_ready(ready):
    from app.analytics.resilient_projection_store import LazySQLiteAnalyticsProjectionStore
    previous = ready.pipeline.projections
    store = LazySQLiteAnalyticsProjectionStore(previous.database.path,
        activation=ready.stored.projection_activation,
        canonical_identity_reader=lambda account: canonical_identity(ready.source.account_read_model(account)))
    store.ensure_ready()
    ready.pipeline.projections = store
    try:
        yield ready, store
    finally:
        ready.pipeline.projections = previous
        store.close()


@pytest.mark.parametrize('lazy', [False, True])
def test_one_analytics_connection_per_question_with_two_live_checks(ready, lazy_ready, monkeypatch, lazy):
    item, lazy_store = lazy_ready
    store = lazy_store if lazy else ready.stores.projections
    ready.pipeline.projections = store
    database = store.database
    original = database.connect
    connections = []
    def counted():
        value = original();connections.append(value);return value
    monkeypatch.setattr(database, 'connect', counted)
    result = ready.resources.execute(policy(), plan())
    assert len(result.page.rows) == 2
    assert len(connections) == 1  # Initial and final statements, one owned autocommit connection.
    ready.resources.close()
    for connection in connections:
        with pytest.raises(Exception):connection.execute('SELECT 1')


def test_connection_cost_stays_inside_original_budget_without_duplicate_opens(lazy_ready, monkeypatch):
    ready, store = lazy_ready
    from app.analytics.query_service import AnalyticsQuestionService
    from app.analytics.query_reader import PublishedQuestionReader
    from app.analytics.query_service import RegisteredQuestion
    from app.analytics.query_handlers import no_later_creator_reply
    REPLY = RegisteredQuestion('no_later_creator_reply.v1', 'canonical.v2', no_later_creator_reply)
    now = [0.0]
    original = store.database.connect
    opens = []
    def delayed():
        # A deterministic model of the native trace's slow open, not a changed
        # production clock or relaxed one-second deadline.
        now[0] += 0.22;opens.append(now[0]);return original()
    monkeypatch.setattr(store.database, 'connect', delayed)
    reader = PublishedQuestionReader(ready.source, store, ACCOUNT, policy(),
        ready.resources.evidence, ready.pipeline.pipeline_revision, ready.pipeline.pipeline_config_digest)
    service = AnalyticsQuestionService(reader, [REPLY], clock=lambda: NOW,
        monotonic=lambda: now[0], limits=QuestionLimits(wall_clock_ms=1000))
    response = service.execute(policy(), plan())
    assert len(response.page.rows) == 2 and opens == [0.22]


def test_publication_connection_timeout_does_not_quarantine_valid_store(lazy_ready, monkeypatch):
    ready, store = lazy_ready
    from app.analytics.query_service import AnalyticsQuestionService
    from app.analytics.query_reader import PublishedQuestionReader
    from app.analytics.query_service import RegisteredQuestion
    from app.analytics.query_handlers import no_later_creator_reply
    REPLY = RegisteredQuestion('no_later_creator_reply.v1', 'canonical.v2', no_later_creator_reply)
    now = [0.0];original = store.database.connect;opened_store = store._store
    def delayed():
        value=original();now[0] += 1.1;return value
    monkeypatch.setattr(store.database, 'connect', delayed)
    reader=PublishedQuestionReader(ready.source,store,ACCOUNT,policy(),ready.resources.evidence,
        ready.pipeline.pipeline_revision,ready.pipeline.pipeline_config_digest)
    service=AnalyticsQuestionService(reader,[REPLY],clock=lambda:NOW,monotonic=lambda:now[0])
    with pytest.raises(QuestionLimitExceeded):service.execute(policy(),plan())
    assert store._store is opened_store and not store._needs_recovery


def test_final_publication_still_rechecks_witness_on_the_request_connection(lazy_ready, monkeypatch):
    ready, store=lazy_ready
    original=store.activation.get;seen=[]
    def changed(generation_id):
        seen.append(generation_id)
        return original(generation_id) if len(seen)==1 else None
    monkeypatch.setattr(store.activation,'get',changed)
    result=ready.client.post('/api/v1/insights/questions',json=plan())
    assert result.status_code==503 and 'rows' not in result.json()
    assert len(seen)==2 and not ready.resources.evidence._entries


def test_final_publication_refuses_replaced_physical_file(lazy_ready, monkeypatch):
    ready,store=lazy_ready
    original=store.database._observe_private_files;seen=[]
    def changed():
        seen.append(True)
        observed=original()
        return observed if len(seen)==1 else ((-1,-1), *observed[1:])
    monkeypatch.setattr(store.database,'_observe_private_files',changed)
    result=ready.client.post('/api/v1/insights/questions',json=plan())
    assert result.status_code==503 and 'rows' not in result.json()
    assert not ready.resources.evidence._entries
    assert store._needs_recovery



def test_missing_analytics_file_is_not_created_by_question(lazy_ready, tmp_path, monkeypatch):
    ready, store = lazy_ready
    missing = tmp_path / 'missing-analytics.sqlite3'
    store.path = missing
    def forbidden():
        raise AssertionError('query must not open/create a missing store')
    monkeypatch.setattr(store.database, 'connect', forbidden)
    result = ready.client.post('/api/v1/insights/questions', json=plan())
    assert result.status_code == 503 and not missing.exists()
    assert store._needs_recovery and not ready.resources.evidence._entries



def test_sql_interrupt_during_identity_read_remains_a_query_budget_error(lazy_ready, monkeypatch):
    ready, store = lazy_ready
    from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion
    from app.analytics.query_reader import PublishedQuestionReader
    from app.analytics.query_handlers import no_later_creator_reply
    now=[0.0];opened=store._store
    def expired_identity(*, connection):
        now[0]=2.0
        connection.execute("WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<1000) SELECT sum(n) FROM x").fetchone()
        raise AssertionError('expired query was not interrupted')
    monkeypatch.setattr(store.database,'store_identity',expired_identity)
    reader=PublishedQuestionReader(ready.source,store,ACCOUNT,policy(),ready.resources.evidence,
        ready.pipeline.pipeline_revision,ready.pipeline.pipeline_config_digest)
    service=AnalyticsQuestionService(reader,[RegisteredQuestion('no_later_creator_reply.v1','canonical.v2',no_later_creator_reply)],
        clock=lambda:NOW,monotonic=lambda:now[0])
    with pytest.raises(QuestionLimitExceeded):service.execute(policy(),plan())
    assert store._store is opened and not store._needs_recovery


@pytest.mark.parametrize('lazy', [False, True])
@pytest.mark.parametrize('retained_cursor', [False, True])
def test_request_connection_observes_committed_changes_at_final_check(ready, lazy_ready, monkeypatch, lazy, retained_cursor):
    from app.analytics.query_reader import _QuestionSession
    item,lazy_store=lazy_ready
    store=lazy_store if lazy else ready.stores.projections
    ready.pipeline.projections=store
    held = []
    if retained_cursor:
        inner = store._store if lazy else store
        read_snapshot = inner.question_snapshot
        def retain(*args, **kwargs):
            connection = kwargs['connection']
            if not held:
                cursor = connection.execute('SELECT generation_id FROM projection_generations '
                    'UNION ALL SELECT generation_id FROM projection_generations')
                assert cursor.fetchone() is not None
                held.append(cursor)
                assert not connection.in_transaction
            return read_snapshot(*args, **kwargs)
        monkeypatch.setattr(inner, 'question_snapshot', retain)
    original=_QuestionSession.assert_current
    commits=[]
    def change_then_recheck(session,snapshot,budget):
        # A different connection commits while the request connection is open.
        # A pinned transaction/cached snapshot would wrongly hide this change.
        with store.database.transaction() as db:
            # The schema intentionally rejects metadata rewrites. Retirement
            # is a permitted committed state change and must be seen at exit.
            changed=db.execute("UPDATE projection_generations SET status='retired' WHERE generation_id=?",
                               (snapshot.generation_id,))
            assert changed.rowcount==1
        commits.append(True)
        return original(session,snapshot,budget)
    monkeypatch.setattr(_QuestionSession,'assert_current',change_then_recheck)
    response=ready.client.post('/api/v1/insights/questions',json=plan())
    assert response.status_code==503 and 'rows' not in response.json()
    assert commits==[True] and not ready.resources.evidence._entries
    for cursor in held:
        with pytest.raises(Exception): cursor.fetchone()


def test_discard_closes_idle_question_handles_and_revokes_evidence(lazy_ready):
    from app.persistence.database import LocalSQLite
    ready, store = lazy_ready
    answer = ready.resources.execute(policy(), plan())
    assert answer.page.rows
    assert LocalSQLite.open_connection_count(ready.stored.database.path) >= 1
    ready.resources.discard(ACCOUNT)
    assert ready.resources._connections.wait_closed(0)
    assert not ready.resources.evidence._entries


def test_publication_native_connection_reused_with_revoked_request_leases(lazy_ready,monkeypatch):
    ready,store=lazy_ready
    original=store.database.connect;connections=[]
    def opened():
        connection=original();connections.append(connection);return connection
    monkeypatch.setattr(store.database,'connect',opened)
    for _ in range(2):
        assert ready.client.post('/api/v1/insights/questions',json=plan()).status_code==200
    assert len(connections)==1
    ready.resources.close()
    for connection in connections:
        with pytest.raises(Exception):connection.execute('SELECT 1')


def test_request_connection_closes_on_handler_failure(lazy_ready,monkeypatch):
    ready,store=lazy_ready
    from app.analytics.query_reader import _QuestionSession
    original=store.database.connect;connections=[]
    def opened():
        connection=original();connections.append(connection);return connection
    monkeypatch.setattr(store.database,'connect',opened)
    def stop(*args,**kwargs):raise QuestionLimitExceeded()
    monkeypatch.setattr(_QuestionSession,'conversations',stop)
    with pytest.raises(QuestionLimitExceeded):ready.resources.execute(policy(),plan())
    assert len(connections)==1 and not ready.resources.evidence._entries
    with pytest.raises(Exception):connections[0].execute('SELECT 1')
    assert not store._needs_recovery



def test_final_live_file_permission_check_is_not_skipped(lazy_ready,monkeypatch):
    ready,store=lazy_ready
    from app.persistence.private_files import PrivateFileSecurityError
    original=store.database._observe_private_files;checks=[]
    def permissions():
        checks.append(True)
        if len(checks)>1:raise PrivateFileSecurityError('live permissions cannot be secured')
        return original()
    monkeypatch.setattr(store.database,'_observe_private_files',permissions)
    response=ready.client.post('/api/v1/insights/questions',json=plan())
    assert len(checks)==2 and response.status_code==503 and 'rows' not in response.json()
    assert not ready.resources.evidence._entries



def test_publication_uses_each_fresh_permission_identity_without_duplicate_stat(lazy_ready,monkeypatch):
    from pathlib import Path
    from app.persistence import database as persistence
    ready,store=lazy_ready
    original_exists=Path.exists;observe=persistence.private_file_identity
    probes=[];identities=[]
    def exists(path):
        if path is store.path:probes.append(True)
        return original_exists(path)
    def identity(path, **kwargs):
        value=observe(path, **kwargs)
        if path == store.path:identities.append(value)
        return value
    def duplicate_stat():raise AssertionError('security already returned this boundary file identity')
    monkeypatch.setattr(Path,'exists',exists)
    monkeypatch.setattr(persistence,'private_file_identity',identity)
    monkeypatch.setattr(store,'_file_identity_for_path',duplicate_stat)
    answer=ready.resources.execute(policy(),plan())
    assert len(answer.page.rows)==2
    assert len(probes)==1  # Still prevent creating a missing file before opening.
    assert len(identities)==2 and identities==[store._file_identity]*2


@pytest.mark.parametrize('boundary',[1,2])
def test_live_file_observation_time_is_in_original_question_budget(lazy_ready, monkeypatch, boundary):
    from app.persistence import database as persistence
    from app.analytics.query_service import AnalyticsQuestionService,RegisteredQuestion
    from app.analytics.query_reader import PublishedQuestionReader
    from app.analytics.query_handlers import no_later_creator_reply
    ready,store=lazy_ready;db=store.database;original=persistence.private_file_identity
    now=[0.0];seen=[];connections=[];connect=db.connect
    def opened():
        value=connect();connections.append(value);return value
    def identity(path,**kwargs):
        value=original(path,**kwargs)
        if path==store.path:
            seen.append(value)
            if len(seen)==boundary:now[0]+=1.1
        return value
    monkeypatch.setattr(persistence,'private_file_identity',identity)
    monkeypatch.setattr(db,'connect',opened)
    reader=PublishedQuestionReader(ready.source,store,ACCOUNT,policy(),ready.resources.evidence,
        ready.pipeline.pipeline_revision,ready.pipeline.pipeline_config_digest)
    service=AnalyticsQuestionService(reader,[RegisteredQuestion('no_later_creator_reply.v1','canonical.v2',no_later_creator_reply)],
        clock=lambda:NOW,monotonic=lambda:now[0],limits=QuestionLimits(wall_clock_ms=1000))
    with pytest.raises(QuestionLimitExceeded):service.execute(policy(),plan())
    assert len(seen)==boundary and not store._needs_recovery
    assert not ready.resources.evidence._entries
    for connection in connections:
        with pytest.raises(Exception):connection.execute('SELECT 1')


def test_live_permission_identities_are_not_reused_between_questions(lazy_ready, monkeypatch):
    from app.persistence import database as persistence
    ready,store=lazy_ready;original=persistence.private_file_identity;seen=[]
    def identity(path,**kwargs):
        value=original(path,**kwargs)
        if path==store.path:seen.append(value)
        return value
    monkeypatch.setattr(persistence,'private_file_identity',identity)
    for _ in range(2):assert ready.resources.execute(policy(),plan()).page.rows
    assert len(seen)==4
