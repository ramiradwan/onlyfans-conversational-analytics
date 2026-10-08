from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED_JOBS = {
    "web-build-and-test", "backend-fast", "analytics-integration",
    "fixed-sqlcipher-wheel", "windows-platform-contract",
    "analytics-windows-contract", "windows-browser-e2e", "windows-full-regression",
    "windows-full-shards",
    "analytics-scale-qualification",
}
BACKEND_JOBS = {
    "backend-fast", "analytics-integration", "windows-platform-contract",
    "analytics-windows-contract", "windows-full-shards",
    "analytics-scale-qualification",
}
WINDOWS_CONSUMERS = {
    "windows-platform-contract", "analytics-windows-contract",
    "browser-e2e-execution", "browser-e2e-serial-control", "windows-full-shards",
    "analytics-scale-qualification",
}
WINDOWS_FULL_CUTOVER_RUN = (
    'case "$GITHUB_EVENT_NAME" in\n'
    "  pull_request) test '${{ needs.windows-full-shards.result }}' = 'skipped' ;;\n"
    "  push|workflow_dispatch) test '${{ needs.windows-full-shards.result }}' = 'success' ;;\n"
    '  *) echo "Unsupported Product CI event"; exit 1 ;;\n'
    "esac\n"
)


def _workflow_document() -> dict[str, Any]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{WORKFLOW} is not a mapping document"
    return document


def _assert_gate_covers_every_lane(workflow: dict[str, Any]) -> None:
    jobs = workflow["jobs"]
    assert REQUIRED_JOBS <= set(jobs), "a required lane is missing"
    gate = jobs["required-ci-gate"]
    assert gate["name"] == "Required CI"
    assert gate["if"] == "${{ always() }}", "the gate must evaluate upstream failures"
    assert set(gate["needs"]) == REQUIRED_JOBS, "the gate must wait for every required lane"
    for job_name in REQUIRED_JOBS:
        job = jobs[job_name]
        if job_name == "analytics-scale-qualification":
            assert job.get("if") == "${{ github.event_name != 'pull_request' }}", "scale may skip only pull requests"
        elif job_name == "windows-full-regression":
            assert job.get("if") == "${{ always() }}", "full Windows aggregate must evaluate shard failures"
            assert job["needs"] == "windows-full-shards"
            assert job["steps"][0]["run"] == WINDOWS_FULL_CUTOVER_RUN
        elif job_name == "windows-full-shards":
            assert job.get("if") == "${{ github.event_name == 'push' || github.event_name == 'workflow_dispatch' }}", "exhaustive Windows may skip only pull requests"
        elif job_name == "windows-browser-e2e":
            assert job.get("if") == "${{ always() }}", "browser aggregate must evaluate execution failures"
            assert set(job["needs"]) == {"browser-reporting-safety", "browser-e2e-execution", "browser-e2e-serial-control"}
        else:
            assert "if" not in job, f"required lane {job_name} cannot skip during PR cutover"
        assert not job.get("continue-on-error"), f"required lane {job_name} cannot ignore failures"


def test_ci_uses_pr_scoped_cancellation_but_preserves_every_main_run() -> None:
    workflow = _workflow_document()
    assert workflow.get("permissions") == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == (
        "${{ github.workflow }}-${{ github.event_name }}-"
        "${{ github.event.pull_request.number || github.run_id }}"
    )
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


@pytest.mark.parametrize("mutation", ["missing", "unneeded", "skip", "ignore-failure", "skip-gate", "skip-scale-on-main"])
def test_required_gate_fails_closed_when_a_lane_is_weakened(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_gate_covers_every_lane(workflow)
    broken = deepcopy(workflow)
    jobs = broken["jobs"]
    if mutation == "missing":
        del jobs["analytics-integration"]
    elif mutation == "unneeded":
        jobs["required-ci-gate"]["needs"].remove("analytics-integration")
    elif mutation == "skip":
        jobs["windows-full-regression"]["if"] = "github.event_name != 'pull_request'"
    elif mutation == "ignore-failure":
        jobs["analytics-windows-contract"]["continue-on-error"] = True
    elif mutation == "skip-scale-on-main":
        jobs["analytics-scale-qualification"]["if"] = "github.event_name == 'schedule'"
    else:
        jobs["required-ci-gate"]["if"] = "success()"
    with pytest.raises(AssertionError):
        _assert_gate_covers_every_lane(broken)


def test_all_jobs_have_explicit_timeouts_and_integration_is_bounded() -> None:
    jobs = _workflow_document()["jobs"]
    for name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int) and 0 < timeout <= 60, f"{name} needs a bounded timeout"
    shard = jobs["analytics-integration"]
    assert shard["name"] == "analytics-integration-${{ matrix.shard }}"
    assert shard["strategy"] == {"fail-fast": False, "max-parallel": 4, "matrix": {"shard": [1, 2, 3, 4]}}
    assert shard["timeout-minutes"] == 20


