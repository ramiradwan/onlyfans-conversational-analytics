"""Question identity preparation belongs to cancellable scheduler work, not reads."""

import asyncio
from datetime import timedelta
from threading import Event, current_thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analytics.errors import ProjectionUnavailable, ProjectionBuildCancelled
from app.analytics.identity import canonical_identity
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.analytics.query_contracts import QuestionPlan
from app.analytics.scheduling import InProcessProjectionScheduler
from tests.continuous_analytics_fixture import ACCOUNT, NOW, cleanup, make_fixture
from tests.analytics_coverage_fixture import seed_coverage

pytestmark = [pytest.mark.ci_tier('integration')]


def cooperative_work(sleep):
    from app.analytics.question_work import QuestionWork
    ticks = [0.0]

    def clock():
        ticks[0] += 0.003
        return ticks[0]

    return QuestionWork(clock=clock, sleep=sleep)


@pytest.fixture
def source_fixture(tmp_path, monkeypatch):
    import app.analytics.source_snapshot as snapshots
    fixture = make_fixture(tmp_path)
    fixture.now = [0.0]
    fixture.source._identity_cache._clock = lambda: fixture.now[0]
    fixture.scan = Mock(wraps=snapshots.scan_identity)
    monkeypatch.setattr(snapshots, "scan_identity", fixture.scan)
    yield fixture
    cleanup(fixture)


def bounded_scope(source):
    return source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=2)))


def coverage_question(kind):
    return SimpleNamespace(plan=QuestionPlan(question=kind, timezone="UTC",
        start=NOW-timedelta(days=10), end=NOW, cutoff=NOW), cutoff=NOW,
        retention_cutoff_exclusive=NOW-timedelta(days=90), selection_clipped_by_retention=False)


@pytest.mark.parametrize("kind", ["no_later_creator_reply.v1", "pricing_discussions.v1"])
def test_conversation_coverage_is_lazily_charged_to_the_same_budget(source_fixture, kind):
    f = source_fixture
    with f.repositories.database.transaction() as db:
        seed_coverage(db, ACCOUNT, [f"chat-{index}" for index in range(3)], NOW)
    f.source.prepare_question_identity(ACCOUNT)
    statements = []
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with f.source.open_question_scope(ACCOUNT, budget) as scope:
        assert budget.records_examined == 2
        scope.connection.set_trace_callback(statements.append)
        rows = scope.conversations(coverage_question(kind), budget)
        assert next(rows).coverage == "complete"
        first_conversation_records = budget.records_examined
        assert len(list(rows)) == 2
    assert sum("FROM account_coverage_heads" in sql for sql in statements) == 1
    assert sum("FROM coverage_members" in sql for sql in statements) == 3

    statements.clear()
    budget = QuestionBudget(QuestionLimits(max_records=first_conversation_records-1,
                                          wall_clock_ms=30_000))
    with pytest.raises(QuestionLimitExceeded):
        with f.source.open_question_scope(ACCOUNT, budget) as scope:
            assert budget.records_examined == 2
            scope.connection.set_trace_callback(statements.append)
            next(scope.conversations(coverage_question(kind), budget))
    assert sum("FROM account_coverage_heads" in sql for sql in statements) == 1
    assert not any("FROM coverage_members" in sql for sql in statements)


def test_coverage_cache_stays_with_its_scope_and_source_identity(source_fixture):
    f = source_fixture
    with f.repositories.database.transaction() as db:
        seed_coverage(db, ACCOUNT, [f"chat-{index}" for index in range(3)], NOW)
    original = f.source.prepare_question_identity(ACCOUNT)
    question = coverage_question("no_later_creator_reply.v1")
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with f.source.open_question_scope(ACCOUNT, budget) as scope:
        assert {row.coverage for row in scope.conversations(question, budget)} == {"complete"}
    with f.repositories.database.transaction() as db:
        db.execute("UPDATE coverage_members SET history_started_at=NULL WHERE creator_account_id=?", (ACCOUNT,))
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    refreshed = f.source.prepare_question_identity(ACCOUNT)
    assert refreshed.revision == original.revision and refreshed != original
    budget = QuestionBudget(QuestionLimits(wall_clock_ms=30_000))
    with f.source.open_question_scope(ACCOUNT, budget) as scope:
        assert {row.coverage for row in scope.conversations(question, budget)} == {"partial"}


