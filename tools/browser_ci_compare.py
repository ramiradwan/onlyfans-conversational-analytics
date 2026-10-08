"""Verify hosted serial/split browser pairs on one exact candidate source.

Each --run takes RUN_ID ARTIFACTS for one opt-in paired CI dispatch.
Artifact directories contain the original immutable producer artifact folders.
GitHub run/job history and the registry at that candidate SHA are read afresh;
saved aggregate pass lines cannot establish qualification.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

try:
    from tools import ci_browser_gate as gate
    from tools.engineering_attestation import (GitHubApi, PRODUCT_REPOSITORY, PRODUCT_CI_POLICY_PATH,
        fetch_git_blob, latest_ci_jobs, product_ci_job_policy)
except ModuleNotFoundError:
    import ci_browser_gate as gate
    from engineering_attestation import (GitHubApi, PRODUCT_REPOSITORY, PRODUCT_CI_POLICY_PATH,
        fetch_git_blob, latest_ci_jobs, product_ci_job_policy)


class ComparisonError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ComparisonError(code)


def verify_run(client, run_id, directory):
    path = f"/repos/{PRODUCT_REPOSITORY}/actions/runs/{run_id}"
    run = client.get(path)
    workflow = ".github/workflows/ci.yml"
    require(isinstance(run, dict) and type(run_id) is int and run_id > 0
            and run.get("id") == run_id and run.get("run_attempt") == 1
            and run.get("event") == "workflow_dispatch" and run.get("status") == "completed"
            and run.get("conclusion") == "success" and run.get("path", "").split("@", 1)[0] == workflow
            and run.get("repository", {}).get("full_name") == PRODUCT_REPOSITORY
            and run.get("head_repository", {}).get("full_name") == PRODUCT_REPOSITORY,
            "comparison_requires_clean_dispatch")
    source = run.get("head_sha")
    jobs = latest_ci_jobs(client, run_id=run_id, run_attempt=1, source_commit=source)
    _, workflow_bytes = fetch_git_blob(client, PRODUCT_REPOSITORY, workflow, source)
    _, policy_bytes = fetch_git_blob(client, PRODUCT_REPOSITORY, PRODUCT_CI_POLICY_PATH, source)
    required = product_ci_job_policy(workflow_bytes, policy_bytes)
    require(set(jobs) == required, "comparison_job_topology_differs")
    require(all(job.get("status") == "completed" and job.get("conclusion") == "success"
                for job in jobs.values()), "comparison_has_failed_or_skipped_jobs")
    needs = {"browser-reporting-safety": {"result": "success"},
             "browser-e2e-execution": {"result": "success"},
             "browser-e2e-serial-control": {"result": "success"}}
    gate.validate_jobs(needs, jobs, serial_requested=True, qualification=True)
    _, registry_bytes = fetch_git_blob(client, PRODUCT_REPOSITORY,
                                       "tools/e2e-capture/ci/registry.json", source)
    with tempfile.TemporaryDirectory(prefix="browser-ci-registry-") as temporary:
        registry = Path(temporary)/"registry.json"
        registry.write_bytes(registry_bytes)
        _, proof = gate.validate_evidence(directory, jobs=jobs, source=source,
            run_id=run_id, current_attempt=1, qualification=True, serial_requested=True, registry_path=registry)
    latest = client.get(path)
    require(all(latest.get(key) == run.get(key) for key in
                ("id", "head_sha", "run_attempt", "status", "conclusion")),
            "comparison_run_changed_during_verification")
    return proof


def compare_proofs(control, split):
    """Only independently verified proofs enter this comparison."""
    require(control["source_commit"] == split["source_commit"]
            and control["registry_sha256"] == split["registry_sha256"], "comparison_source_differs")
    require(control["serial_control"] is True and split["serial_control"] is False
            and control["qualification"] is True and split["qualification"] is True
            and control["current_attempt"] == split["current_attempt"] == 1
            and control["workflow_run_id"] == split["workflow_run_id"]
            and control["retries"] == split["retries"] == 0, "comparison_is_not_clean")
    require(control["selected_ids"] == split["selected_ids"]
            and control["selected_count"] == split["selected_count"], "comparison_coverage_differs")
    require(set(control["lanes"]) == {"legacy"} and set(split["lanes"]) == {"core", "catchup"},
            "comparison_lane_topology_differs")
    control_lane = control["lanes"]["legacy"]
    split_lanes = list(split["lanes"].values())
    producer_ids = [control_lane["job_id"], *(lane["job_id"] for lane in split_lanes)]
    require(len(set(producer_ids)) == 3 and all(type(value) is int and value > 0 for value in producer_ids),
            "comparison_reuses_producer")
    combined = {}
    for lane in split_lanes:
        require(not combined.keys() & lane["outcomes"].keys(), "comparison_overlapping_outcomes")
        combined.update(lane["outcomes"])
    require(combined == control_lane["outcomes"]
            and set(combined) == set(control["selected_ids"])
            and set(combined.values()) == {"expected"}, "comparison_outcomes_differ")
    control_ms = control["lanes"]["legacy"]["runner_duration_ms"]
    split_ms = max(lane["runner_duration_ms"] for lane in split["lanes"].values())
    return dict(source_commit=control["source_commit"], registry_sha256=control["registry_sha256"],
                workflow_run_id=control["workflow_run_id"],
                selected_count=control["selected_count"], serial_runner_ms=control_ms,
                slowest_split_runner_ms=split_ms,
                split_runner_ms_sum=sum(lane["runner_duration_ms"] for lane in split["lanes"].values()))


def summarize_pairs(pairs):
    import statistics
    require(bool(pairs), "comparison_has_no_pairs")
    require(len({pair["source_commit"] for pair in pairs}) == 1
            and len({pair["registry_sha256"] for pair in pairs}) == 1, "comparison_candidates_differ")
    executions = [pair["workflow_run_id"] for pair in pairs]
    require(len(set(executions)) == len(executions), "comparison_reuses_run")
    return dict(schema="browser-ci-comparison/v1", source_commit=pairs[0]["source_commit"],
                clean_pairs=len(pairs), three_clean_comparisons=len(pairs) >= 3,
                pairs=pairs,
                median_serial_runner_ms=statistics.median(pair["serial_runner_ms"] for pair in pairs),
                slowest_serial_runner_ms=max(pair["serial_runner_ms"] for pair in pairs),
                median_split_runner_ms=statistics.median(pair["slowest_split_runner_ms"] for pair in pairs),
                slowest_split_runner_ms=max(pair["slowest_split_runner_ms"] for pair in pairs),
                performance_targets_established=False,
                timing_scope="runner supervisor includes independent collection and browser execution; use browser_ci_metrics.py for hosted scheduling/bootstrap/assembly")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", nargs=2, action="append", required=True,
                        metavar=("RUN_ID", "ARTIFACTS"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            token = subprocess.run(["gh", "auth", "token"], check=True, capture_output=True,
                                   text=True).stdout.strip()
        client = GitHubApi(token)
        pairs = []
        for run_id, directory in args.run:
            split = verify_run(client, int(run_id), Path(directory))
            require(split.get("paired_control") is True and isinstance(split.get("control"), dict),
                    "comparison_has_no_serial_control")
            pairs.append(compare_proofs(split["control"], split))
        encoded = json.dumps(summarize_pairs(pairs), indent=2, allow_nan=False)+"\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (gate.BrowserGateError, gate.ContractError, ComparisonError, ValueError, TypeError,
            KeyError, OSError, subprocess.SubprocessError):
        print("browser_ci_comparison_refused")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
