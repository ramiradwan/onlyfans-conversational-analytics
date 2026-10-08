"""Visual timing keeps scheduling and execution evidence distinct."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from tools import visual_ci_metrics as metrics

pytestmark = pytest.mark.ci_tier("fast")
SHA = "a"*40


def evidence(control=False):
    def job(name, start, end, identity, steps=()):
        return dict(name=name, id=identity, run_id=7, run_attempt=1, head_sha=SHA, status="completed",
                    conclusion="success", started_at=f"2026-10-03T00:00:{start:02}Z",
                    completed_at=f"2026-10-03T00:00:{end:02}Z", steps=list(steps),
                    runner_id=identity + 100, runner_name=f"Hosted runner {identity}", runner_group_id=0)
    steps = [dict(name=name, number=number, status="completed", conclusion="success",
                  started_at=f"2026-10-03T00:00:{start:02}Z", completed_at=f"2026-10-03T00:00:{end:02}Z")
             for number, (name, start, end) in enumerate([
                 ("Setup Node.js", 6, 9), ("Capture visual stage group", 10, 28),
                 ("Seal visual producer evidence", 29, 30)], 1)]
    jobs = [job("visual-contracts", 1, 10, 10), job("visual-capture-dynamic", 5, 35, 11, steps),
            job("visual-capture-remaining", 5, 30, 12), job("visual-capture-serial-control", 15, 45, 13),
            job("visual-capture", 50, 55, 14)]
    if not control:
        jobs[3].update(conclusion="skipped", started_at=None, completed_at=None,
                       runner_id=0, runner_name=None)
    return dict(id=7, head_sha=SHA, run_attempt=1, created_at="2026-10-03T00:00:00Z"), dict(jobs=jobs, total_count=5)


@pytest.mark.parametrize("control", [True, False])
def test_independent_capture_groups_and_requested_control_have_distinct_wait_times(control):
    result = metrics.summarize(*evidence(control))
    dynamic = next(row for row in result["jobs"] if row["job"] == "visual-capture-dynamic")
    aggregate = next(row for row in result["jobs"] if row["job"] == "visual-capture")
    assert dynamic["prerequisite_seconds"] == 0 and dynamic["queue_after_prerequisites_seconds"] == 5
    assert (dynamic["bootstrap_seconds"], dynamic["execution_seconds"], dynamic["assembly_seconds"]) == (3, 18, 1)
    assert aggregate["prerequisite_seconds"] == (45 if control else 35)
    assert aggregate["queue_after_prerequisites_seconds"] == (5 if control else 15)
    assert result["qualification_claim"] is False
    assert "internal preparation" in result["measurement_scope"]


@pytest.mark.parametrize("mutation", ["wrong-source", "wrong-run", "duplicate", "missing-history", "overlap"])
def test_visual_metrics_refuse_mismatched_or_incomplete_metadata(mutation):
    run, document = evidence()
    if mutation == "wrong-source": document["jobs"][1]["head_sha"] = "b"*40
    if mutation == "wrong-run": document["jobs"][1]["run_id"] = 8
    if mutation == "duplicate": document["jobs"].append(deepcopy(document["jobs"][1])); document["total_count"] += 1
    if mutation == "missing-history": document["total_count"] += 1
    if mutation == "overlap": document["jobs"][1]["steps"][1]["started_at"] = "2026-10-03T00:00:08Z"
    with pytest.raises(metrics.timing.TimingError): metrics.summarize(run, document)


def test_rerun_runner_cost_is_retained_and_three_timings_never_establish_p95():
    run, document = evidence()
    run["run_attempt"] = 2
    replacement = deepcopy(document["jobs"][1]); replacement.update(id=20, run_attempt=2, conclusion="failure")
    document["jobs"].append(replacement); document["total_count"] += 1
    result = metrics.summarize(run, document)
    assert result["all_attempts_selected_runner_minutes"] > result["latest_selected_runner_minutes"]
    assert next(row for row in result["jobs"] if row["job"] == "visual-capture-dynamic")["conclusion"] == "failure"
    assert next(row for row in metrics.observations([result]*3) if row["job"] == "visual-capture-dynamic")["observations"] == 3
    assert all("p95" not in row for row in metrics.observations([result]*3))


@pytest.mark.parametrize("reverse_history", [False, True])
def test_visual_retained_dependencies_keep_original_attempt_and_count_each_owned_execution_once(reverse_history):
    run, document = evidence()
    expected = metrics.summarize(run, document)
    original = document["jobs"][1]
    for attempt in (2, 3):
        alias = deepcopy(original)
        alias.update(id=20 + attempt, run_attempt=attempt, runner_group_id=None)
        document["jobs"].append(alias)
    run["run_attempt"] = 3
    document["total_count"] = len(document["jobs"])
    if reverse_history:
        document["jobs"].reverse()
    result = metrics.summarize(run, document)
    assert result["jobs"] == expected["jobs"]
    assert result["all_attempts_selected_runner_minutes"] == expected["all_attempts_selected_runner_minutes"]
    assert result["all_attempts_workflow_runner_minutes"] == expected["all_attempts_workflow_runner_minutes"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "success"])
def test_new_visual_execution_supersedes_success_even_when_a_later_attempt_copies_the_old_success(conclusion):
    run, document = evidence()
    original = document["jobs"][1]
    actual = deepcopy(original)
    actual.update(id=21, run_attempt=2, conclusion=conclusion)
    for record in [actual, *actual["steps"]]:
        for field in ("started_at", "completed_at"):
            record[field] = (datetime.strptime(record[field], "%Y-%m-%dT%H:%M:%SZ")
                             + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conclusion != "success":
        actual["steps"][-1]["conclusion"] = conclusion
    alias = deepcopy(original)
    alias.update(id=22, run_attempt=3)
    document["jobs"].extend([alias, actual])
    run["run_attempt"] = 3
    document["total_count"] = len(document["jobs"])
    result = metrics.summarize(run, document)
    dynamic = next(row for row in result["jobs"] if row["job"] == "visual-capture-dynamic")
    assert (dynamic["producer_attempt"], dynamic["conclusion"]) == (2, conclusion)
    assert dynamic["queue_after_prerequisites_seconds"] == 65
    assert result["all_attempts_selected_runner_minutes"] == round(99 / 60, 3)
    assert result["all_attempts_workflow_runner_minutes"] == round(99 / 60, 3)


def test_visual_aggregate_uses_prerequisites_from_its_actual_execution_after_a_partial_rerun():
    run, document = evidence()
    original = document["jobs"][1]
    retained = deepcopy(original)
    retained.update(id=21, run_attempt=2)
    remaining = deepcopy(document["jobs"][2])
    remaining.update(id=22, run_attempt=2, started_at="2026-10-03T00:01:05Z",
                     completed_at="2026-10-03T00:01:30Z")
    aggregate = deepcopy(document["jobs"][4])
    aggregate.update(id=23, run_attempt=2, started_at="2026-10-03T00:01:50Z",
                     completed_at="2026-10-03T00:01:55Z")
    document["jobs"].extend([retained, remaining, aggregate])
    run["run_attempt"] = 2
    document["total_count"] = len(document["jobs"])
    result = metrics.summarize(run, document)
    dynamic = next(row for row in result["jobs"] if row["job"] == "visual-capture-dynamic")
    final = next(row for row in result["jobs"] if row["job"] == "visual-capture")
    assert dynamic["producer_attempt"] == 1
    assert final["producer_attempt"] == 2
    assert final["prerequisite_seconds"] == 90
    assert final["queue_after_prerequisites_seconds"] == 20
    assert result["all_attempts_workflow_runner_minutes"] == round(99 / 60, 3)


def test_visual_metrics_ignore_only_nonexecuted_copied_control_placeholders():
    run, document = evidence()
    expected = metrics.summarize(run, document)
    placeholder = document["jobs"][3]
    placeholder.update(started_at="2026-10-03T00:00:00Z", completed_at="2026-10-03T00:00:00Z")
    copied = deepcopy(placeholder)
    copied.update(id=21, run_attempt=2, started_at="2026-10-03T00:01:00Z")
    document["jobs"].append(copied)
    document["total_count"] += 1
    run["run_attempt"] = 2
    result = metrics.summarize(run, document)
    assert result["jobs"] == expected["jobs"]
    assert result["all_attempts_workflow_runner_minutes"] == expected["all_attempts_workflow_runner_minutes"]
    copied.update(runner_id=77, runner_name="Owned runner")
    with pytest.raises(metrics.timing.TimingError):
        metrics.summarize(run, document)


@pytest.mark.parametrize("mutation", ["runner", "steps", "missing-time", "duplicate-id"])
def test_visual_retained_aliases_refuse_contradictory_or_missing_execution_evidence(mutation):
    run, document = evidence()
    alias = deepcopy(document["jobs"][1])
    alias.update(id=21, run_attempt=2)
    if mutation == "runner": alias["runner_name"] = "Different runner"
    if mutation == "steps": alias["steps"][0]["name"] = "different step"
    if mutation == "missing-time": alias.pop("completed_at")
    if mutation == "duplicate-id": alias["id"] = document["jobs"][1]["id"]
    document["jobs"].append(alias)
    document["total_count"] += 1
    run["run_attempt"] = 2
    with pytest.raises(metrics.timing.TimingError):
        metrics.summarize(run, document)