@pytest.mark.parametrize("expired", [False, True])
def test_unprepared_questions_do_not_attempt_an_inline_scan(source_fixture, monkeypatch, expired):
    import app.analytics.query_identity as identity
    f = source_fixture
    if expired:
        f.source.read_identity(ACCOUNT)
        f.now[0] = 61
    scan = Mock(wraps=identity.scan_identity)
    monkeypatch.setattr(identity, "scan_identity", scan)
    for _ in range(3):
        with pytest.raises(ProjectionUnavailable) as error:
            with bounded_scope(f.source):
                pytest.fail("unprepared identity reached a question")
        assert error.value.availability == "building"
    assert scan.call_count == 0


def test_preparation_renews_only_after_an_independent_scan(source_fixture):
    f = source_fixture
    expected = canonical_identity(f.source.account_read_model(ACCOUNT))
    assert f.source.prepare_question_identity(ACCOUNT) == expected
    assert f.scan.call_count == 1
    f.now[0] = 29
    assert f.source.prepare_question_identity(ACCOUNT) == expected
    assert f.scan.call_count == 1
    f.now[0] = 30
    assert f.source.prepare_question_identity(ACCOUNT) == expected
    assert f.scan.call_count == 2
    f.now[0] = 61
    with bounded_scope(f.source) as scope:
        assert scope.identity == expected
    f.now[0] = 90
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    assert f.scan.call_count == 2


@pytest.mark.asyncio
async def test_reconciliation_prepares_before_expiry_on_owned_thread(source_fixture, monkeypatch):
    f = source_fixture
    f.pipeline.project_account(ACCOUNT)
    f.scan.reset_mock()
    seen = []
    original = f.source.prepare_question_identity
    def observed(*args, **kwargs):
        seen.append(current_thread().name)
        return original(*args, **kwargs)
    monkeypatch.setattr(f.source, "prepare_question_identity", observed)
    scheduler = InProcessProjectionScheduler(f.pipeline)
    try:
        await scheduler.start()
        f.now[0] = 30
        await scheduler.reconcile_once()
        f.now[0] = 61
        with bounded_scope(f.source):
            pass
        assert f.scan.call_count == 1
        assert seen and all(name.startswith("analytics-projection-") for name in seen)
    finally:
        assert await scheduler.close(timeout=10)
        assert scheduler.detached_worker_count == 0


def test_cancelled_refresh_does_not_replace_or_extend_identity(source_fixture, monkeypatch):
    f = source_fixture
    expected = f.source.read_identity(ACCOUNT)
    f.now[0] = 30
    stop = Event()
    scan = f.scan
    def cancel(*args, **kwargs):
        stop.set()
        return scan(*args, **kwargs)
    monkeypatch.setattr("app.analytics.source_snapshot.scan_identity", cancel)
    with pytest.raises(ProjectionBuildCancelled):
        f.source.prepare_question_identity(ACCOUNT, cancellation_check=stop.is_set)
    with bounded_scope(f.source) as scope:
        assert scope.identity == expected
    f.now[0] = 60
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass


def test_preparation_is_account_scoped(source_fixture):
    f = source_fixture
    f.source.prepare_question_identity(ACCOUNT)
    assert f.source.prepare_question_identity("another-account") is None
    assert f.scan.call_count == 1


@pytest.mark.asyncio
async def test_query_recovery_prepares_without_rebuilding(source_fixture, monkeypatch):
    f = source_fixture
    f.pipeline.project_account(ACCOUNT)
    f.now[0] = 61
    scheduler = InProcessProjectionScheduler(f.pipeline)
    def unexpected_build(*args, **kwargs):
        pytest.fail("an unchanged publication must not rebuild for identity preparation")
    monkeypatch.setattr(f.pipeline, "build_candidate", unexpected_build)
    try:
        await scheduler.request_recovery(ACCOUNT, 1)
        await scheduler._recovery_task
        assert scheduler.state(ACCOUNT).availability.value == "available"
        with bounded_scope(f.source):
            pass
    finally:
        assert await scheduler.close(timeout=10)
        assert scheduler.detached_worker_count == 0


