"""Compare indexed reply selection with full canonical message sets."""

from dataclasses import replace
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from app.analytics.opaque_refs import account_ref, message_ref
from app.analytics.query_canonical import CanonicalQuestionScope
from app.analytics.query_contracts import QuestionPlan, utc_instant
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.analytics.query_handlers import _last
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup, insert_message


def resolved(start=None, end=None, cutoff=None):
    plan = QuestionPlan(question='no_later_creator_reply.v1', timezone='UTC',
        start=start or NOW-timedelta(days=1), end=end or NOW, cutoff=cutoff or NOW)
    return SimpleNamespace(plan=plan, cutoff=plan.cutoff,
        retention_cutoff_exclusive=NOW-timedelta(days=90))


def selected(f, question, limit=10000):
    budget = QuestionBudget(QuestionLimits(max_records=limit, wall_clock_ms=5000))
    with f.repositories.database.read() as db:
        scope = CanonicalQuestionScope(db, ACCOUNT, budget)
        values = list(scope.conversations(question, budget))
    return values, budget.records_examined


@pytest.mark.parametrize('offset', [-1, 0, 1, 100, 999, 1000, 999999, 1000000])
@pytest.mark.parametrize('utc_offset', [-5, 0, 3])
def test_exact_instants_survive_coarse_index_rounding(tmp_path, offset, utc_offset):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        times = [NOW-timedelta(hours=1), NOW+timedelta(microseconds=offset),
                 NOW-timedelta(microseconds=1), NOW+timedelta(microseconds=offset)]
        with f.repositories.database.transaction() as db:
            for index, at in enumerate(times):
                insert_message(db, 'chat-0', str(index), at.astimezone(timezone(timedelta(hours=utc_offset))), index)
        records, _ = selected(f, resolved())
        eligible = [(i,t) for i,t in enumerate(times) if t <= NOW]
        latest = max(t for _,t in eligible)
        assert len(records) == 1
        assert {m.message_ref for m in _last(records[0].messages)} == {
            message_ref(ACCOUNT, 'chat-0', str(i)) for i,t in eligible if t == latest}
        assert any(NOW-timedelta(days=1) <= m.sent_at < NOW for m in records[0].messages)
        assert all(m.kind == 'unknown' for m in records[0].messages)
    finally:
        cleanup(f)


def test_later_reply_outside_selection_is_not_hidden(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'request', NOW-timedelta(hours=2), 0)
            insert_message(db, 'chat-0', 'reply', NOW-timedelta(minutes=1), 1)
        records, _ = selected(f, resolved(end=NOW-timedelta(hours=1)))
        assert len(records) == 1
        latest = _last(records[0].messages)
        assert len(latest) == 1 and latest[0].role == 'creator'
    finally:
        cleanup(f)


def test_large_thread_selection_is_bounded_by_returned_records(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            for index in range(10010):
                insert_message(db, 'chat-0', str(index), NOW-timedelta(seconds=10020-index), index)
        records, examined = selected(f, resolved(), limit=50)
        assert len(records) == 1 and examined < 10
        assert len(records[0].messages) == 1
        with f.repositories.database.read() as db:
            plan = list(db.execute('''EXPLAIN QUERY PLAN SELECT message_id FROM account_messages
                WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
                  AND julianday(sent_at)>=julianday(?) ORDER BY julianday(sent_at) DESC LIMIT 10''',
                (ACCOUNT,'chat-0',(NOW-timedelta(hours=1)).isoformat())))
        assert any('analytics_message_time_lookup' in str(tuple(row)) for row in plan)
    finally:
        cleanup(f)


@pytest.mark.parametrize('at', [NOW, NOW+timedelta(microseconds=1), NOW-timedelta(days=90)])
def test_non_selected_boundary_messages_do_not_create_candidates(tmp_path, at):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'boundary', at)
        assert selected(f, resolved())[0] == []
    finally:
        cleanup(f)


def test_unbounded_timestamp_ties_still_exceed_the_work_limit(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=0)
    try:
        with f.repositories.database.transaction() as db:
            for index in range(100):
                insert_message(db, 'chat-0', str(index), NOW-timedelta(seconds=1), index)
        with pytest.raises(QuestionLimitExceeded):
            selected(f, resolved(), limit=50)
    finally:
        cleanup(f)