def _assert_full_windows_shards_are_blocking(workflow: dict[str, Any]) -> None:
    _assert_gate_covers_every_lane(workflow)
    job = workflow["jobs"]["windows-full-shards"]
    assert job["name"] == "windows-full-regression-${{ matrix.shard }}"
    assert job["runs-on"] == "windows-latest"
    assert job["strategy"] == {"fail-fast": False, "max-parallel": 2, "matrix": {"shard": [1, 2]}}
    assert job["timeout-minutes"] == 60
    execution = next(step for step in job["steps"] if step.get("name") == "Test complete Windows backend regression shard")
    assert execution["run"] == "python tools/test_backend.py --lane windows-full-regression --shard ${{ matrix.shard }}"
    assert execution["env"]["CI_TEST_LANE"] == "windows-full-regression-${{ matrix.shard }}"
    assert not execution.get("continue-on-error")
    upload = next(step for step in job["steps"] if step.get("name") == "Retain backend timing and selection")
    assert upload["with"]["name"] == "ci-tests-windows-full-regression-${{ matrix.shard }}-${{ env.PRODUCT_SHA }}-${{ github.run_id }}-${{ github.run_attempt }}"


@pytest.mark.parametrize("mutation", ["omit-shard", "overlap-shard", "ignore-failure", "omit-aggregate", "hide-shards", "allow-cancelled"])
def test_full_windows_split_cannot_drop_or_hide_a_required_shard(mutation: str) -> None:
    workflow = _workflow_document()
    _assert_full_windows_shards_are_blocking(workflow)
    broken = deepcopy(workflow)
    jobs = broken["jobs"]
    if mutation == "omit-shard":
        jobs["windows-full-shards"]["strategy"]["matrix"]["shard"] = [1]
    elif mutation == "overlap-shard":
        jobs["windows-full-shards"]["strategy"]["matrix"]["shard"] = [1, 1]
    elif mutation == "ignore-failure":
        jobs["windows-full-shards"]["continue-on-error"] = True
    elif mutation == "omit-aggregate":
        jobs["required-ci-gate"]["needs"].remove("windows-full-regression")
    elif mutation == "hide-shards":
        jobs["required-ci-gate"]["needs"].remove("windows-full-shards")
    else:
        jobs["windows-full-regression"]["steps"][0]["run"] = "true"
    with pytest.raises(AssertionError):
        _assert_full_windows_shards_are_blocking(broken)


def test_windows_consumers_fail_closed_on_the_shared_producer() -> None:
    jobs = _workflow_document()["jobs"]
    for name in WINDOWS_CONSUMERS:
        job = jobs[name]
        if name in {"browser-e2e-execution", "browser-e2e-serial-control"}:
            assert set(job["needs"]) == {"fixed-sqlcipher-wheel", "browser-reporting-safety"}
        else:
            assert job["needs"] == "fixed-sqlcipher-wheel"
        assert job["runs-on"].startswith("windows")
        steps = job["steps"]
        verify = next(step for step in steps if step.get("name") == "Verify fixed SQLCipher CI source")
        install = next(step for step in steps if step.get("name") == "Install backend dependencies")
        assert steps.index(verify) < steps.index(install)
        assert "source SHA mismatch" in verify["run"] and "run ID mismatch" in verify["run"]
        assert "workflow_run_attempt -lt 1" in verify["run"]
        assert "workflow_run_attempt -gt [int]$env:GITHUB_RUN_ATTEMPT" in verify["run"]


def test_every_backend_invocation_retains_distinct_failure_evidence() -> None:
    jobs = _workflow_document()["jobs"]
    lanes = set()
    for name in BACKEND_JOBS:
        steps = jobs[name]["steps"]
        invocations = [step for step in steps if "python -m pytest" in str(step.get("run", "")) or "python tools/test_backend.py" in str(step.get("run", ""))]
        assert invocations, f"{name} has no tests"
        for step in invocations:
            environment = step["env"]
            lane = environment["CI_TEST_LANE"]
            assert lane not in lanes, f"{lane} overwrites another invocation's evidence"
            lanes.add(lane)
            assert environment["CI_REPORT_DIR"] == f"artifacts/ci-tests/{lane}"
            addopts = environment["PYTEST_ADDOPTS"]
            for required in ("-p tools.ci_pytest", "--durations=50", "--durations-min=0.25", f"--junitxml=artifacts/ci-tests/{lane}/junit.xml"):
                assert required in addopts
        upload = next(step for step in steps if step.get("name") == "Retain backend timing and selection")
        assert upload["if"] == "always()"
        assert upload["with"]["retention-days"] == 14
        assert upload["with"]["if-no-files-found"] == "error"
        assert not upload["with"].get("overwrite"), "attempt-specific execution evidence must stay immutable"
        for coordinate in ("${{ env.PRODUCT_SHA }}", "${{ github.run_id }}", "${{ github.run_attempt }}"):
            assert coordinate in upload["with"]["name"]


