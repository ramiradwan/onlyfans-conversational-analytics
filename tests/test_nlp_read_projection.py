"""Published SQLite findings, immutable filters and live source evidence."""

from contextlib import contextmanager
import copy
from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.nlp_read_projection import frozen_labels
from app.analytics.query_runtime import QuestionResources
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.opaque_refs import account_ref
from app.models.insights import FindingsCategoryFilter, FindingsRequest
from app.nlp.profiles import AnalysisProfile
from app.security.local_sessions import build_runtime_policy
from app.security.runtime_policy import AuthContext
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, advance
from tests.test_nlp_processing_publication import ConformanceAdapter, FIXTURES

pytestmark = pytest.mark.ci_tier("integration")


class FixtureAdapter(ConformanceAdapter):
    def __call__(self, request, cancelled):
        response = json.loads(super().__call__(request, cancelled))
        number = int(request.items[0].text.rsplit("case", 1)[1])
        outcomes = {
            0: ("assessed_findings", "assessed_findings"),
            1: ("assessed_findings", "assessed_findings"),
            2: ("partial", "partial"),
            3: ("uncertain", "unsupported"),
            4: ("failed", "failed"),
            5: ("assessed_no_match", "uncertain"),
            6: ("unprocessed", "unprocessed"),
            7: ("unsupported", "assessed_no_match"),
        }
        for index, item in enumerate(response["items"][0]["tasks"]):
            payload = item["payload"]
            outcome = outcomes[number][index]
            payload["outcome"] = outcome
            if outcome == "partial":
                payload["coverage"].update(
                    examined=[{"start": 0, "end": 6}],
                    excluded=[{"start": 6, "end": len(request.items[0].text)}],
                )
            if outcome in ("assessed_findings", "partial"):
                if index == 0:
                    payload["findings"] = [
                        {"topic_id": c}
                        for c in self.profile.components[0].tasks[0].enabled_ids
                    ]
                else:
                    payload["findings"] = [
                        {
                            "concept_id": "concept.synthetic",
                            "span": {"start": 2, "end": 6},
                        }
                    ]
                    if outcome != "partial":
                        payload["findings"].append(
                            {
                                "concept_id": "concept.synthetic",
                                "span": {"start": 7, "end": 11},
                            }
                        )
        return json.dumps(response).encode()


class CategoryAdapter(FixtureAdapter):
    def __call__(self, request, cancelled):
        response = json.loads(super().__call__(request, cancelled))
        number = int(request.items[0].text.rsplit("case", 1)[1])
        topics = response["items"][0]["tasks"][0]["payload"]
        categories = {0: ["topic.synthetic"], 1: ["concept.synthetic"],
                      2: ["topic.partial"], 4: ["topic.synthetic", "topic.second"]}
        if number in categories:
            topics["outcome"] = "partial" if number == 2 else "assessed_findings"
            topics["findings"] = [{"topic_id": c} for c in categories[number]]
        return json.dumps(response).encode()


