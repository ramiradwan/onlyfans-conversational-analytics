"""A finished diagnostic with failed questions is not a successful run."""
from copy import deepcopy

import pytest

from tools.qualify_continuous_analytics import measurement_checks_passed

pytestmark = [pytest.mark.ci_tier('fast')]


def successful():
    return {"complete": True, "cleanup_complete": True, "clean_rebuild_equal": True,
            "forced_unchanged_rebuild": True,
            "phases": [{"phase": name} for name in ("cold", "unchanged_rebuild", "one_new_message")],
            "query": {"samples": 100, "failures": {}, "warmup_failures": {}}}


@pytest.mark.parametrize("fault", ["question_errors", "warmup_errors", "cleanup", "oracle", "missing_phase", "exception"])
def test_failed_diagnostics_cannot_pass(fault):
    report = deepcopy(successful())
    assert measurement_checks_passed(report)
    if fault == "question_errors":
        report["query"]["failures"] = {"analytics_question_limit_exceeded": 100}
    elif fault == "warmup_errors":
        report["query"]["warmup_failures"] = {"analytics_question_limit_exceeded": 1}
    elif fault == "cleanup":
        report["cleanup_complete"] = False
    elif fault == "oracle":
        report["clean_rebuild_equal"] = False
    elif fault == "missing_phase":
        report["phases"].pop()
    else:
        report["error_type"] = "RuntimeError"
    assert not measurement_checks_passed(report)