def test_gate_keeps_attempt_artifacts_separate_and_compatibility_checks_block() -> None:
    jobs = _workflow_document()["jobs"]
    gate = jobs["required-ci-gate"]
    assert gate["permissions"] == {"contents": "read", "actions": "read"}
    download = next(step for step in gate["steps"] if step.get("name") == "Retrieve backend execution evidence")
    assert download["with"]["pattern"] == "ci-tests-*"
    assert download["with"]["merge-multiple"] is False
    check = next(step for step in gate["steps"] if step.get("name") == "Require complete successful CI evidence")
    assert "tools/ci_gate.py" in check["run"]
    assert check["env"]["CI_NEEDS_JSON"] == "${{ toJSON(needs) }}"
    for alias in ("build-and-test", "windows-tests"):
        job = jobs[alias]
        assert job["if"] == "${{ always() }}"
        assert job["needs"] == "required-ci-gate"
        assert job["steps"][0]["run"] == "test '${{ needs.required-ci-gate.result }}' = 'success'"


def test_main_and_manual_qualification_are_explicit_without_duplicate_schedule_or_path_filters() -> None:
    workflow = _workflow_document()
    assert workflow["env"]["CI_POLICY_VERSION"] == "sharded-v3-pr-cutover"
    events = workflow["on"]
    assert set(events) == {"push", "pull_request", "workflow_dispatch"}
    dispatch = events["workflow_dispatch"]
    assert set(dispatch) == {"inputs"}
    assert set(dispatch["inputs"]) == {"browser_qualification", "browser_serial_control"}
    for declaration in dispatch["inputs"].values():
        assert declaration["type"] == "boolean" and declaration["default"] is False
        assert declaration.get("required", False) is False
    assert events["push"] == {"branches": ["main"]}
    assert events["pull_request"] == {"branches": ["main"], "types": ["opened", "synchronize", "reopened"]}
    for event in ("push", "pull_request"):
        assert "paths" not in events[event] and "paths-ignore" not in events[event]
    assert "edited" not in events["pull_request"]["types"]
    architecture = yaml.load((WORKFLOW.parent / "architecture-impact.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert "edited" in architecture["on"]["pull_request"]["types"]


def test_analytics_scale_qualification_uses_complete_analytics_populations() -> None:
    jobs = _workflow_document()["jobs"]
    job = jobs["analytics-scale-qualification"]
    assert job["if"] == "${{ github.event_name != 'pull_request' }}"
    steps = job["steps"]
    execution = next(step for step in steps if step.get("env", {}).get("CI_TEST_LANE") == "analytics-scale-qualification")
    assert execution["run"] == (
        "python tools/test_backend.py scale -- --ignore=tests/test_packaged_runtime.py "
        "--ignore=tests/test_installation_key.py"
    )
    contract_steps = jobs["analytics-windows-contract"]["steps"]
    contract_execution = next(step for step in contract_steps if step.get("env", {}).get("CI_TEST_LANE") == "analytics-windows-contract")
    assert steps[:steps.index(execution)] == contract_steps[:contract_steps.index(contract_execution)]


def test_legacy_reference_collections_are_independent_of_the_new_selector() -> None:
    jobs = _workflow_document()["jobs"]
    for job_name, lane, marker in (
        ("backend-fast", "legacy-linux-reference", "not slow and not windows_production and not stateful_tier_a and not stateful_tier_b and not stateful_agent_tier_a"),
        ("windows-platform-contract", "legacy-windows-reference", "not slow and not stateful_tier_a and not stateful_tier_b and not stateful_agent_tier_a"),
    ):
        references = [step for step in jobs[job_name]["steps"] if step.get("env", {}).get("CI_TEST_LANE") == lane]
        assert len(references) == 1
        reference = references[0]
        assert reference["run"] == f'python -m pytest --collect-only --override-ini=addopts= -m "{marker}"'
        assert "--ci-lane" not in reference["env"]["PYTEST_ADDOPTS"]
