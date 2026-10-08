"""Verify three distinct hosted visual serial/split comparisons at one source.

Run from a clean checkout of the candidate commit. Each --run takes RUN_ID and
the directory containing the original immutable visual-inputs-* artifact folders.
Fresh GitHub history and source-versioned policy determine eligibility; saved
aggregate summaries cannot establish complete execution or clean comparisons.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess

try:
    from tools import ci_visual_gate as gate
    from tools.engineering_attestation import GitHubApi, PRODUCT_REPOSITORY, fetch_git_blob, latest_ci_jobs
except ModuleNotFoundError:
    import ci_visual_gate as gate
    from engineering_attestation import GitHubApi, PRODUCT_REPOSITORY, fetch_git_blob, latest_ci_jobs

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/visual-capture.yml"
POLICY = "ci/visual-ci-policy.json"
JOB_NAMES = {"visual-contracts", "visual-capture-dynamic", "visual-capture-remaining",
             "visual-capture-serial-control", "visual-capture"}


class ComparisonError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ComparisonError(code)


def verify_run(client, run_id, directory, *, root=ROOT):
    path = f"/repos/{PRODUCT_REPOSITORY}/actions/runs/{run_id}"
    run = client.get(path)
    require(isinstance(run, dict) and type(run_id) is int and run_id > 0
            and run.get("id") == run_id and run.get("run_attempt") == 1
            and run.get("event") == "workflow_dispatch" and run.get("status") == "completed"
            and run.get("conclusion") == "success" and run.get("path", "").split("@", 1)[0] == WORKFLOW
            and run.get("repository", {}).get("full_name") == PRODUCT_REPOSITORY
            and run.get("head_repository", {}).get("full_name") == PRODUCT_REPOSITORY,
            "visual_comparison_requires_clean_dispatch")
    source = run.get("head_sha")
    _, workflow = fetch_git_blob(client, PRODUCT_REPOSITORY, WORKFLOW, source)
    _, policy = fetch_git_blob(client, PRODUCT_REPOSITORY, POLICY, source)
    gate.validate_policy(workflow, policy)
    jobs = latest_ci_jobs(client, run_id=run_id, run_attempt=1, source_commit=source)
    require(set(jobs) == JOB_NAMES, "visual_comparison_job_topology_differs")
    require(all(job.get("status") == "completed" and job.get("conclusion") == "success"
                for job in jobs.values()), "visual_comparison_has_failed_or_skipped_jobs")
    needs = {"visual-contracts": {"result": "success"}, "visual-capture-execution": {"result": "success"},
             "visual-capture-serial-control": {"result": "success"}}
    gate.validate_jobs(needs, jobs, serial_requested=True, qualification=True)
    _, proof = gate.validate_evidence(directory, jobs=jobs, source=source, run_id=run_id,
        current_attempt=1, qualification=True, serial_requested=True, root=root)
    latest = client.get(path)
    require(all(latest.get(key) == run.get(key) for key in
                ("id", "head_sha", "run_attempt", "status", "conclusion")),
            "visual_comparison_changed_during_verification")
    return proof


def compare_proof(split):
    require(split.get("paired_control") is True and isinstance(split.get("control"), dict),
            "visual_comparison_has_no_control")
    control = split["control"]
    require(control["source_commit"] == split["source_commit"]
            and control["inventory_sha256"] == split["inventory_sha256"]
            and control["workflow_run_id"] == split["workflow_run_id"], "visual_comparison_source_differs")
    require(control["qualification"] is split["qualification"] is True
            and control["current_attempt"] == split["current_attempt"] == 1
            and control["serial_control"] is True and split["serial_control"] is False
            and control["retries"] == split["retries"] == 0, "visual_comparison_is_not_clean")
    require(control["selected_ids"] == split["selected_ids"]
            and control["selected_count"] == split["selected_count"], "visual_comparison_coverage_differs")
    require(set(control["groups"]) == {"all"} and set(split["groups"]) == {"dynamic", "remaining"},
            "visual_comparison_groups_differ")
    legacy = control["groups"]["all"]
    groups = list(split["groups"].values())
    producer_ids = [legacy["job_id"], *(group["job_id"] for group in groups)]
    require(len(set(producer_ids)) == 3 and all(type(value) is int and value > 0 for value in producer_ids),
            "visual_comparison_reuses_producer")
    outcomes = {}
    for group in groups:
        require(not outcomes.keys() & group["outcomes"].keys(), "visual_comparison_overlapping_outcomes")
        outcomes.update(group["outcomes"])
    require(outcomes == legacy["outcomes"] and set(outcomes) == set(split["selected_ids"])
            and set(outcomes.values()) == {"passed"}, "visual_comparison_outcomes_differ")
    return dict(source_commit=split["source_commit"], inventory_sha256=split["inventory_sha256"],
                workflow_run_id=split["workflow_run_id"], selected_count=split["selected_count"],
                serial_runner_ms=legacy["runner_duration_ms"],
                slowest_split_runner_ms=max(group["runner_duration_ms"] for group in groups),
                split_runner_ms_sum=sum(group["runner_duration_ms"] for group in groups),
                phase_timings={"all": legacy["phase_timings"],
                               **{name: group["phase_timings"] for name, group in split["groups"].items()}})


def summarize_pairs(pairs):
    require(bool(pairs), "visual_comparison_has_no_pairs")
    require(len({pair["source_commit"] for pair in pairs}) == 1
            and len({pair["inventory_sha256"] for pair in pairs}) == 1, "visual_comparison_candidates_differ")
    require(len({pair["workflow_run_id"] for pair in pairs}) == len(pairs), "visual_comparison_reuses_run")
    return dict(schema="visual-ci-comparison/v1", source_commit=pairs[0]["source_commit"],
                clean_pairs=len(pairs), three_clean_comparisons=len(pairs) >= 3, pairs=pairs,
                median_serial_runner_ms=statistics.median(pair["serial_runner_ms"] for pair in pairs),
                slowest_serial_runner_ms=max(pair["serial_runner_ms"] for pair in pairs),
                median_split_runner_ms=statistics.median(pair["slowest_split_runner_ms"] for pair in pairs),
                slowest_split_runner_ms=max(pair["slowest_split_runner_ms"] for pair in pairs),
                performance_targets_established=False,
                timing_scope="capture runner includes owned fixture preparation; phase timings separate it from scenarios; use visual_ci_metrics.py for hosted scheduling/bootstrap/assembly")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", nargs=2, action="append", required=True, metavar=("RUN_ID", "ARTIFACTS"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            token = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
        client = GitHubApi(token)
        pairs = [compare_proof(verify_run(client, int(run_id), Path(directory))) for run_id, directory in args.run]
        encoded = json.dumps(summarize_pairs(pairs), indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (gate.VisualGateError, gate.ContractError, ComparisonError, ValueError, TypeError,
            KeyError, OSError, subprocess.SubprocessError):
        print("visual_ci_comparison_refused")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