@pytest.fixture
def ready(tmp_path, request):
    fixture = make_fixture(tmp_path, conversations=2, messages=4)
    with fixture.repositories.database.transaction() as db:
        rows = db.execute(
            "SELECT message_id FROM account_messages ORDER BY message_id"
        ).fetchall()
        for index, row in enumerate(rows):
            db.execute(
                "UPDATE account_messages SET text=? WHERE message_id=?",
                (f"😀 echo echo case{index}", row[0]),
            )
    document = json.loads((FIXTURES / "joint-profile.json").read_bytes())
    component = document["components"][0]
    component["tasks"][0]["enabled_ids"].append("topic.second")
    component["executed_labels"].append(
        {
            "task": "message_topics",
            "label_id": "topic.second",
            "description": "Other synthetic label",
        }
    )
    category_case = getattr(request, "param", None) == "categories"
    if category_case:
        for identifier in ("concept.synthetic", "topic.partial", "topic.zero", "topic.disabled"):
            component["executed_labels"].append({"task": "message_topics", "label_id": identifier,
                                                 "description": "Synthetic category"})
            if identifier != "topic.disabled":
                component["tasks"][0]["enabled_ids"].append(identifier)
    profile = AnalysisProfile.model_validate_json(json.dumps(document))
    adapter = (CategoryAdapter if category_case else FixtureAdapter)(profile)

    def labels(identity):
        return (
            None
            if identity["category_id"] == "topic.second"
            else {**identity, "display_label": "Example label"}
        )

    runner = FindingsRunner(
        fixture.source,
        fixture.stores.projections,
        authorize=lambda account: None,
        execution_ready=lambda profile: True,
        adapter=adapter,
        clock=lambda: fixture.clock.now,
        enrichment=fixture.pipeline.enrichment,
        label_resolver=labels,
    )
    runner.findings.select(ACCOUNT, profile)
    for _ in range(8):
        assert runner.process_one(ACCOUNT)
    run = runner.publish_pending(ACCOUNT)
    assert run.changed
    fixture.source.prepare_question_identity(ACCOUNT)
    resources = QuestionResources(
        fixture.source, fixture.pipeline, clock=lambda: fixture.clock.now
    )
    policy = build_runtime_policy(
        AuthContext(
            "principal.synthetic",
            ACCOUNT,
            "creator",
            platform_creator_id="platform.synthetic",
            session_id="session.synthetic",
        )
    )
    request = FindingsRequest(
        expected_generation=run.artifact.projection.projection_generation,
        expected_projection_digest=run.artifact.projection.projection_digest,
    )
    result = SimpleNamespace(
        fixture=fixture,
        runner=runner,
        profile=profile,
        resources=resources,
        policy=policy,
        request=request,
        projection=run.artifact.projection,
    )
    try:
        yield result
    finally:
        resources.close()
        assert resources.wait_closed()
        cleanup(fixture)


def test_published_both_tasks_counts_coverage_labels_and_exact_repeated_spans(ready):
    response = ready.resources.findings(ready.policy, ready.request)
    topics, mentions = response.tasks
    assert topics.task == "message_topics" and mentions.task == "concept_mentions"
    assert topics.eligible_messages == mentions.eligible_messages == 8
    assert (
        topics.complete_messages,
        topics.partial_messages,
        topics.uncertain_messages,
        topics.failed_messages,
        topics.unsupported_messages,
        topics.pending_messages,
    ) == (3, 1, 1, 1, 1, 1)
    assert (
        mentions.complete_messages,
        mentions.partial_messages,
        mentions.uncertain_messages,
        mentions.failed_messages,
        mentions.unsupported_messages,
        mentions.pending_messages,
    ) == (3, 1, 1, 1, 1, 1)
    assert topics.rows[0].supporting_messages == 3
    assert topics.rows[0].complete_supporting_messages == 2
    assert topics.rows[0].share_of_complete_messages == 2 / 3
    assert (
        topics.rows[1].label_status == "id_only"
        and topics.rows[1].display_label == "topic.second"
    )
    assert topics.rows[0].label_status == "resolved"
    assert (
        mentions.rows[0].occurrences == 5 and mentions.rows[0].supporting_messages == 3
    )
    assert "text" not in response.model_dump_json()
    item = next(
        e
        for e in response.evidence_page.items
        if e.reference.span is not None and e.reference.span.start == 7
    )
    resolved = ready.resources.resolve(ready.policy, item.reference)
    assert resolved.text[7:11] == "echo"
    assert (resolved.browser_span.start, resolved.browser_span.end) == (8, 12)


def test_read_inventory_serialized_size_is_explicit(ready):
    from app.analytics.nlp_findings_store import encoded

    with ready.fixture.stores.database.read() as db:
        raw = bytes(db.execute("SELECT document FROM nlp_snapshots").fetchone()[0])
    document = json.loads(raw)
    with ready.fixture.stores.database.read() as db:
        rows = [
            item
            for row in db.execute("SELECT document FROM nlp_snapshot_source_pages")
            for item in json.loads(row[0])
        ]
    sizes = [len(encoded(item)) for item in rows]
    print(
        {
            "source_count": len(sizes),
            "minimum_source_bytes": min(sizes),
            "maximum_source_bytes": max(sizes),
            "snapshot_bytes": len(raw),
            "estimated_100000_source_bytes": 100000 * max(sizes),
        }
    )
    assert len(sizes) == 8 and max(sizes) < 2048


