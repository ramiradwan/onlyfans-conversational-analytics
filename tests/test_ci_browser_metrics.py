"""Timing separates scheduling delays and never promotes measurements to qualification."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from tools import browser_ci_metrics as metrics

pytestmark = pytest.mark.ci_tier("fast")
SHA = "a" * 40


def evidence():
    def job(name, start, end, index, steps=()):
        return dict(name=name, id=index, run_id=7, run_attempt=1, head_sha=SHA,
                    status="completed", conclusion="success", started_at=f"2026-10-03T00:00:{start:02}Z",
                    completed_at=f"2026-10-03T00:00:{end:02}Z", steps=list(steps),
                    runner_id=index + 100, runner_name=f"Hosted runner {index}", runner_group_id=0)
    steps = [dict(name=name, number=number, status="completed", conclusion="success",
                  started_at=f"2026-10-03T00:00:{start:02}Z", completed_at=f"2026-10-03T00:00:{end:02}Z")
             for number, (name, start, end) in enumerate([("Setup Node.js", 21, 25),
                                      ("Run browser E2E and preserve output", 26, 35),
                                      ("Seal browser producer evidence", 35, 38)], 1)]
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


@pytest.mark.parametrize("name", [
    "Test local onboarding projection ordering and authority",
    "Test browser E2E projection read recovery",
])
def test_upstream_web_contract_execution_is_not_bootstrap(name):
    run, jobs = evidence()
    web = jobs["jobs"][-1]
    web["name"] = "web-build-and-test"
    web["steps"][0]["name"] = name
    web["steps"][1]["name"] = "Test frontend"
    row = next(row for row in metrics.summarize(run, jobs)["jobs"] if row["job"] == "web-build-and-test")
    assert row["execution_seconds"] == 13
    assert row["bootstrap_seconds"] == 0


@pytest.mark.parametrize("control", [True, False])
def test_aggregate_prerequisite_wait_includes_requested_control_only(control):
    run, jobs = evidence()
    core = jobs["jobs"][-1]
    catchup = deepcopy(core); catchup.update(name="browser-e2e-catchup", id=13)
    serial = deepcopy(core); serial.update(name="browser-e2e-serial-control", id=14,
                                           completed_at="2026-10-03T00:00:45Z", steps=[])
    if not control:
        serial.update(conclusion="skipped", started_at=None, completed_at=None,
                      runner_id=0, runner_name=None)
    aggregate = deepcopy(core); aggregate.update(name="windows-browser-e2e", id=15,
        started_at="2026-10-03T00:00:50Z", completed_at="2026-10-03T00:00:55Z", steps=[])
    jobs["jobs"].extend([catchup, serial, aggregate]); jobs["total_count"] = len(jobs["jobs"])
    row = next(row for row in metrics.summarize(run, jobs)["jobs"] if row["job"] == "windows-browser-e2e")
    assert row["prerequisite_seconds"] == (45 if control else 40)
    assert row["queue_after_prerequisites_seconds"] == (5 if control else 10)


def later_execution(original, *, attempt, identity, conclusion="success"):
    job = deepcopy(original)
    job.update(id=identity, run_attempt=attempt, conclusion=conclusion)
    for record in [job, *job["steps"]]:
        for field in ("started_at", "completed_at"):
            record[field] = (datetime.strptime(record[field], "%Y-%m-%dT%H:%M:%SZ")
                             + timedelta(minutes=attempt - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conclusion != "success":
        job["steps"][-1]["conclusion"] = conclusion
    return job


@pytest.mark.parametrize("reverse_history", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_corroborated_retained_alias_chains_do_not_invent_runner_usage(reverse_history, selected):
    run, document = evidence()
    expected = metrics.summarize(run, document)
    original = document["jobs"][-1]
    if not selected:
        original = deepcopy(original)
        original.update(name="unrelated-real-job", id=90)
        document["jobs"].append(original)
    # GitHub's recorded failed-job rerun copies runner, timestamps and complete
    # step records while assigning new IDs/attempts and changing runner groups.
    for attempt in (2, 3):
        alias = deepcopy(original)
        alias.update(id=100 + attempt, run_attempt=attempt, runner_group_id=None)
        document["jobs"].append(alias)
    run["run_attempt"] = 3
    document["total_count"] = len(document["jobs"])
    if reverse_history:
        document["jobs"].reverse()
    result = metrics.summarize(run, document)
    core = next(row for row in result["jobs"] if row["job"] == "browser-e2e-core")
    assert core["producer_attempt"] == 1
    assert result["latest_selected_runner_minutes"] == expected["latest_selected_runner_minutes"]
    assert result["all_attempts_selected_runner_minutes"] == expected["all_attempts_selected_runner_minutes"]
    assert result["all_attempts_workflow_runner_minutes"] == round((43 + (0 if selected else 20)) / 60, 3)


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "timed_out"])
def test_stale_success_alias_cannot_replace_a_real_later_execution_or_hide_its_cost(conclusion):
    run, document = evidence()
    original = document["jobs"][-1]
    later = later_execution(original, attempt=2, identity=20, conclusion=conclusion)
    alias = deepcopy(original)
    alias.update(id=21, run_attempt=3, runner_group_id=None)
    run["run_attempt"] = 3
    document["jobs"].extend([alias, later])
    document["total_count"] = len(document["jobs"])
    result = metrics.summarize(run, document)
    core = next(row for row in result["jobs"] if row["job"] == "browser-e2e-core")
    assert (core["producer_attempt"], core["conclusion"]) == (2, conclusion)
    assert core["queue_after_prerequisites_seconds"] == 65
    assert result["all_attempts_selected_runner_minutes"] == round(63 / 60, 3)
    assert result["all_attempts_workflow_runner_minutes"] == round(63 / 60, 3)


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_retained_success_alias_does_not_hide_a_newer_unfinished_producer(status):
    run, document = evidence()
    original = document["jobs"][-1]
    newer = later_execution(original, attempt=2, identity=20)
    newer.update(status=status, conclusion=None, completed_at=None)
    alias = deepcopy(original)
    alias.update(id=21, run_attempt=3)
    document["jobs"].extend([newer, alias])
    document["total_count"] = len(document["jobs"])
    run["run_attempt"] = 3
    result = metrics.summarize(run, document)
    assert all(row["job"] != "browser-e2e-core" for row in result["jobs"])
    assert result["all_attempts_selected_runner_minutes"] == round(43 / 60, 3)


@pytest.mark.parametrize("mutation", ["runner", "steps", "missing-time", "step-number", "duplicate-id"])
def test_retained_aliases_require_the_same_complete_execution_evidence_as_the_gates(mutation):
    run, document = evidence()
    alias = deepcopy(document["jobs"][-1])
    alias.update(id=20, run_attempt=2)
    if mutation == "runner": alias["runner_id"] += 1
    if mutation == "steps": alias["steps"][0]["name"] = "different executed step"
    if mutation == "missing-time": alias.pop("started_at")
    if mutation == "step-number": alias["steps"][0]["number"] = True
    if mutation == "duplicate-id": alias["id"] = document["jobs"][-1]["id"]
    run["run_attempt"] = 2
    document["jobs"].append(alias)
    document["total_count"] += 1
    with pytest.raises(metrics.TimingError):
        metrics.summarize(run, document)


@pytest.mark.parametrize("runner", [None, 0])
def test_copied_runnerless_skips_have_no_duration_even_when_their_start_moves_after_their_end(runner):
    run, document = evidence()
    expected = metrics.summarize(run, document)
    for attempt in (1, 2):
        document["jobs"].append(dict(name="browser-e2e-serial-control", id=30 + attempt,
            run_id=7, run_attempt=attempt, head_sha=SHA, status="completed", conclusion="skipped",
            started_at=f"2026-10-03T00:0{attempt}:00Z", completed_at="2026-10-03T00:00:00Z",
            runner_id=runner, runner_name=None, steps=[]))
    run["run_attempt"] = 2
    document["total_count"] = len(document["jobs"])
    result = metrics.summarize(run, document)
    assert result["jobs"] == expected["jobs"]
    assert result["all_attempts_workflow_runner_minutes"] == expected["all_attempts_workflow_runner_minutes"]


@pytest.mark.parametrize("mutation", ["owned-runner", "steps", "missing-steps", "success"])
def test_skip_interval_exception_does_not_hide_owned_or_conflicting_execution_evidence(mutation):
    run, document = evidence()
    job = deepcopy(document["jobs"][-1])
    job.update(id=30, name="administrative-placeholder", conclusion="skipped", runner_id=0,
               runner_name=None, steps=[], started_at="2026-10-03T00:01:00Z",
               completed_at="2026-10-03T00:00:00Z")
    if mutation == "owned-runner": job.update(runner_id=99, runner_name="Owned runner")
    if mutation == "steps": job["steps"] = deepcopy(document["jobs"][-1]["steps"])
    if mutation == "missing-steps": job.pop("steps")
    if mutation == "success": job["conclusion"] = "success"
    document["jobs"].append(job)
    document["total_count"] += 1
    with pytest.raises(metrics.TimingError):
        metrics.summarize(run, document)


def test_completed_actual_execution_without_timestamps_is_not_silently_omitted():
    run, document = evidence()
    document["jobs"][-1].pop("started_at")
    with pytest.raises(metrics.TimingError):
        metrics.summarize(run, document)


def test_complete_saved_history_is_paginated_through_the_shared_resolver():
    run, document = evidence()
    for identity in range(100, 202):
        job = deepcopy(document["jobs"][-1])
        job.update(id=identity, name=f"unrelated-job-{identity}")
        document["jobs"].append(job)
    document["total_count"] = len(document["jobs"])
    result = metrics.summarize(run, document)
    assert result["all_attempts_workflow_runner_minutes"] == round((43 + 102 * 20) / 60, 3)
