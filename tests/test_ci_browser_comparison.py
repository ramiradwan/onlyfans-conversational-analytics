"""Clean-pair accounting cannot reuse successful evidence or change candidates."""
from copy import deepcopy

import pytest

from tools import browser_ci_compare as compare

pytestmark = pytest.mark.ci_tier("fast")


def proofs():
    shared = dict(source_commit="a" * 40, registry_sha256="b" * 64, qualification=True,
                  current_attempt=1, retries=0, selected_ids=["capture", "catchup"], selected_count=2)
    control = dict(shared, workflow_run_id=11, serial_control=True,
                   lanes={"legacy": {"runner_duration_ms": 9000, "job_id": 91,
                                     "outcomes": {"capture": "expected", "catchup": "expected"}}})
    split = dict(shared, workflow_run_id=11, serial_control=False,
                 lanes={"core": {"runner_duration_ms": 5000, "job_id": 92, "outcomes": {"capture": "expected"}},
                        "catchup": {"runner_duration_ms": 6000, "job_id": 93, "outcomes": {"catchup": "expected"}}})
    return control, split


@pytest.mark.parametrize("field,value", [("source_commit", "c" * 40), ("registry_sha256", "c" * 64),
    ("retries", 1), ("qualification", False), ("current_attempt", 2), ("workflow_run_id", 12),
    ("selected_ids", ["capture"]), ("selected_count", 1)])
def test_changed_source_retries_reruns_and_lost_coverage_refuse_clean_pair(field, value):
    control, split = proofs(); split[field] = value
    with pytest.raises(compare.ComparisonError): compare.compare_proofs(control, split)


def test_three_different_pairs_report_observations_without_p95_or_target_claim():
    control, split = proofs()
    pairs = []
    for index in range(3):
        control["workflow_run_id"] = split["workflow_run_id"] = 11+index
        pairs.append(compare.compare_proofs(control, split))
    result = compare.summarize_pairs(pairs)
    assert result["three_clean_comparisons"] is True
    assert result["median_split_runner_ms"] == 6000
    assert result["performance_targets_established"] is False
    assert "p95" not in result
    assert compare.summarize_pairs(pairs[:1])["three_clean_comparisons"] is False


def test_duplicate_runs_and_different_candidates_cannot_make_three_pairs():
    pair = compare.compare_proofs(*proofs())
    with pytest.raises(compare.ComparisonError, match="reuses_run"):
        compare.summarize_pairs([pair, deepcopy(pair), deepcopy(pair)])
    other = deepcopy(pair); other.update(workflow_run_id=21, source_commit="c" * 40)
    with pytest.raises(compare.ComparisonError, match="candidates_differ"):
        compare.summarize_pairs([pair, other])


def test_live_verification_refuses_missing_mandatory_jobs_before_reading_receipts(monkeypatch, tmp_path):
    class Client:
        def get(self, path):
            return dict(id=19, head_sha="a" * 40, run_attempt=1, event="workflow_dispatch",
                        status="completed", conclusion="success",
                        path=".github/workflows/ci.yml",
                        repository={"full_name": compare.PRODUCT_REPOSITORY},
                        head_repository={"full_name": compare.PRODUCT_REPOSITORY})
    monkeypatch.setattr(compare, "latest_ci_jobs", lambda *args, **kwargs: {"browser-reporting-safety": {"status": "completed", "conclusion": "success"}})
    monkeypatch.setattr(compare, "fetch_git_blob", lambda *args: ("b" * 40, b"source"))
    monkeypatch.setattr(compare, "product_ci_job_policy", lambda *args: {"browser-reporting-safety", "backend-fast"})
    with pytest.raises(compare.ComparisonError, match="job_topology_differs"):
        compare.verify_run(Client(), 19, tmp_path)


@pytest.mark.parametrize("mutation", ["same_producer", "overlap", "missing", "flaky"])
def test_reused_producers_or_different_outcomes_cannot_qualify(mutation):
    control, split = proofs()
    if mutation == "same_producer": split["lanes"]["core"]["job_id"] = 91
    if mutation == "overlap": split["lanes"]["core"]["outcomes"]["catchup"] = "expected"
    if mutation == "missing": split["lanes"]["catchup"]["outcomes"] = {}
    if mutation == "flaky": split["lanes"]["catchup"]["outcomes"]["catchup"] = "flaky"
    with pytest.raises(compare.ComparisonError): compare.compare_proofs(control, split)
