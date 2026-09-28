"""Pending reads preserve validation and never establish a ready generation."""
from types import SimpleNamespace
from threading import RLock,Event
from unittest.mock import Mock
import pytest
from app.analytics.query_reader import PublishedQuestionReader
from app.analytics.query_service import AnalyticsQuestionService,RegisteredQuestion
from app.analytics.query_runtime import QuestionResources
from app.analytics.errors import InvalidAnalyticsRequest,ProjectionUnavailable
from app.analytics.query_execution import QuestionCancelled,QuestionResultInvalid
from app.analytics.query_cursor import InvalidQuestionCursor
from tests.test_analytics_question_service import policy,plan,NOW,ACCOUNT


def service(pending):
    source=SimpleNamespace(open_question_scope=Mock(side_effect=AssertionError('source read')))
    evidence=SimpleNamespace(clear_account=Mock())
    reader=PublishedQuestionReader(source,None,ACCOUNT,policy(),evidence,'revision','digest',preparing=pending)
    engine=AnalyticsQuestionService(reader,[RegisteredQuestion('no_later_creator_reply.v1','canonical.v1',lambda *args:None)],clock=lambda:NOW)
    return engine,source,evidence


def test_preparing_response_follows_request_and_cursor_validation():
    engine,source,evidence=service(lambda account:True)
    with pytest.raises(InvalidAnalyticsRequest):engine.execute(policy(),plan(timezone='invalid'))
    with pytest.raises(InvalidQuestionCursor):engine.execute(policy(),plan(cursor='broken'))
    with pytest.raises(QuestionCancelled):engine.execute(policy(),plan(),cancellation_check=lambda:True)
    with pytest.raises(ProjectionUnavailable) as result:engine.execute(policy(),plan())
    assert result.value.code=='analytics_projection_building'
    source.open_question_scope.assert_not_called()
    evidence.clear_account.assert_called_once()


def test_foreign_account_is_rejected_before_read_or_pending_notice():
    pending=Mock(return_value=True);engine,source,evidence=service(pending)
    with pytest.raises(QuestionResultInvalid):engine.execute(policy('another'),plan())
    pending.assert_not_called();source.open_question_scope.assert_not_called()


def test_pending_cannot_supply_an_answer_after_update_ends():
    flag=[True];engine,source,evidence=service(lambda account:flag[0])
    with pytest.raises(ProjectionUnavailable):engine.execute(policy(),plan())
    flag[0]=False
    with pytest.raises(QuestionResultInvalid):engine.execute(policy(),plan())
    source.open_question_scope.assert_called_once()


@pytest.mark.parametrize('condition',['closed','cancelled','no_epoch','no_work','not_attempted'])
def test_scheduler_pending_notice_ends_with_ownership(condition):
    from app.analytics.scheduling import InProcessProjectionScheduler,_AccountWork,_CancellationToken
    scheduler=object.__new__(InProcessProjectionScheduler)
    scheduler._state_lock=RLock();scheduler._closed=False;scheduler._publication_closed=Event()
    scheduler._publication_epoch='epoch';scheduler._scheduler_owner_id='owner'
    work=_AccountWork(2,2,_CancellationToken());scheduler._work={ACCOUNT:work}
    assert scheduler._pending_question_state(ACCOUNT)==('owner','epoch',2)
    if condition=='closed':scheduler._closed=True
    elif condition=='cancelled':work.cancellation.cancel()
    elif condition=='no_epoch':scheduler._publication_epoch=None
    elif condition=='no_work':scheduler._work.clear()
    else:work.attempted_revision=None
    assert scheduler._pending_question_state(ACCOUNT) is None
