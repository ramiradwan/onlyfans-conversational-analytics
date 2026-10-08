"""Bound acquisition coverage by cutoff without inventing source message semantics."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.errors import CanonicalRevisionChanged, ProjectionUnavailable
from app.analytics.identity import canonical_identity
from app.analytics.query_canonical import CanonicalQuestionScope
from app.analytics.query_contracts import QuestionPlan
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.analytics.query_handlers import no_later_creator_reply
from app.analytics.source_coverage import AcquisitionCoverage, question_coverage
from tests.analytics_coverage_fixture import seed_coverage
from tests.test_analytics_evidence import stored, ACCOUNT, OTHER, NOW


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
CHATS = ("synthetic-chat-a", "synthetic-chat-b")


def question(*, cutoff=NOW, clipped=False, kind="no_later_creator_reply.v1"):
    return SimpleNamespace(plan=QuestionPlan(question=kind, timezone="UTC",
        start=NOW-timedelta(days=1), end=NOW, cutoff=cutoff), cutoff=cutoff,
        retention_cutoff_exclusive=NOW-timedelta(days=90), selection_clipped_by_retention=clipped)


def facts(stored, requested=None):
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with stored.database.read() as db:
        scope = CanonicalQuestionScope(db, ACCOUNT, budget)
        return list(scope.conversations(requested or question(), budget))


@pytest.fixture
def covered(stored):
    with stored.database.transaction() as db:
        seed_coverage(db, ACCOUNT, CHATS, NOW)
    return stored


@pytest.mark.parametrize("change,cutoff,clipped,expected", [
    ({}, NOW, False, "complete"),
    ({}, NOW-timedelta(microseconds=1), False, "complete"),
    ({}, NOW+timedelta(microseconds=1), False, "partial"),
    ({}, NOW, True, "partial"),
    ({"state": "backfilling"}, NOW, False, "partial"),
    ({"state": "partial"}, NOW, False, "partial"),
    ({"state": "superseded"}, NOW, False, "partial"),
    ({"inventory_ended_at": None}, NOW, False, "partial"),
    ({"closed_at": None}, NOW, False, "partial"),
    ({"history_started_at": None}, NOW, False, "partial"),
    ({"head_reconciled_through": None}, NOW, False, "partial"),
    ({"head_reconciled_through": (NOW-timedelta(microseconds=1)).isoformat()}, NOW, False, "partial"),
    ({"head_reconciled_through": (NOW+timedelta(days=1)).isoformat()}, NOW+timedelta(seconds=1), False, "partial"),
    ({"as_of": NOW.astimezone(timezone(timedelta(hours=3))).isoformat()}, NOW, False, "complete"),
])
def test_cutoff_uses_generation_as_of(change, cutoff, clipped, expected):
    evidence = dict(generation_id="synthetic", state="complete", as_of=NOW.isoformat(),
        inventory_ended_at=NOW.isoformat(), closed_at=NOW.isoformat(),
        history_started_at=NOW.isoformat(), head_reconciled_through=NOW.isoformat())
    evidence.update(change)
    assert question_coverage(evidence, SimpleNamespace(
        cutoff=cutoff, selection_clipped_by_retention=clipped)) == expected
    assert question_coverage(None, question()) == "unknown"


@pytest.mark.parametrize("kind", ["no_later_creator_reply.v1", "pricing_discussions.v1"])
def test_both_canonical_readers_use_coverage_without_promoting_kinds(covered, kind):
    values = facts(covered, question(kind=kind))
    assert len(values) == 2 and {item.coverage for item in values} == {"complete"}
    assert all(message.kind == "unknown" and message.ordering == "inferred" and message.order is None
               for item in values for message in item.messages)
    assert {item.coverage for item in facts(covered, question(
        kind=kind, cutoff=NOW+timedelta(seconds=1)))} == {"partial"}


def test_generation_absence_and_missing_member_are_distinct(stored):
    source = HistoryAnalyticsSource(stored.history)
    before = source.analytics_snapshot(ACCOUNT)
    assert {item.coverage for item in facts(stored)} == {"unknown"}
    with stored.database.transaction() as db:
        seed_coverage(db, ACCOUNT, CHATS[:1], NOW)
    assert sorted(item.coverage for item in facts(stored)) == ["complete", "partial"]
    assert source.analytics_snapshot(ACCOUNT).identity != before.identity
    assert source.verify_identity_proof(ACCOUNT, before.identity, before.identity_proof) is False


def test_current_generation_never_borrows_an_old_member_or_stream_order(covered):
    with covered.database.transaction() as db:
        seed_coverage(db, ACCOUNT, (), NOW, generation="new-generation", state="partial")
        db.execute("UPDATE account_coverage_heads SET last_complete_generation_id='synthetic-coverage'")
        db.execute("UPDATE account_messages SET winning_stream_epoch=99,winning_source_seq=100")
    assert {item.coverage for item in facts(covered)} == {"partial"}


def test_empty_chats_do_not_create_evaluated_conversations(covered):
    with covered.database.transaction() as db:
        db.execute("DELETE FROM account_messages WHERE creator_account_id=?", (ACCOUNT,))
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with covered.database.read() as db:
        scope = CanonicalQuestionScope(db, ACCOUNT, budget)
        page = no_later_creator_reply(scope, question(), None, budget)
    assert not page.rows and page.evaluated_conversation_count == 0
    assert page.coverage.history == "unknown"


def test_coverage_lookup_uses_the_question_budget(covered):
    budget = QuestionBudget(QuestionLimits(max_records=1, wall_clock_ms=30_000))
    with covered.database.read() as db:
        reader = AcquisitionCoverage(db, ACCOUNT, consume=budget.consume)
        with pytest.raises(QuestionLimitExceeded):
            reader.conversation(CHATS[0])


@pytest.mark.parametrize("sql", [
    "UPDATE coverage_members SET history_started_at=NULL WHERE creator_account_id=?",
    "UPDATE coverage_members SET head_reconciled_through=NULL WHERE creator_account_id=?",
    "DELETE FROM coverage_members WHERE creator_account_id=?",
    "UPDATE coverage_generations SET as_of='2026-09-17T12:00:00+00:00' WHERE creator_account_id=?",
    "UPDATE coverage_generations SET state='partial' WHERE creator_account_id=?",
    "UPDATE coverage_generations SET inventory_ended_at=NULL WHERE creator_account_id=?",
    "UPDATE coverage_generations SET closed_at=NULL WHERE creator_account_id=?",
    "DELETE FROM coverage_generations WHERE creator_account_id=?",
    "UPDATE account_coverage_heads SET active_generation_id=NULL WHERE creator_account_id=?",
    "DELETE FROM account_coverage_heads WHERE creator_account_id=?",
])
def test_coverage_changes_invalidate_catalog_and_proof_without_revision_change(covered, sql):
    source = HistoryAnalyticsSource(covered.history)
    before = source.analytics_snapshot(ACCOUNT)
    other = source.analytics_snapshot(OTHER)
    with covered.database.transaction() as db:
        db.execute(sql, (ACCOUNT,))
    after = source.analytics_snapshot(ACCOUNT)
    assert before.identity.revision == after.identity.revision
    assert before.identity != after.identity
    assert source.verify_identity_proof(ACCOUNT, before.identity, before.identity_proof) is False
    assert after.identity == canonical_identity(source.account_read_model(ACCOUNT))
    assert after.conversation(CHATS[0]) == source.account_read_model(ACCOUNT).conversations[CHATS[0]]
    with pytest.raises(CanonicalRevisionChanged):
        before.conversation(CHATS[0])
    assert source.analytics_snapshot(OTHER).identity == other.identity


def test_coverage_rollback_preserves_a_verified_catalog(covered):
    source = HistoryAnalyticsSource(covered.history)
    before = source.analytics_snapshot(ACCOUNT)
    with covered.database.read() as db:
        db.execute("BEGIN")
        db.execute("UPDATE coverage_members SET history_started_at=NULL")
        supplied = HistoryAnalyticsSource(covered.history, connection=db)
        assert supplied.analytics_snapshot(ACCOUNT).identity != before.identity
        db.rollback()
    assert source.analytics_snapshot(ACCOUNT).identity == before.identity
    assert source.verify_identity_proof(ACCOUNT, before.identity, before.identity_proof) is True


def test_concurrent_coverage_change_refuses_the_pinned_scope(covered):
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with covered.database.read() as db:
        scope = CanonicalQuestionScope(db, ACCOUNT, budget)
        with covered.database.transaction() as changed:
            changed.execute("UPDATE coverage_members SET history_started_at=NULL")
        with pytest.raises(ProjectionUnavailable):
            list(scope.conversations(question(), budget))


def test_catchup_current_does_not_extend_historical_completeness(covered):
    source = HistoryAnalyticsSource(covered.history)
    before = source.analytics_snapshot(ACCOUNT)
    with covered.database.transaction() as db:
        db.execute("""INSERT INTO message_catchup(creator_account_id,gap_open,observing,last_closed_at)
            VALUES (?,0,1,?) ON CONFLICT(creator_account_id) DO UPDATE SET
            gap_open=0,observing=1,last_closed_at=excluded.last_closed_at""", (ACCOUNT, NOW.isoformat()))
    assert source.analytics_snapshot(ACCOUNT).identity == before.identity
    assert {item.coverage for item in facts(covered, question(cutoff=NOW+timedelta(seconds=1)))} == {"partial"}


def test_missing_coverage_tracking_refuses_prepared_questions(covered):
    source = HistoryAnalyticsSource(covered.history)
    source.prepare_question_identity(ACCOUNT)
    with covered.database.transaction() as db:
        db.execute("DROP TRIGGER analytics_source_token_coverage_members_update")
    with pytest.raises(ProjectionUnavailable):
        source.prepare_question_identity(ACCOUNT)


@pytest.mark.asyncio
async def test_accepted_acquisition_reaches_scheduled_production_questions(tmp_path):
    from uuid import uuid4
    from app.analytics.factory import create_analytics_stores
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.persistence.factory import create_canonical_repositories
    from tests.test_analytics_evidence import policy
    from tests.test_history_v2 import (
        ACCOUNT_ID, authorize_history, commit_base_snapshot, delta, raw_message,
    )

    at = datetime.now(timezone.utc) + timedelta(days=1)
    stored = create_canonical_repositories("sqlite", canonical_path=tmp_path/"canonical.sqlite3")
    key, _ = commit_base_snapshot(stored.history,
        messages=[raw_message("acquired", sent_at=(at-timedelta(hours=1)).isoformat())])
    authorize_history(stored.history)
    generation = str(uuid4())
    events = [
        dict(type="generation.started", as_of=at.isoformat(), authorization_revision="consent-1"),
        dict(type="inventory.member", conversation_id="chat-1"),
        dict(type="inventory.ended", observed_at=at.isoformat()),
        dict(type="conversation.history_started", conversation_id="chat-1",
             earliest_observed_at=(at-timedelta(hours=1)).isoformat(), observed_at=at.isoformat()),
        dict(type="conversation.head_reconciled", conversation_id="chat-1", reconciled_through=at.isoformat()),
        dict(type="generation.closed", closed_at=at.isoformat()),
    ]
    for index, event in enumerate(events, 1):
        result = stored.history.commit_delta(key, delta(index,
            dict(type="coverage.observed", evidence={**event, "generation_id": generation})))
        assert result.status == "accepted"
    source = HistoryAnalyticsSource(stored.history)
    stores = create_analytics_stores("sqlite", projections_path=tmp_path/"analytics.sqlite3",
        activation=stored.projection_activation, canonical_identity_reader=source.read_identity,
        retention_clock=lambda: at)
    pipeline = AnalyticsPipeline(source, projections=stores.projections, clock=lambda: at)
    scheduler = InProcessProjectionScheduler(pipeline)
    resources = QuestionResources(source, pipeline, clock=lambda: at)
    try:
        await scheduler.schedule(ACCOUNT_ID, stored.history.account_revision(ACCOUNT_ID)[0])
        assert (await scheduler.wait(ACCOUNT_ID)).availability.value == "available"
        source.prepare_question_identity(ACCOUNT_ID)
        result = resources.execute(policy(ACCOUNT_ID), QuestionPlan(question="no_later_creator_reply.v1",
            timezone="UTC", start=at-timedelta(days=1), end=at, cutoff=at))
        assert result.page.coverage.history == "complete"
        assert result.page.undetermined_conversation_count == 1 and not result.page.rows
    finally:
        resources.close()
        await scheduler.close()
        stores.projections.close_retention_scheduler()
