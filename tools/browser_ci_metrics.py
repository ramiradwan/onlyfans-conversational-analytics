"""Summarize hosted browser/web job timing from saved GitHub API metadata.

This measures scheduling and runner use; it does not qualify test coverage.
Save the run response as run.json and its complete filter=all jobs response as
jobs.json. Partial reruns use each producer's newest actual execution.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import statistics


DEPENDENCIES = {
    "web-build-and-test": (),
    "browser-reporting-safety": (),
    "fixed-sqlcipher-wheel": (),
    "browser-e2e-core": ("fixed-sqlcipher-wheel", "browser-reporting-safety"),
    "browser-e2e-catchup": ("fixed-sqlcipher-wheel", "browser-reporting-safety"),
    "browser-e2e-serial-control": ("fixed-sqlcipher-wheel", "browser-reporting-safety"),
    "windows-browser-e2e": ("browser-reporting-safety", "browser-e2e-core", "browser-e2e-catchup",
                            "browser-e2e-serial-control"),
}
WEB_EXECUTION = {
    "Check Bridge architecture boundary contracts", "Typecheck frontend design sync",
    "Check unused frontend files and dependencies", "Check Agent architecture boundary contracts",
    "Build frontend", "Check committed theme artifacts against the generator", "Test frontend",
    "Test extension", "Test provisioning page module", "Test analytics conformance adapter",
    "Test local onboarding projection ordering and authority", "Test browser E2E projection read recovery",
    "Test the release bindings gate", "Test the packaged signing rule gate",
    "Build and audit deterministic extension artifact", "Qualify bounded 10k-message snapshot repair",
}
BROWSER_EXECUTION = {"Run browser E2E and preserve output", "Run Product #5 evidence scenarios",
                     "Build fixed SQLCipher wheel", "Test restricted browser reporting",
                     "Probe the actual pinned Playwright reporter", "Test existing browser diagnostic redaction",
                     "Test Python session diagnostics explicitly"}
ASSEMBLY = {
    "Seal browser producer evidence", "Retain immutable browser producer evidence",
    "Retain restricted browser diagnostics", "Upload frontend build", "Upload audited extension artifact",
    "Retrieve immutable browser producer evidence", "Validate browser execution and assemble Legal inputs",
    "Generate Product #5 Legal evidence bundle", "Upload Product #5 Legal evidence bundle",
    "Retain verified browser assembly provenance",
}
CONCLUSIONS = {"success", "failure", "cancelled", "skipped", "timed_out", "neutral", "action_required", "stale", None}


class TimingError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise TimingError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise TimingError("invalid_timestamp") from None


def positive(value):
    return type(value) is int and value > 0


def summarize(run, document):
    source, run_id, attempt = run.get("head_sha"), run.get("id"), run.get("run_attempt")
    if (not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source)
            or not positive(run_id) or not positive(attempt)):
        raise TimingError("invalid_run_identity")
    created = timestamp(run.get("created_at"))
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs or document.get("total_count") != len(jobs):
        raise TimingError("incomplete_job_history")
    latest = {}; identities = set(); execution_ids = set()
    for job in jobs:
        name, producer, execution_id = job.get("name"), job.get("run_attempt"), job.get("id")
        if (not isinstance(name, str) or not name or not positive(producer) or producer > attempt
                or not positive(execution_id) or job.get("run_id") != run_id
                or job.get("head_sha") != source or job.get("conclusion") not in CONCLUSIONS
                or (name, producer) in identities or execution_id in execution_ids):
            raise TimingError("invalid_job_identity")
        identities.add((name, producer)); execution_ids.add(execution_id)
        if name not in latest or latest[name]["run_attempt"] < producer:
            latest[name] = job
    rows = []
    all_runner_seconds = 0
    selected_runner_seconds = 0
    for job in jobs:
        if job.get("status") != "completed" or not job.get("started_at") or not job.get("completed_at"):
            continue
        duration = timestamp(job["completed_at"])-timestamp(job["started_at"])
        if duration < 0:
            raise TimingError("invalid_job_interval")
        all_runner_seconds += duration
        if job["name"] in DEPENDENCIES:
            selected_runner_seconds += duration
    for name, prerequisites in DEPENDENCIES.items():
        if name not in latest:
            continue
        job = latest[name]
        if job.get("status") != "completed" or not job.get("started_at") or not job.get("completed_at"):
            continue
        started, completed = timestamp(job["started_at"]), timestamp(job["completed_at"])
        if completed < started or started < created:
            raise TimingError("invalid_job_interval")
        ready = created
        for dependency in prerequisites:
            if dependency == "browser-e2e-serial-control" and (
                    dependency not in latest or latest[dependency].get("conclusion") == "skipped"):
                # The opt-in control is not a scheduling prerequisite on ordinary runs.
                continue
            # A retained lane may predate a later unrelated rerun. Resolve the
            # prerequisite execution available when THIS producer began.
            predecessors = [row for row in jobs if row["name"] == dependency
                            and row.get("status") == "completed" and row.get("completed_at")
                            and timestamp(row["completed_at"]) <= started]
            if not predecessors:
                raise TimingError("missing_prerequisite_execution")
            predecessor = max(predecessors, key=lambda row: row["run_attempt"])
            ready = max(ready, timestamp(predecessor["completed_at"]))
        phases = {"bootstrap_seconds": 0.0, "execution_seconds": 0.0, "assembly_seconds": 0.0}
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise TimingError("missing_step_timing")
        intervals = []
        for step in steps:
            if step.get("status") != "completed" or step.get("conclusion") == "skipped":
                continue
            begin, end = timestamp(step.get("started_at")), timestamp(step.get("completed_at"))
            if begin < started or end < begin or end > completed:
                raise TimingError("invalid_step_interval")
            intervals.append((begin, end))
            key = ("assembly_seconds" if step.get("name") in ASSEMBLY else
                   "execution_seconds" if step.get("name") in (WEB_EXECUTION if name == "web-build-and-test" else BROWSER_EXECUTION)
                   else "bootstrap_seconds")
            phases[key] += end - begin
        intervals.sort()
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise TimingError("overlapping_step_intervals")
        rows.append(dict(job=name, producer_attempt=job["run_attempt"], conclusion=job["conclusion"],
                         prerequisite_seconds=round(ready-created, 3),
                         queue_after_prerequisites_seconds=round(started-ready, 3),
                         runner_seconds=round(completed-started, 3),
                         unallocated_runner_seconds=round(completed-started-sum(phases.values()), 3),
                         **{key: round(value, 3) for key, value in phases.items()}))
    return dict(schema="browser-ci-timing/v1", source_commit=source, workflow_run_id=run_id,
                current_attempt=attempt, jobs=rows,
                latest_selected_runner_minutes=round(sum(row["runner_seconds"] for row in rows)/60, 3),
                all_attempts_selected_runner_minutes=round(selected_runner_seconds/60, 3),
                all_attempts_workflow_runner_minutes=round(all_runner_seconds/60, 3),
                measurement_scope="selected browser/web jobs; coverage qualification is separate",
                rerun_queue_note="queue includes rerun request delay when attempt exceeds one")


def observations(summaries):
    """Report median and slowest observations without extrapolating p95."""
    result = []
    for name in DEPENDENCIES:
        rows = [row for summary in summaries for row in summary["jobs"] if row["job"] == name]
        if rows:
            result.append(dict(job=name, observations=len(rows),
                               median_runner_seconds=statistics.median(row["runner_seconds"] for row in rows),
                               slowest_runner_seconds=max(row["runner_seconds"] for row in rows)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        summaries = [summarize(json.loads((directory/"run.json").read_text(encoding="utf-8-sig")),
                               json.loads((directory/"jobs.json").read_text(encoding="utf-8-sig")))
                     for directory in args.directories]
        output = dict(runs=summaries, observations=observations(summaries), qualification_claim=False)
        encoded = json.dumps(output, indent=2, allow_nan=False)+"\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("browser_ci_timing_invalid_evidence")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
