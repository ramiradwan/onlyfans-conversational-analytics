"""Readiness rechecks live source identity after slow stored-state verification."""

import pytest

from app.analytics.errors import AnalyticsError
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture


@pytest.fixture
def ready(tmp_path):
    fixture = make_fixture(tmp_path)
    now = [0.0]
    fixture.source._identity_cache._clock = lambda: now[0]
    fixture.pipeline.project_account(ACCOUNT)
    fixture.identity_time = now
    yield fixture
    cleanup(fixture)


def test_slow_currentness_keeps_identity_available_to_bounded_questions(ready, monkeypatch):
    original = ready.stores.projections.projection_currentness
    def slow(*args):
        result = original(*args)
        ready.identity_time[0] += 61
        return result
    monkeypatch.setattr(ready.stores.projections, 'projection_currentness', slow)
    assert ready.pipeline.projection_is_current(ACCOUNT, 1)
    with ready.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=2))) as scope:
        assert scope.identity.revision == 1


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'revision'])
def test_source_change_during_currentness_cannot_report_ready(ready, monkeypatch, mutation):
    original = ready.stores.projections.projection_currentness
    def changed(*args):
        result = original(*args)
        assert result
        with ready.repositories.database.transaction() as db:
            if mutation == 'edit':
                db.execute("UPDATE account_messages SET text='Changed during readiness'")
            elif mutation == 'delete':
                db.execute("DELETE FROM account_messages WHERE message_id='m-0-0'")
            else:
                db.execute('UPDATE account_heads SET canonical_revision=canonical_revision+1')
        return result
    monkeypatch.setattr(ready.stores.projections, 'projection_currentness', changed)
    assert not ready.pipeline.projection_is_current(ACCOUNT, 1)


def test_fast_currentness_does_not_extend_the_identity_lifetime(ready):
    ready.identity_time[0] = 59
    assert ready.pipeline.projection_is_current(ACCOUNT, 1)
    ready.identity_time[0] = 60
    with pytest.raises(AnalyticsError) as error:
        with ready.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=2))):
            pass
    assert error.value.code == 'analytics_question_limit_exceeded'


def test_legacy_currentness_path_rechecks_identity(ready, monkeypatch):
    original = ready.stores.projections.get
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.identity_time[0] += 61
        return result
    monkeypatch.setattr(ready.stores.projections, 'projection_currentness', None)
    monkeypatch.setattr(ready.stores.projections, 'get', slow)
    assert ready.pipeline.projection_is_current(ACCOUNT, 1)
    with ready.source.open_question_scope(ACCOUNT, QuestionBudget(QuestionLimits(max_records=2))) as scope:
        assert scope.identity.revision == 1


def test_failed_final_identity_read_does_not_report_ready(ready, monkeypatch):
    read = ready.source.read_identity
    calls = []
    def fail_second(account):
        calls.append(account)
        if len(calls) == 2:
            raise RuntimeError('synthetic identity read failure')
        return read(account)
    monkeypatch.setattr(ready.source, 'read_identity', fail_second)
    monkeypatch.setattr(ready.stores.projections, 'projection_currentness', lambda *args: True)
    with pytest.raises(RuntimeError, match='synthetic identity read failure'):
        ready.pipeline.projection_is_current(ACCOUNT, 1)