def test_schema30_trigger_identity(tmp_path):
    import hashlib

    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        with fixture.stores.database.read() as db:
            signatures = {
                r[0]: hashlib.sha256(r[1].encode()).hexdigest()
                for r in db.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
                )
            }
        for name, values in [
            ("all_guards", signatures),
            (
                "content",
                {
                    k: v
                    for k, v in signatures.items()
                    if k.startswith("generation_content_")
                },
            ),
        ]:
            print(
                name,
                hashlib.sha256(
                    json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
        from app.analytics.validation_receipt import content_stamp

        with fixture.stores.database.read() as db:
            assert content_stamp(db) is not None
    finally:
        cleanup(fixture)


def test_shared_published_wire_fixture(ready, tmp_path):
    response = ready.resources.findings(ready.policy, ready.request)
    start = ready.fixture.clock.now - timedelta(days=8) + timedelta(minutes=2)
    comparison = ready.resources.findings(
        ready.policy,
        ready.request.model_copy(
            update={"start_date": start, "end_date": start + timedelta(minutes=2)}
        ),
    )
    item = next(
        e
        for e in response.evidence_page.items
        if e.reference.span is not None and e.reference.span.start == 7
    )
    resolved = ready.resources.resolve(ready.policy, item.reference)
    values = {
        "findings-response.v1.json": response.model_dump(mode="json"),
        "findings-comparison.v1.json": comparison.model_dump(mode="json"),
        "findings-evidence.v1.json": {
            "account_id": ACCOUNT,
            "resolved_evidence": resolved.model_dump(mode="json"),
        },
    }
    for name, value in values.items():
        target = tmp_path / name
        target.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(target)
    assert comparison.comparison.availability == "available"


def test_filter_comparison_boundary_conversation_and_zero_denominator(ready):
    from app.analytics.opaque_refs import conversation_ref

    start = ready.fixture.clock.now - timedelta(days=8) + timedelta(minutes=2)
    plan = ready.request.model_copy(
        update={
            "start_date": start,
            "end_date": start + timedelta(minutes=2),
            "conversation_ref": conversation_ref(ACCOUNT, "chat-0"),
            "conversation_limit": 1,
        }
    )
    response = ready.resources.findings(ready.policy, plan)
    assert [t.eligible_messages for t in response.tasks] == [2, 2]
    assert [t.complete_messages for t in response.tasks] == [0, 0]
    assert response.tasks[0].rows[0].partial_supporting_messages == 1
    assert response.tasks[0].rows[0].share_of_complete_messages is None
    assert response.comparison.availability == "available"
    assert [t.complete_messages for t in response.comparison.tasks] == [2, 2]
    assert response.comparison.requested_window.end == start
    assert response.comparison.requested_window.start == start - timedelta(minutes=2)
    assert all(e.reference.sent_at >= start for e in response.evidence_page.items)
    point = ready.resources.findings(
        ready.policy, plan.model_copy(update={"end_date": start})
    )
    assert (
        point.tasks[0].eligible_messages == 1 and point.tasks[0].partial_messages == 1
    )
    assert point.comparison.unavailable_reason == "zero_width_window"
    assert (
        ready.resources.findings(
            ready.policy, ready.request
        ).comparison.unavailable_reason
        == "unbounded_window"
    )
    outbound = ready.resources.findings(
        ready.policy, ready.request.model_copy(update={"direction": "outbound"})
    )
    assert (
        outbound.tasks[0].eligible_messages == 4
        and outbound.tasks[0].complete_messages == 2
    )


def test_mutable_jobs_and_source_index_cannot_change_published_counts(ready):
    before = ready.resources.findings(ready.policy, ready.request)
    with ready.fixture.stores.database.transaction() as db:
        db.execute("DELETE FROM nlp_jobs")
        db.execute("UPDATE nlp_sources SET read_metadata='{}'")
    assert ready.resources.findings(ready.policy, ready.request) == before


@pytest.mark.parametrize("fault", ["missing", "bytes", "ordinal", "extra"])
def test_metadata_page_corruption_refuses_the_whole_read(ready, fault):
    with ready.fixture.stores.database.transaction() as db:
        db.execute("DROP TRIGGER nlp_source_page_immutable")
        db.execute("DROP TRIGGER nlp_source_page_delete")
        if fault == "missing":
            db.execute("DELETE FROM nlp_snapshot_source_pages")
        elif fault == "bytes":
            db.execute("UPDATE nlp_snapshot_source_pages SET document=?", (b"[]",))
        elif fault == "ordinal":
            db.execute("UPDATE nlp_snapshot_source_pages SET ordinal=2")
        else:
            db.execute(
                "INSERT INTO nlp_snapshot_source_pages(account_ref,snapshot_id,ordinal,document) SELECT account_ref,snapshot_id,1,document FROM nlp_snapshot_source_pages"
            )
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, ready.request)
    assert not ready.resources.evidence._entries


def test_generation_swap_during_read_cannot_mix_or_return_old_result(
    ready, monkeypatch
):
    store = ready.fixture.stores.projections
    original = store.read_findings_snapshot

    def swap(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.fixture.pipeline.project_account(ACCOUNT)
        return result

    monkeypatch.setattr(store, "read_findings_snapshot", swap)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, ready.request)
    assert not ready.resources.evidence._entries


def test_live_delete_and_expiry_revoke_summary_and_registered_evidence(ready):
    from app.persistence.retention import CreatorVaultRetention

    response = ready.resources.findings(ready.policy, ready.request)
    reference = response.evidence_page.items[0].reference
    CreatorVaultRetention(
        ready.fixture.repositories.database, clock=lambda: ready.fixture.clock.now
    ).delete_all(ACCOUNT)
    with pytest.raises(EvidenceUnavailable):
        ready.resources.resolve(ready.policy, reference)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, ready.request)