@pytest.mark.asyncio
async def test_scheduler_close_cancels_identity_scan(source_fixture, monkeypatch):
    f = source_fixture
    f.pipeline.project_account(ACCOUNT)
    f.now[0] = 61
    entered = Event()
    def waiting_scan(*args, **kwargs):
        entered.set()
        while True:
            kwargs["check"]()
            Event().wait(0.005)
    monkeypatch.setattr("app.analytics.source_snapshot.scan_identity", waiting_scan)
    scheduler = InProcessProjectionScheduler(f.pipeline)
    task = asyncio.create_task(scheduler.reconcile_once())
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert await scheduler.close(timeout=5)
        await task
        assert scheduler.detached_worker_count == 0
        assert scheduler.executor_thread_count == 0
    finally:
        await scheduler.close(timeout=5)
        await asyncio.gather(task, return_exceptions=True)


def test_missing_tracking_cannot_prepare_or_use_a_cached_identity(source_fixture, monkeypatch):
    f = source_fixture
    f.source.prepare_question_identity(ACCOUNT)
    monkeypatch.setattr(f.source._identity_cache, "token", lambda *args: None)
    with pytest.raises(ProjectionUnavailable) as error:
        f.source.prepare_question_identity(ACCOUNT)
    assert error.value.availability == "error"
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    assert f.scan.call_count == 1


def test_cancelled_lock_wait_does_not_start_another_scan(source_fixture):
    f = source_fixture
    cancelled = Event()
    cancelled.set()
    with f.source._question_preparation_lock:
        with pytest.raises(ProjectionBuildCancelled):
            f.source.prepare_question_identity(ACCOUNT, cancellation_check=cancelled.is_set)
    assert f.scan.call_count == 0


@pytest.mark.asyncio
async def test_identity_upkeep_is_not_blocked_by_slow_projection_reconciliation(source_fixture, monkeypatch):
    f = source_fixture
    f.pipeline.project_account(ACCOUNT)
    scheduler = InProcessProjectionScheduler(f.pipeline, reconciliation_interval=0.01)
    release = asyncio.Event()
    entered = asyncio.Event()
    async def delayed_reconciliation():
        entered.set()
        await release.wait()
    monkeypatch.setattr(scheduler, "reconcile_once", delayed_reconciliation)
    starting = asyncio.create_task(scheduler.start())
    try:
        await entered.wait()
        f.now[0] = 61
        async with asyncio.timeout(2):
            while True:
                try:
                    with bounded_scope(f.source):
                        break
                except ProjectionUnavailable:
                    await asyncio.sleep(0.01)
    finally:
        release.set()
        await starting
        assert await scheduler.close(timeout=5)


def test_changed_token_after_scan_cannot_publish_prepared_identity(source_fixture, monkeypatch):
    from dataclasses import replace
    from app.analytics.errors import CanonicalRevisionChanged
    f = source_fixture
    original = f.source._identity_cache.token
    calls = []
    def changing_token(db, account):
        value = original(db, account)
        calls.append(value)
        return value if len(calls) == 1 else replace(value, value="changed-during-scan")
    monkeypatch.setattr(f.source._identity_cache, "token", changing_token)
    with pytest.raises(CanonicalRevisionChanged):
        f.source.prepare_question_identity(ACCOUNT)
    assert f.scan.call_count == 1 and len(calls) == 2
    assert f.source._identity_cache.get(ACCOUNT, calls[0]) is None


def test_readiness_rechecks_the_prepared_identity_after_publication_validation(source_fixture, monkeypatch):
    from app.analytics.identity import CanonicalIdentity
    f = source_fixture
    f.pipeline.project_account(ACCOUNT)
    identity = f.source.read_identity(ACCOUNT)
    changed = CanonicalIdentity(identity.revision, "sha256:" + "0" * 64)
    monkeypatch.setattr(f.source, "prepare_question_identity", Mock(side_effect=[identity, changed]))
    assert not f.pipeline.prepare_questions(ACCOUNT, identity.revision)


