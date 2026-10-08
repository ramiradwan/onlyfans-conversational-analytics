"""Timing separates scheduling delays and never promotes measurements to qualification."""
from copy import deepcopy

import pytest

from tools import browser_ci_metrics as metrics

pytestmark = pytest.mark.ci_tier("fast")
SHA = "a" * 40


def evidence():
    def job(name, start, end, index, steps=()):
        return dict(name=name, id=index, run_id=7, run_attempt=1, head_sha=SHA,
                    status="completed", conclusion="success", started_at=f"2026-10-03T00:00:{start:02}Z",
                    completed_at=f"2026-10-03T00:00:{end:02}Z", steps=list(steps))
    steps = [dict(name=name, status="completed", conclusion="success",
                  started_at=f"2026-10-03T00:00:{start:02}Z", completed_at=f"2026-10-03T00:00:{end:02}Z")
             for name, start, end in [("Setup Node.js", 21, 25),
                                      ("Run browser E2E and preserve output", 26, 35),
                                      ("Seal browser producer evidence", 35, 38)]]
    jobs = [job("fixed-sqlcipher-wheel", 1, 15, 10), job("browser-reporting-safety", 1, 10, 11),
            job("browser-e2e-core", 20, 40, 12, steps)]
    return dict(id=7, head_sha=SHA, run_attempt=1, created_at="2026-10-03T00:00:00Z"), dict(jobs=jobs, total_count=len(jobs))


def test_queue_prerequisites_steps_and_runner_time_are_distinct():
    run, jobs = evidence()
    result = metrics.summarize(run, jobs)
    row = next(row for row in result["jobs"] if row["job"] == "browser-e2e-core")
    assert row["prerequisite_seconds"] == 15
    assert row["queue_after_prerequisites_seconds"] == 5
    assert (row["bootstrap_seconds"], row["execution_seconds"], row["assembly_seconds"], row["runner_seconds"]) == (4, 9, 3, 20)
    assert row["unallocated_runner_seconds"] == 4


@pytest.mark.parametrize("mutation", ["wrong_source", "wrong_run", "duplicate", "incomplete", "overlap"])
def test_bad_metadata_cannot_silently_produce_timings(mutation):
    run, jobs = evidence()
    if mutation == "wrong_source": jobs["jobs"][-1]["head_sha"] = "b" * 40
    if mutation == "wrong_run": jobs["jobs"][-1]["run_id"] = 8
    if mutation == "duplicate": jobs["jobs"].append(deepcopy(jobs["jobs"][-1])); jobs["total_count"] += 1
    if mutation == "incomplete": jobs["total_count"] += 1
    if mutation == "overlap": jobs["jobs"][-1]["steps"][1]["started_at"] = "2026-10-03T00:00:24Z"
    with pytest.raises(metrics.TimingError): metrics.summarize(run, jobs)


def test_newer_failure_supersedes_old_success_and_preserves_original_timing():
    run, jobs = evidence()
    run["run_attempt"] = 2
    new = deepcopy(jobs["jobs"][-1]); new.update(id=20, run_attempt=2, conclusion="failure")
    jobs["jobs"].append(new); jobs["total_count"] += 1
    result = metrics.summarize(run, jobs)
    row = next(row for row in result["jobs"] if row["job"] == "browser-e2e-core")
    assert row["producer_attempt"] == 2 and row["conclusion"] == "failure"
    assert result["all_attempts_selected_runner_minutes"] > result["latest_selected_runner_minutes"]
    observed = metrics.observations([result, result, result])
    assert next(row for row in observed if row["job"] == "browser-e2e-core")["observations"] == 3
    assert all("p95" not in row for row in observed)


def test_safety_and_existing_legal_scenario_execution_are_not_bootstrap():
    run, jobs = evidence()
    jobs["jobs"][-1]["steps"][0]["name"] = "Run Product #5 evidence scenarios"
    safety = jobs["jobs"][1]
    safety["steps"] = [dict(name="Test Python session diagnostics explicitly", status="completed",
                            conclusion="success", started_at=safety["started_at"], completed_at=safety["completed_at"])]
    result = metrics.summarize(run, jobs)
    assert next(row for row in result["jobs"] if row["job"] == "browser-e2e-core")["execution_seconds"] == 13
    assert next(row for row in result["jobs"] if row["job"] == "browser-reporting-safety")["execution_seconds"] == 9


@pytest.mark.parametrize("control", [True, False])
def test_aggregate_prerequisite_wait_includes_requested_control_only(control):
    run, jobs = evidence()
    core = jobs["jobs"][-1]
    catchup = deepcopy(core); catchup.update(name="browser-e2e-catchup", id=13)
    serial = deepcopy(core); serial.update(name="browser-e2e-serial-control", id=14,
                                           completed_at="2026-10-03T00:00:45Z", steps=[])
    if not control: serial.update(conclusion="skipped", started_at=None, completed_at=None)
    aggregate = deepcopy(core); aggregate.update(name="windows-browser-e2e", id=15,
        started_at="2026-10-03T00:00:50Z", completed_at="2026-10-03T00:00:55Z", steps=[])
    jobs["jobs"].extend([catchup, serial, aggregate]); jobs["total_count"] = len(jobs["jobs"])
    row = next(row for row in metrics.summarize(run, jobs)["jobs"] if row["job"] == "windows-browser-e2e")
    assert row["prerequisite_seconds"] == (45 if control else 40)
    assert row["queue_after_prerequisites_seconds"] == (5 if control else 10)