def test_expired_generation_does_not_return_saved_findings(ready):
    ready.fixture.clock.now += timedelta(days=100)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, ready.request)


def test_baseline_and_pre_inventory_snapshot_are_unavailable(ready, monkeypatch):
    with ready.fixture.stores.database.transaction() as db:
        db.execute("UPDATE nlp_sources SET read_metadata=''")
        db.execute(
            "UPDATE nlp_accounts SET availability_revision=availability_revision+1,notification_revision=notification_revision+1"
        )
    monkeypatch.setattr(ready.runner, "reconcile", lambda account: None)
    legacy = ready.runner.publish_pending(ACCOUNT)
    request = ready.request.model_copy(
        update={
            "expected_generation": legacy.artifact.projection.projection_generation,
            "expected_projection_digest": legacy.artifact.projection.projection_digest,
        }
    )
    with pytest.raises(ProjectionUnavailable) as error:
        ready.resources.findings(ready.policy, request)
    assert error.value.availability == "unavailable"
    baseline = ready.fixture.pipeline.project_account(ACCOUNT)
    request = request.model_copy(
        update={
            "expected_generation": baseline.artifact.projection.projection_generation,
            "expected_projection_digest": baseline.artifact.projection.projection_digest,
        }
    )
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, request)


@pytest.mark.parametrize("fault", ["identity", "control", "too_long"])
def test_display_labels_are_bounded_and_exactly_identity_bound(ready, fault):
    def resolve(identity):
        if fault == "identity":
            return {
                **identity,
                "taxonomy": {**identity["taxonomy"], "revision": "other"},
                "display_label": "Name",
            }
        return {
            **identity,
            "display_label": "not\nplain" if fault == "control" else "x" * 257,
        }

    with pytest.raises(ValueError):
        frozen_labels(ready.profile, resolve)


