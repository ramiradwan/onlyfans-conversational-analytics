"""Visual comparison samples require independent complete producer evidence."""
from copy import deepcopy

import pytest

from tools import visual_ci_compare as compare

pytestmark = pytest.mark.ci_tier("fast")


def proof():
    shared = dict(source_commit="a"*40, inventory_sha256="b"*64, workflow_run_id=7, current_attempt=1,
                  qualification=True, retries=0, selected_ids=["dynamic-case", "freshness-case"], selected_count=2)
    def group(identity, duration, outcomes):
        return dict(job_id=identity, runner_duration_ms=duration, outcomes=outcomes, phase_timings={"prepare": 1})
    control = dict(shared, serial_control=True,
                   groups={"all": group(91, 9000, {"dynamic-case": "passed", "freshness-case": "passed"})})
    return dict(shared, serial_control=False, paired_control=True, control=control,
                groups={"dynamic": group(92, 5000, {"dynamic-case": "passed"}),
                        "remaining": group(93, 6000, {"freshness-case": "passed"})})


@pytest.mark.parametrize("field,value", [("source_commit", "c"*40), ("inventory_sha256", "c"*64),
    ("workflow_run_id", 8), ("qualification", False), ("current_attempt", 2), ("retries", 1),
    ("selected_ids", ["dynamic-case"]), ("selected_count", 1), ("serial_control", False)])
def test_mismatched_control_source_retries_or_missing_cases_refuse_comparison(field, value):
    split = proof(); split["control"][field] = value
    with pytest.raises(compare.ComparisonError): compare.compare_proof(split)


@pytest.mark.parametrize("mutation", ["missing-control", "same-producer", "overlap", "missing-outcome", "failed-outcome"])
def test_missing_or_reused_execution_cannot_qualify(mutation):
    split = proof()
    if mutation == "missing-control": split["paired_control"] = False
    if mutation == "same-producer": split["groups"]["dynamic"]["job_id"] = 91
    if mutation == "overlap": split["groups"]["dynamic"]["outcomes"]["freshness-case"] = "passed"
    if mutation == "missing-outcome": split["groups"]["remaining"]["outcomes"] = {}
    if mutation == "failed-outcome": split["groups"]["remaining"]["outcomes"]["freshness-case"] = "failed"
    with pytest.raises(compare.ComparisonError): compare.compare_proof(split)


def test_three_distinct_clean_runs_report_median_and_slowest_without_target_claim():
    pairs = []
    for run_id in (7, 8, 9):
        split = proof(); split["workflow_run_id"] = split["control"]["workflow_run_id"] = run_id
        pairs.append(compare.compare_proof(split))
    result = compare.summarize_pairs(pairs)
    assert result["three_clean_comparisons"] is True and result["median_split_runner_ms"] == 6000
    assert result["performance_targets_established"] is False and "p95" not in result
    assert compare.summarize_pairs(pairs[:1])["three_clean_comparisons"] is False
    with pytest.raises(compare.ComparisonError, match="reuses_run"):
        compare.summarize_pairs([pairs[0]]*3)
    changed = deepcopy(pairs[1]); changed["source_commit"] = "c"*40
    with pytest.raises(compare.ComparisonError, match="candidates_differ"):
        compare.summarize_pairs([pairs[0], changed])


def test_live_comparison_refuses_missing_mandatory_jobs_before_receipts(monkeypatch, tmp_path):
    class Client:
        def get(self, path):
            return dict(id=19, head_sha="a"*40, run_attempt=1, event="workflow_dispatch", status="completed",
                        conclusion="success", path=compare.WORKFLOW,
                        repository={"full_name": compare.PRODUCT_REPOSITORY},
                        head_repository={"full_name": compare.PRODUCT_REPOSITORY})
    monkeypatch.setattr(compare, "fetch_git_blob", lambda *args: ("b"*40, b"source"))
    monkeypatch.setattr(compare.gate, "validate_policy", lambda *args: {})
    monkeypatch.setattr(compare, "latest_ci_jobs", lambda *args, **kwargs: {"visual-contracts": {"status": "completed", "conclusion": "success"}})
    with pytest.raises(compare.ComparisonError, match="job_topology_differs"):
        compare.verify_run(Client(), 19, tmp_path)
