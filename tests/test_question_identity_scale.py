"""100k identity boundary regression, not full-question latency qualification."""

from pathlib import Path

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.errors import ProjectionUnavailable
from app.analytics.identity import canonical_identity
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from tools import analytics_qualification as qualification
from tools.analytics_qualification_fixture import Workload


@pytest.mark.slow
def test_fresh_and_expired_100k_identity_fits_the_unchanged_request_budget(tmp_path):
    manifest = qualification.read_json(Path(__file__).resolve().parents[1] /
                                      "docs/analytics/acceptance-manifest.json")
    work = Workload(tmp_path, manifest, 100000, question_case="populated")
    try:
        source = HistoryAnalyticsSource(work.f.repositories.history)
        now = [0.0]
        source._identity_cache._clock = lambda: now[0]
        expected = canonical_identity(source.account_read_model(work.account))
        for instant in (0.0, 61.0):
            now[0] = instant
            with pytest.raises(ProjectionUnavailable):
                with source.open_question_scope(work.account, QuestionBudget(QuestionLimits())):
                    pass
            assert source.prepare_question_identity(work.account) == expected
            for _ in range(100):
                budget = QuestionBudget(QuestionLimits())
                with source.open_question_scope(work.account, budget) as scope:
                    assert scope.identity == expected
                assert budget.records_examined == 2
                assert budget.limits.max_records == 10000
    finally:
        work.close()