def test_source_pages_cascade_with_unreferenced_snapshot(ready):
    from app.analytics.identity import source_identity

    binding = ready.runner.findings.freeze(
        ACCOUNT, source_identity(ready.fixture.source, ACCOUNT)
    )
    with ready.fixture.stores.database.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM nlp_snapshot_source_pages WHERE snapshot_id=?",
                (binding.snapshot_id,),
            ).fetchone()[0]
            > 0
        )
        db.execute(
            "DELETE FROM nlp_snapshots WHERE snapshot_id=?", (binding.snapshot_id,)
        )
        assert (
            db.execute(
                "SELECT count(*) FROM nlp_snapshot_source_pages WHERE snapshot_id=?",
                (binding.snapshot_id,),
            ).fetchone()[0]
            == 0
        )


def test_optional_metadata_capacity_failure_preserves_core_publication(
    ready, monkeypatch
):
    import app.analytics.nlp_findings_store as storage
    from app.analytics.identity import source_identity

    with ready.fixture.stores.database.read() as db:
        used = db.execute("SELECT sum(length(document)) FROM nlp_snapshots").fetchone()[
            0
        ]
        used += db.execute(
            "SELECT sum(length(document)) FROM nlp_snapshot_source_pages"
        ).fetchone()[0]
    # Enough room for the exact original core input, insufficient for new read pages.
    with ready.fixture.stores.database.read() as db:
        core = json.loads(
            db.execute("SELECT document FROM nlp_snapshots").fetchone()[0]
        )
    core.pop("read_inventory")
    monkeypatch.setattr(
        storage, "MAX_SNAPSHOT_STORAGE", used + len(storage.encoded(core))
    )
    # Request a new frozen mode rather than reusing the existing exact snapshot.
    ready.runner.findings.mode(ACCOUNT, "paused")
    binding = ready.runner.findings.freeze(
        ACCOUNT, source_identity(ready.fixture.source, ACCOUNT)
    )
    with ready.fixture.stores.database.read() as db:
        value = json.loads(
            db.execute(
                "SELECT document FROM nlp_snapshots WHERE snapshot_id=?",
                (binding.snapshot_id,),
            ).fetchone()[0]
        )
    assert "read_inventory" not in value and len(value["records"]) == 8
    pipeline = ready.runner.frozen_pipeline(ACCOUNT)
    published = pipeline.publish_candidate(pipeline.build_candidate(ACCOUNT))
    assert published.changed
    request = ready.request.model_copy(
        update={
            "expected_generation": published.artifact.projection.projection_generation,
            "expected_projection_digest": published.artifact.projection.projection_digest,
        }
    )
    with pytest.raises(ProjectionUnavailable) as error:
        ready.resources.findings(ready.policy, request)
    assert error.value.availability == "unavailable"


@pytest.mark.parametrize("category_filtered", [False, True])
def test_full_source_capacity_is_counted_once_across_tasks_comparison_and_evidence(
    ready, category_filtered,
):
    import sqlite3
    from app.analytics.nlp_read_projection import (
        pack_source_pages,
        load_source_pages,
        aggregate,
    )
    from app.analytics.nlp_findings_store import checked_snapshot, FrozenFindings
    from app.analytics.opaque_refs import message_ref
    from app.analytics.query_execution import QuestionBudget, QuestionLimits

    with ready.fixture.stores.database.read() as db:
        generation = dict(
            db.execute(
                "SELECT * FROM projection_generations WHERE status='active'"
            ).fetchone()
        )
        key = db.execute(
            "SELECT snapshot_id FROM nlp_generation_refs WHERE generation_id=?",
            (generation["generation_id"],),
        ).fetchone()[0]
        records = {}
        value = checked_snapshot(
            db, FrozenFindings(account_ref(ACCOUNT), key), decoded_records=records
        )
        rows = [
            item
            for page in db.execute(
                "SELECT document FROM nlp_snapshot_source_pages WHERE snapshot_id=? ORDER BY ordinal",
                (key,),
            )
            for item in json.loads(page[0])
        ]
    prototype = rows[0]
    for number in range(100_000 - len(rows)):
        rows.append(
            ["capacity-chat", f"capacity-{number}", *prototype[2:6], -1, "pending"]
        )
    rows.sort(key=lambda row: message_ref(ACCOUNT, row[0], row[1]))
    header, pages = pack_source_pages(rows)
    value["read_inventory"].update(header)
    # A frozen test clock isolates count arithmetic from performance. Production
    # retains its five-second all-or-error deadline over the same helper path.
    budget = QuestionBudget(
        QuestionLimits(max_records=100_000, wall_clock_ms=5000), monotonic=lambda: 0
    )
    budget.consume(2)  # Existing canonical framing is not an inventory source.
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE nlp_snapshot_source_pages(account_ref,snapshot_id,ordinal,document)"
        )
        db.executemany(
            "INSERT INTO nlp_snapshot_source_pages VALUES(?,?,?,?)",
            [
                (account_ref(ACCOUNT), key, index, page)
                for index, page in enumerate(pages)
            ],
        )
        sources = load_source_pages(db, ACCOUNT, key, value, budget)
    plan = ready.request.model_copy(
        update={
            "start_date": ready.fixture.clock.now - timedelta(days=9),
            "end_date": ready.fixture.clock.now,
            "evidence_limit": 2,
            "category_filter": FindingsCategoryFilter(task="message_topics", category_id="topic.synthetic")
            if category_filtered else None,
        }
    )
    response, bindings = aggregate(
        value, records, generation, ready.projection, plan, budget, sources
    )
    assert all(task.eligible_messages == 100_000 for task in response.tasks)
    assert response.comparison.availability == "available"
    assert len(bindings) == 2 and response.evidence_page.total > 2
    assert budget.records_examined == 2
    assert sum(map(len, pages)) < 64 * 1024 * 1024