@pytest.mark.parametrize("mutation", ["edit", "delete", "schema"])
def test_committed_changes_are_rechecked_before_preparation(source_fixture, mutation):
    f = source_fixture
    previous = f.source.prepare_question_identity(ACCOUNT)
    with f.repositories.database.transaction() as db:
        if mutation == "edit":
            db.execute("UPDATE account_messages SET text=? WHERE message_id=?",
                       ("Synthetic changed text", "m-1-1"))
        elif mutation == "delete":
            db.execute("DELETE FROM account_messages WHERE message_id=?", ("m-1-1",))
        else:
            db.execute("DROP TRIGGER analytics_source_token_account_messages_update")
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    if mutation == "schema":
        with pytest.raises(ProjectionUnavailable) as error:
            f.source.prepare_question_identity(ACCOUNT)
        assert error.value.availability == "error"
    else:
        expected = canonical_identity(f.source.account_read_model(ACCOUNT))
        assert expected != previous
        assert f.source.prepare_question_identity(ACCOUNT) == expected
        assert f.scan.call_count == 2


def test_cooperative_refresh_preserves_expiry_while_scan_is_paused(source_fixture):
    f = source_fixture
    expected = f.source.prepare_question_identity(ACCOUNT)
    f.now[0] = 30
    pauses = []

    def pause(duration):
        pauses.append(duration)
        if len(pauses) != 1:
            return
        with bounded_scope(f.source) as scope:
            assert scope.identity == expected
        f.now[0] = 60
        with pytest.raises(ProjectionUnavailable):
            with bounded_scope(f.source):
                pytest.fail("an incomplete refresh extended the old identity")

    f.source._question_work = cooperative_work(pause)
    with f.source._question_work.foreground():
        assert f.source.prepare_question_identity(ACCOUNT) == expected
    assert pauses and f.scan.call_count == 2
    assert expected == canonical_identity(f.source.account_read_model(ACCOUNT))
    f.now[0] = 119
    with bounded_scope(f.source) as scope:
        assert scope.identity == expected
    f.now[0] = 120
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass


def test_cancellation_at_cooperative_pause_does_not_renew_identity(source_fixture):
    f = source_fixture
    expected = f.source.prepare_question_identity(ACCOUNT)
    f.now[0] = 30
    stop = Event()
    f.source._question_work = cooperative_work(lambda duration: stop.set())
    with f.source._question_work.foreground():
        with pytest.raises(ProjectionBuildCancelled):
            f.source.prepare_question_identity(ACCOUNT, cancellation_check=stop.is_set)
    assert not f.source._question_preparation_lock.locked()
    with bounded_scope(f.source) as scope:
        assert scope.identity == expected
    f.now[0] = 60
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    assert f.source.prepare_question_identity(ACCOUNT) == expected


@pytest.mark.parametrize("mutation", ["edit", "delete", "schema"])
def test_committed_change_during_cooperative_pause_cannot_publish_identity(source_fixture, mutation):
    from app.analytics.errors import CanonicalRevisionChanged
    f = source_fixture
    f.source.prepare_question_identity(ACCOUNT)
    f.now[0] = 30
    changed = []

    def pause(duration):
        if changed:
            return
        with f.repositories.database.transaction() as db:
            if mutation == "edit":
                db.execute("UPDATE account_messages SET text=? WHERE message_id=?",
                           ("Synthetic changed during audit", "m-1-1"))
            elif mutation == "delete":
                db.execute("DELETE FROM account_messages WHERE message_id=?", ("m-1-1",))
            else:
                db.execute("DROP TRIGGER analytics_source_token_account_messages_update")
        changed.append(mutation)

    f.source._question_work = cooperative_work(pause)
    with f.source._question_work.foreground():
        with pytest.raises(CanonicalRevisionChanged):
            f.source.prepare_question_identity(ACCOUNT)
    assert changed == [mutation] and f.scan.call_count == 2
    assert not f.source._question_preparation_lock.locked()
    with pytest.raises(ProjectionUnavailable):
        with bounded_scope(f.source):
            pass
    if mutation != "schema":
        expected = canonical_identity(f.source.account_read_model(ACCOUNT))
        assert f.source.prepare_question_identity(ACCOUNT) == expected
