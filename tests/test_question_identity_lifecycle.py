"""Real-time lifecycle regressions using the existing synthetic question collector."""

import pytest

from tools import analytics_qualification as qualification
from tools.analytics_qualification_worker import collect
from tests.test_analytics_closure_collectors import configuration


@pytest.mark.slow
@pytest.mark.parametrize("case,state", [
    ("populated", "idle"),
    ("generation_bound_pagination", "mutated"),
])
def test_owned_questions_after_real_idle_and_mutation(tmp_path, monkeypatch, case, state):
    monkeypatch.setenv("OFCA_QUALIFICATION_PROCESS", "lifecycle-test-owner")
    config = configuration(tmp_path, "questions", case)
    config["state"] = state
    config["manifest"]["questions"]["idle_seconds"] = 61
    report = collect(config)
    assert report["complete"], report
    assert not qualification.check_questions(config["manifest"],
        f"questions/reference-windows-16g/{case}/{state}", report), report
    assert report["statistics"]["errors"] == {}
    assert report["scheduler_closed"] and report["detached_workers"] == 0
    if state == "idle":
        assert report["idle_seconds"] >= 61
    else:
        assert report["stale_cursor_rejected"] and report["pagination_complete"]