def test_pack_and_read_observe_deadline_and_cancellation(ready):
    from app.analytics.nlp_read_projection import pack_source_pages, load_source_pages
    from app.analytics.query_execution import (
        QuestionBudget,
        QuestionLimits,
        QuestionLimitExceeded,
        QuestionCancelled,
    )

    ticks = iter([0, 0, 6])
    budget = QuestionBudget(
        QuestionLimits(wall_clock_ms=5000), monotonic=lambda: next(ticks)
    )
    with pytest.raises(QuestionLimitExceeded):
        pack_source_pages([[0]] * 4, check=budget.check)
    budget = QuestionBudget(QuestionLimits(), cancellation_check=lambda: True)
    with ready.fixture.stores.database.read() as db:
        row = db.execute(
            "SELECT account_ref,snapshot_id,document FROM nlp_snapshots"
        ).fetchone()
        with pytest.raises(QuestionCancelled):
            load_source_pages(
                db, ACCOUNT, row["snapshot_id"], json.loads(row["document"]), budget
            )


@pytest.fixture
def api(ready, monkeypatch):
    from unittest.mock import AsyncMock
    from app.analytics.runtime import AnalyticsRuntime
    from app.api.activation import require_activated_runtime
    from app.api.dependencies import get_authenticated_account_session
    from app.api.endpoints import insights
    from app.api.security import csrf_token
    from app.services import insights_service

    scheduler = SimpleNamespace(request_recovery=AsyncMock())
    runtime = AnalyticsRuntime(
        ready.fixture.source, ready.fixture.pipeline, scheduler, ready.resources
    )
    monkeypatch.setattr(insights_service, "analytics_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[get_authenticated_account_session] = lambda: ready.policy
    app.dependency_overrides[require_activated_runtime] = lambda: None
    with TestClient(app) as client:
        client.headers["x-csrf-token"] = csrf_token(ready.policy)
        yield client, scheduler


def test_api_generation_fence_filters_and_existing_unicode_evidence(api, ready):
    client, scheduler = api
    query = ready.request.model_dump(mode="json", exclude_none=True)
    response = client.get("/api/v1/insights/findings", params=query)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["requested_window"]["start"] is None
    evidence = next(
        item["reference"]
        for item in response.json()["evidence_page"]["items"]
        if item["reference"]["span"] and item["reference"]["span"]["start"] == 7
    )
    resolved = client.post("/api/v1/insights/questions/evidence", json=evidence)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["text"][7:11] == "echo"
    mismatch = client.get(
        "/api/v1/insights/findings", params={**query, "expected_generation": 1000}
    )
    assert (
        mismatch.status_code == 503
        and mismatch.json()["detail"]["code"] == "analytics_projection_building"
    )
    assert mismatch.headers["cache-control"] == "no-store"
    scheduler.request_recovery.assert_not_called()
    for extra in (
        {"account_id": "other"},
        {"direction": "sideways"},
        {"evidence_limit": 101},
    ):
        assert (
            client.get(
                "/api/v1/insights/findings", params={**query, **extra}
            ).status_code
            == 422
        )
    duplicate = list(query.items()) + [("direction", "all")]
    assert client.get("/api/v1/insights/findings", params=duplicate).status_code == 422


def test_read_slot_and_shutdown_refuse_findings_without_execution(ready):
    from app.analytics.errors import ProjectionBackpressure

    assert ready.resources._slots.acquire(blocking=False)
    assert ready.resources._slots.acquire(blocking=False)
    try:
        with pytest.raises(ProjectionBackpressure):
            ready.resources.findings(ready.policy, ready.request)
    finally:
        ready.resources._slots.release()
        ready.resources._slots.release()
    ready.resources.close()
    assert ready.resources.wait_closed()
    with pytest.raises(ProjectionUnavailable):
        ready.resources.findings(ready.policy, ready.request)


@pytest.mark.parametrize("ready", ["categories"], indirect=True)
def test_category_pages_preserve_population_and_filter_before_pagination(ready):
    from app.analytics.opaque_refs import conversation_ref, message_ref

    start = ready.fixture.clock.now - timedelta(days=8)
    plan = ready.request.model_copy(update={"start_date": start, "end_date": start + timedelta(hours=2)})
    full = ready.resources.findings(ready.policy, plan)
    selected = plan.model_copy(update={
        "category_filter": FindingsCategoryFilter(task="message_topics", category_id="topic.synthetic"),
        "evidence_limit": 1, "evidence_offset": 1,
        "conversation_limit": 1, "conversation_offset": 1,
    })
    response = ready.resources.findings(ready.policy, selected)
    assert response.schema_version == "analytics-findings.v3"
    assert response.category_filter == selected.category_filter
    assert response.tasks == full.tasks
    assert response.comparison == full.comparison
    assert response.effective_window == full.effective_window
    assert response.tasks[0].complete_messages == 4
    assert response.tasks[0].rows[0].share_of_complete_messages == 1 / 2
    chats = sorted(conversation_ref(ACCOUNT, f"chat-{i}") for i in range(2))
    assert response.conversation_page.total == 2
    assert [(c.conversation_ref, c.eligible_messages) for c in response.conversation_page.items] == [(chats[1], 4)]
    expected = sorted(message_ref(ACCOUNT, f"chat-{i}", f"m-{i}-0") for i in range(2))
    assert response.evidence_page.total == 2
    assert [e.reference.message_ref for e in response.evidence_page.items] == [expected[1]]
    assert all(e.task == "message_topics" and e.category_id == "topic.synthetic" for e in response.evidence_page.items)

    conversation = selected.model_copy(update={"conversation_ref": chats[0], "conversation_offset": 0, "evidence_offset": 0})
    narrowed = ready.resources.findings(ready.policy, conversation)
    original = ready.resources.findings(ready.policy, conversation.model_copy(update={"category_filter": None}))
    assert narrowed.tasks == original.tasks and narrowed.comparison == original.comparison
    assert narrowed.effective_window == original.effective_window
    assert narrowed.conversation_page.total == 2
    assert all(e.reference.conversation_ref == chats[0] for e in narrowed.evidence_page.items)


@pytest.mark.parametrize("ready", ["categories"], indirect=True)
def test_category_partial_zero_and_same_identifier_in_other_task(ready):
    from app.analytics.opaque_refs import conversation_ref, message_ref

    full = ready.resources.findings(ready.policy, ready.request)
    def read(task, identifier):
        return ready.resources.findings(ready.policy, ready.request.model_copy(update={
            "category_filter": FindingsCategoryFilter(task=task, category_id=identifier)}))
    partial = read("message_topics", "topic.partial")
    assert [(e.reference.message_ref, e.assessment) for e in partial.evidence_page.items] == [(message_ref(ACCOUNT, "chat-0", "m-0-2"), "partial")]
    assert [(c.conversation_ref, c.eligible_messages) for c in partial.conversation_page.items] == [(conversation_ref(ACCOUNT, "chat-0"), 4)]
    zero = read("message_topics", "topic.zero")
    assert zero.evidence_page.total == zero.conversation_page.total == 0
    assert zero.tasks == partial.tasks == full.tasks
    assert zero.comparison == partial.comparison == full.comparison
    for identifier, chat in (("topic.partial", "chat-0"), ("topic.second", "chat-1")):
        for offset in (0, 1):
            page = ready.resources.findings(ready.policy, ready.request.model_copy(update={
                "category_filter": FindingsCategoryFilter(task="message_topics", category_id=identifier),
                "conversation_limit": 1, "conversation_offset": offset})).conversation_page
            assert page.total == 1
            assert [c.conversation_ref for c in page.items] == ([conversation_ref(ACCOUNT, chat)] if offset == 0 else [])
            assert all(c.eligible_messages == 4 for c in page.items)
    topic = read("message_topics", "concept.synthetic")
    mentions = read("concept_mentions", "concept.synthetic")
    assert [e.reference.message_ref for e in topic.evidence_page.items] == [message_ref(ACCOUNT, "chat-0", "m-0-1")]
    assert mentions.evidence_page.total == 5
    assert all(e.task == "concept_mentions" and e.reference.span is not None for e in mentions.evidence_page.items)
    for identifier in ("topic.disabled", "unknown"):
        with pytest.raises(InvalidAnalyticsRequest):
            read("message_topics", identifier)


def test_category_endpoint_pairs_strict_queries_and_response_version(api, ready):
    client, _ = api
    query = ready.request.model_dump(mode="json", exclude_none=True)
    pair = {"category_task": "message_topics", "category_id": "topic.synthetic"}
    response = client.get("/api/v1/insights/findings", params={**query, **pair})
    assert response.status_code == 200, response.text
    assert response.json()["schema_version"] == "analytics-findings.v3"
    assert response.json()["category_filter"] == {"task": pair["category_task"], "category_id": pair["category_id"]}
    assert len(response.json()["task_availability"]) == 2
    assert client.get("/api/v1/insights/findings", params=query).json()["schema_version"] == "analytics-findings.v1"
    for extra in ({"category_task": "message_topics"}, {"category_id": "topic.synthetic"},
                  {**pair, "category_task": "unknown"}, {**pair, "category_id": "unknown"},
                  {**pair, "unexpected": "value"}):
        invalid = client.get("/api/v1/insights/findings", params={**query, **extra})
        assert invalid.status_code == 422, invalid.text
        assert invalid.json()["detail"]["code"] == "analytics_findings_request_invalid"
    for key in pair:
        duplicate = list({**query, **pair}.items()) + [(key, pair[key])]
        assert client.get("/api/v1/insights/findings", params=duplicate).status_code == 422


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_category_filter_observes_shared_budget_after_summary(ready, monkeypatch, interruption):
    from app.analytics import nlp_read_projection as projection
    from app.analytics.query_execution import QuestionBudget, QuestionCancelled, QuestionLimitExceeded, QuestionLimits

    interrupted = False
    summarize = projection._summarize
    def complete_summary(*args, **kwargs):
        nonlocal interrupted
        result = summarize(*args, **kwargs)
        interrupted = True
        return result
    monkeypatch.setattr(projection, "_summarize", complete_summary)
    budget = QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=5000),
        cancellation_check=lambda: interrupted and interruption == "cancel",
        monotonic=lambda: 6 if interrupted and interruption == "deadline" else 0)
    monkeypatch.setattr(projection, "QuestionBudget", lambda *args, **kwargs: budget)
    plan = ready.request.model_copy(update={"category_filter":
        FindingsCategoryFilter(task="message_topics", category_id="topic.synthetic")})
    error = QuestionCancelled if interruption == "cancel" else QuestionLimitExceeded
    with pytest.raises(error):
        ready.resources.findings(ready.policy, plan)
    assert not ready.resources.evidence._entries
