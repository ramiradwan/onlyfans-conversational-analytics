"""A fixed fixture cutoff must not pretend the application clock stopped."""
from datetime import datetime, timedelta, timezone
from tests.test_analytics_closure_collectors import configuration
from tools.analytics_qualification_worker import collect

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]


def test_fresh_query_uses_real_runtime_time_with_a_past_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('OFCA_QUALIFICATION_PROCESS', 'test-owner')
    config = configuration(tmp_path, 'questions', 'empty')
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    config['manifest']['fixture']['evaluation_clock'] = cutoff
    report = collect(config)
    assert report['complete'] and report['scheduler_closed']
    assert report['first_query']['error'] is None, report['first_query']
    assert all(call['error'] is None and call['correct'] for call in report['calls'])
    assert report['plan']['cutoff'] == cutoff
