"""A fixed fixture cutoff must not pretend the application clock stopped."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analytics import query_runtime, query_service
from app.analytics.query_execution import QuestionLimits
from tests.test_analytics_closure_collectors import configuration
from tools import analytics_qualification as q
from tools import analytics_qualification_worker as worker

pytestmark = [pytest.mark.ci_tier('integration')]

LIMIT_ERROR = 'analytics_question_limit_exceeded'


def assert_recording(config, report):
    """Recorded deadline failures still fail qualification on an unqualified host."""
    assert report['complete'] and report['scheduler_closed'], report
    calls, first = report['calls'], report['first_query']
    assert len(calls) == 105, calls
    successes = [call for call in calls if call['error'] is None]
    assert successes, {'first_query': first, 'calls': calls}
    failures = []
    for call in [first, *calls]:
        if call['error'] is None:
            assert call['correct'] is True, call
        else:
            assert (call['error'] == LIMIT_ERROR and call['correct'] is False
                    and all(call[field] is None for field in
                            ('actual', 'rows', 'truncated', 'has_more', 'undetermined_conversations'))), call
            failures.append(call)
    expected_errors = [f"invalid_question_call:{call['index']}"
                       for call in calls if call['error'] is not None]
    if first['error'] is not None:
        expected_errors.append('readiness_or_first_query_missing_or_failed')
    if any(call['error'] is not None for call in calls):
        expected_errors.append('empty_answer_not_empty')
    errors = q.check_questions(config['manifest'],
                              'questions/reference-windows-16g/empty/fresh', report)
    assert errors == expected_errors, {'errors': errors, 'failures': failures, 'report': report}
    if failures:
        assert errors, failures
    if any(call['phase'] == 'measured' for call in failures):
        assert report['statistics']['p95_seconds'] is None, report['statistics']
    assert report['plan']['cutoff'] == config['manifest']['fixture']['evaluation_clock'], report['plan']


def assert_runtime_clock(observations, cutoff):
    assert observations, 'no query clock observations'
    for observation in observations:
        for field in ('clock', 'checked_at'):
            value = observation[field]
            if field == 'checked_at' and value is None:
                continue
            assert observation['before'] <= value <= observation['after'], observation
            assert value > cutoff, observation


def collect_observed(tmp_path, monkeypatch, *, expire_one=False, freeze_clock=False):
    """Observe the real collector in process while retaining its storage reopen."""
    monkeypatch.setenv('OFCA_QUALIFICATION_PROCESS', 'test-owner')
    config = configuration(tmp_path, 'questions', 'empty')
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    config['manifest']['fixture']['evaluation_clock'] = cutoff.isoformat()
    observations, limits = [], []
    resource_type, budget_type = query_runtime.QuestionResources, query_service.QuestionBudget

    class ObservedResources(resource_type):
        def __init__(self, *args, **kwargs):
            if freeze_clock:
                kwargs['clock'] = lambda: cutoff
            super().__init__(*args, **kwargs)

        def execute(self, *args, **kwargs):
            request = kwargs.get('request', args[1] if len(args) > 1 else None)
            if request['question'] != 'no_later_creator_reply.v1':
                return super().execute(*args, **kwargs)
            before = datetime.now(timezone.utc)
            observation = dict(before=before, clock=self.clock(), checked_at=None)
            try:
                result = super().execute(*args, **kwargs)
                observation['checked_at'] = result.checked_at
                return result
            finally:
                observation['after'] = datetime.now(timezone.utc)
                observations.append(observation)

    def observe_budget(*args, **kwargs):
        if expire_one and len(limits) == config['manifest']['questions']['warmups'] + 1:
            ticks = iter((0.0, 1.0))
            kwargs['monotonic'] = lambda: next(ticks, 1.0)
        budget = budget_type(*args, **kwargs)
        limits.append(budget.limits)
        return budget

    def inline_child(child_config, directory, mode):
        assert mode == 'questions-child', mode
        return worker.collect(dict(child_config, mode=mode, output=str(directory)))

    monkeypatch.setattr(query_runtime, 'QuestionResources', ObservedResources)
    monkeypatch.setattr(query_service, 'QuestionBudget', observe_budget)
    monkeypatch.setattr(worker, 'child', inline_child)
    report = worker.collect(config)
    assert report['complete'], report
    assert len(limits) == 106 and all(limit == QuestionLimits() for limit in limits), limits
    assert len(list((Path(config['output']) / 'fresh-process/events').glob('*-question.json'))) == 105
    assert len(observations) == 106, observations
    assert sum(item['checked_at'] is not None for item in observations) == sum(
        call['error'] is None for call in [report['first_query'], *report['calls']]), report
    return config, report, observations, cutoff


def test_query_collector_uses_real_runtime_time_with_a_past_fixture(tmp_path, monkeypatch):
    config, report, observations, cutoff = collect_observed(tmp_path, monkeypatch)
    assert_recording(config, report)
    assert_runtime_clock(observations, cutoff)
    for fault in ('unexpected_error', 'no_answers', 'wrong_answer'):
        damaged = deepcopy(report)
        if fault == 'no_answers':
            for call in [damaged['first_query'], *damaged['calls']]:
                call.update(error=LIMIT_ERROR, correct=False, actual=None)
        elif fault == 'unexpected_error':
            damaged['calls'][0].update(error='analytics_question_result_invalid', correct=False, actual=None)
        else:
            damaged['calls'][0].update(error=None, correct=False)
        with pytest.raises(AssertionError):
            assert_recording(config, damaged)


def test_runtime_clock_recording_retains_a_real_budget_expiry(tmp_path, monkeypatch):
    config, report, observations, cutoff = collect_observed(tmp_path, monkeypatch, expire_one=True)
    assert_recording(config, report)
    assert_runtime_clock(observations, cutoff)
    failed = report['calls'][config['manifest']['questions']['warmups']]
    assert failed['error'] == LIMIT_ERROR and failed['phase'] == 'measured', failed
    assert report['statistics']['p95_seconds'] is None, report['statistics']
    assert q.check_questions(config['manifest'], 'questions/reference-windows-16g/empty/fresh', report)


def test_runtime_clock_observation_rejects_a_frozen_fixture_clock(tmp_path, monkeypatch):
    config, report, observations, cutoff = collect_observed(tmp_path, monkeypatch, freeze_clock=True)
    assert report['scheduler_closed'] and report['detached_workers'] == report['backlog'] == 0, report
    calls = [report['first_query'], *report['calls']]
    assert any(call['error'] == 'analytics_question_result_invalid' for call in calls), calls
    assert all(call['error'] in {'analytics_question_result_invalid', LIMIT_ERROR}
               and call['correct'] is False and call['actual'] is None for call in calls), calls
    assert all(item['clock'] == cutoff and item['checked_at'] is None for item in observations), observations
    assert q.check_questions(config['manifest'], 'questions/reference-windows-16g/empty/fresh', report)
    with pytest.raises(AssertionError):
        assert_recording(config, report)
    with pytest.raises(AssertionError):
        assert_runtime_clock(observations, cutoff)
